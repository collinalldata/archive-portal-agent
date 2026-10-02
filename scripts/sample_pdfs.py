#!/usr/bin/env python3
"""Sample PDFs from the archive and measure how many need OCR. READ-ONLY.

Phase 0 question 2 asks what fraction of the corpus is text-extractable versus
scanned images. At ~56,000 PDFs that ratio decides real money: AWS Textract bills
per page, Tesseract is free but slow, and the answer changes the ingestion design.

Method. Draw a stratified random sample across size bands (a 40 KB PDF and a 400 MB
PDF are different animals), download each to a temp directory, run pdfinfo and
pdftotext, and classify by characters of extracted text per page. Files are deleted
immediately after measurement — nothing is cached and nothing is written to S3.

Estimates are stratum-weighted, so oversampling the rare large-file bands does not
skew the corpus-wide number.

Text is measured over at most --pages-scanned pages per document. A 600-page report
does not need full extraction to reveal whether it has a text layer, and full
extraction on the largest files would dominate the runtime.

    python3 scripts/sample_pdfs.py --sample 300
    python3 scripts/sample_pdfs.py --sample 50 --only www.canopycoalition.example
"""

from __future__ import annotations

import argparse
import gzip
import json
import random
import re
import statistics
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from s3_corpus_summary import is_noise, rel  # noqa: E402

DEFAULT_INDIR = REPO_ROOT / "data" / "raw" / "inventory"
DEFAULT_CONFIG = REPO_ROOT / "config" / "buckets.json"
DEFAULT_OUT = REPO_ROOT / "data" / "manifests" / "pdf-sample.json"

# Size bands, in bytes. Scanned pages are large and text-native pages are small, so
# size is the single best predictor of which way a document will fall.
STRATA = [
    ("tiny",   0,            100_000),
    ("small",  100_000,      1_000_000),
    ("medium", 1_000_000,    10_000_000),
    ("large",  10_000_000,   100_000_000),
    ("huge",   100_000_000,  float("inf")),
]

# Characters of extracted text per page. Below FLOOR there is effectively no text
# layer and the page must be OCR'd. Between FLOOR and RICH there is something —
# usually a scanned page carrying only a header, a stamp, or a bad auto-OCR pass —
# which still needs OCR to be searchable. Above RICH the text layer is real.
CHARS_PER_PAGE_FLOOR = 50
CHARS_PER_PAGE_RICH = 250

# Textract DetectDocumentText, us-east-1, first million pages. Verify before relying
# on it: pricing changes and this is the number that decides the OCR approach.
TEXTRACT_USD_PER_1K_PAGES = 1.50


def stratum_of(size: int) -> str:
    for name, low, high in STRATA:
        if low <= size < high:
            return name
    return "huge"


def load_exclusions(config_path: Path) -> dict[str, list[str]]:
    if not config_path.exists():
        return {}
    config = json.loads(config_path.read_text())
    raw = config.get("exclude_prefixes", {})
    return {k: v for k, v in raw.items() if isinstance(v, list)}


def collect_pdfs(indir: Path, exclusions: dict[str, list[str]],
                 only: list[str]) -> dict[str, list[dict]]:
    """Every ingestable PDF key, bucketed by size stratum."""
    by_stratum: dict[str, list[dict]] = defaultdict(list)
    for path in sorted(indir.glob("*.jsonl.gz")):
        bucket = path.name[: -len(".jsonl.gz")]
        if only and bucket not in only:
            continue
        excluded = tuple(exclusions.get(bucket, []))
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                obj = json.loads(line)
                key = obj["Key"]
                if not key.lower().endswith(".pdf") or is_noise(key):
                    continue
                if excluded and key.startswith(excluded):
                    continue
                if obj["Size"] == 0:
                    continue
                obj["Bucket"] = bucket
                by_stratum[stratum_of(obj["Size"])].append(obj)
    return by_stratum


def allocate(by_stratum: dict[str, list[dict]], total: int, minimum: int) -> dict[str, int]:
    """Proportional allocation with a floor, so rare strata are still represented."""
    population = sum(len(v) for v in by_stratum.values())
    allocation = {}
    for name, objects in by_stratum.items():
        want = max(minimum, round(total * len(objects) / population)) if population else 0
        allocation[name] = min(want, len(objects))
    return allocation


def measure(obj: dict, session, tmpdir: Path, max_bytes: int, pages_scanned: int) -> dict:
    """Download one PDF, measure its text layer, delete it."""
    import botocore.exceptions

    result = {
        "bucket": obj["Bucket"], "key": obj["Key"], "size": obj["Size"],
        "stratum": stratum_of(obj["Size"]),
    }
    if obj["Size"] > max_bytes:
        result["verdict"] = "skipped_too_large"
        return result

    local = tmpdir / f"{abs(hash(obj['Bucket'] + obj['Key']))}.pdf"
    try:
        session.download_file(obj["Bucket"], obj["Key"], str(local))
    except botocore.exceptions.ClientError as exc:
        result["verdict"] = "download_failed"
        result["error"] = exc.response.get("Error", {}).get("Code", "Unknown")
        return result
    except Exception as exc:  # noqa: BLE001
        result["verdict"] = "download_failed"
        result["error"] = type(exc).__name__
        return result

    try:
        info = subprocess.run(["pdfinfo", str(local)], capture_output=True,
                              text=True, timeout=60)
        if info.returncode != 0:
            stderr = info.stderr.lower()
            if "encrypted" in stderr or "password" in stderr:
                result["verdict"] = "encrypted"
            else:
                result["verdict"] = "corrupt"
                result["error"] = info.stderr.strip()[:120]
            return result

        pages = 0
        for line in info.stdout.splitlines():
            if line.startswith("Pages:"):
                pages = int(line.split(":", 1)[1].strip())
            elif line.startswith("Producer:"):
                result["producer"] = line.split(":", 1)[1].strip()[:80]
        result["pages"] = pages
        if pages == 0:
            result["verdict"] = "corrupt"
            result["error"] = "pdfinfo reported 0 pages"
            return result

        scanned = min(pages, pages_scanned)
        text = subprocess.run(
            ["pdftotext", "-l", str(scanned), "-q", str(local), "-"],
            capture_output=True, text=True, timeout=180)
        body = text.stdout
        # Collapse whitespace: pdftotext pads scanned pages with spaces and newlines,
        # which would otherwise read as content.
        chars = len(re.sub(r"\s+", "", body))
        result["chars_scanned"] = chars
        result["pages_measured"] = scanned
        result["chars_per_page"] = round(chars / scanned, 1) if scanned else 0.0

        if result["chars_per_page"] < CHARS_PER_PAGE_FLOOR:
            result["verdict"] = "needs_ocr"
        elif result["chars_per_page"] < CHARS_PER_PAGE_RICH:
            result["verdict"] = "sparse_text"
        else:
            result["verdict"] = "text_layer"
    except subprocess.TimeoutExpired:
        result["verdict"] = "timeout"
    finally:
        local.unlink(missing_ok=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--indir", type=Path, default=DEFAULT_INDIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--sample", type=int, default=300, help="Target sample size.")
    parser.add_argument("--min-per-stratum", type=int, default=15)
    parser.add_argument("--max-mb", type=int, default=64,
                        help="Skip files larger than this rather than pay to move them.")
    parser.add_argument("--pages-scanned", type=int, default=30,
                        help="Pages per document to extract text from.")
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--seed", type=int, default=20260907,
                        help="Fixed so the sample is reproducible.")
    parser.add_argument("--only", action="append", default=[], metavar="BUCKET")
    args = parser.parse_args()

    try:
        import boto3
    except ImportError:
        print("boto3 is not installed. Run: pip3 install -r requirements.txt", file=sys.stderr)
        return 1

    exclusions = load_exclusions(args.config)
    by_stratum = collect_pdfs(args.indir, exclusions, args.only)
    population = sum(len(v) for v in by_stratum.values())
    if not population:
        print(f"No ingestable PDFs found in {args.indir}.", file=sys.stderr)
        return 1

    allocation = allocate(by_stratum, args.sample, args.min_per_stratum)
    rng = random.Random(args.seed)
    chosen: list[dict] = []
    for name, count in allocation.items():
        chosen.extend(rng.sample(by_stratum[name], count))

    print(f"Population: {population:,} ingestable PDFs")
    for name, _, _ in STRATA:
        if name in by_stratum:
            print(f"  {name:<7} {len(by_stratum[name]):>7,} PDFs  "
                  f"-> sampling {allocation.get(name, 0)}")
    print(f"\nDownloading {len(chosen)} PDFs (max {args.max_mb} MB each)...\n")

    # Buckets span two regions; boto3 follows the redirect when the client is
    # region-aware, so build one session and let it resolve per request.
    boto_session = boto3.Session()
    s3 = boto_session.client("s3")

    results: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="pdf-sample-") as tmp:
        tmpdir = Path(tmp)
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(measure, obj, s3, tmpdir, args.max_mb * 1_000_000,
                                   args.pages_scanned) for obj in chosen]
            for done, future in enumerate(futures, 1):
                results.append(future.result())
                if done % 25 == 0:
                    print(f"  {done}/{len(futures)}")

    # Stratum-weighted estimates: each sampled file stands for N_stratum / n_stratum
    # files in the corpus, so rare strata do not distort the corpus-wide figure.
    weights = {name: len(objs) / allocation[name]
               for name, objs in by_stratum.items() if allocation.get(name)}
    weighted: Counter[str] = Counter()
    for r in results:
        weighted[r["verdict"]] += weights.get(r["stratum"], 0)
    weighted_total = sum(weighted.values())

    verdicts = Counter(r["verdict"] for r in results)
    measured = [r for r in results if "chars_per_page" in r]
    pages = [r["pages"] for r in results if r.get("pages")]

    ocr_share = ((weighted["needs_ocr"] + weighted["sparse_text"]) / weighted_total
                 if weighted_total else 0.0)
    mean_pages = statistics.mean(pages) if pages else 0

    # Page counts must be estimated directly, not as population x share x mean_pages.
    # Documents needing OCR run shorter than text-native ones (scans are often a few
    # pages; born-digital reports are long), so the naive product overstates the OCR
    # page count by roughly half. This is the Horvitz-Thompson estimator: each sampled
    # document contributes its own page count, scaled by its stratum weight.
    est_pages_needing_ocr = int(sum(
        weights.get(r["stratum"], 0) * r.get("pages", 0)
        for r in results if r["verdict"] in {"needs_ocr", "sparse_text"}))
    est_corpus_pages = int(sum(
        weights.get(r["stratum"], 0) * r.get("pages", 0)
        for r in results if r.get("pages")))

    # Standard error of a stratified proportion:
    #   Var = sum_h (W_h^2 * p_h(1 - p_h) / n_h)
    # Reported so nobody treats a single decimal place as precision it does not have.
    strata_counts: dict[str, Counter] = defaultdict(Counter)
    for r in results:
        strata_counts[r["stratum"]][r["verdict"]] += 1
    variance = 0.0
    for name, objs in by_stratum.items():
        n_h = allocation.get(name, 0)
        if n_h < 2:
            continue
        counts = strata_counts[name]
        p_h = (counts["needs_ocr"] + counts["sparse_text"]) / n_h
        w_h = len(objs) / population
        variance += (w_h ** 2) * p_h * (1 - p_h) / n_h
    std_error = variance ** 0.5
    ci95 = (round(max(0.0, ocr_share - 1.96 * std_error), 4),
            round(min(1.0, ocr_share + 1.96 * std_error), 4))

    # Tesseract runs roughly 1.5-3 s/page on scanned text at 300 dpi on one core.
    tesseract_core_hours = est_pages_needing_ocr * 2.0 / 3600

    summary = {
        "population_pdfs": population,
        "sample_size": len(results),
        "seed": args.seed,
        "max_mb": args.max_mb,
        "pages_scanned_per_doc": args.pages_scanned,
        "thresholds": {"chars_per_page_floor": CHARS_PER_PAGE_FLOOR,
                       "chars_per_page_rich": CHARS_PER_PAGE_RICH},
        "strata_population": {n: len(v) for n, v in by_stratum.items()},
        "strata_sampled": allocation,
        "verdicts_raw": dict(verdicts),
        "verdicts_weighted_share": {k: round(v / weighted_total, 4)
                                    for k, v in weighted.items()} if weighted_total else {},
        "ocr_required_share": round(ocr_share, 4),
        "mean_pages_per_pdf": round(mean_pages, 1),
        "median_chars_per_page": round(
            statistics.median(r["chars_per_page"] for r in measured), 1) if measured else 0,
        "ocr_required_share_ci95": ci95,
        "ocr_required_share_stderr": round(std_error, 4),
        "estimated_corpus_pages": est_corpus_pages,
        "estimated_pages_needing_ocr": est_pages_needing_ocr,
        "estimated_textract_usd": round(
            est_pages_needing_ocr / 1000 * TEXTRACT_USD_PER_1K_PAGES, 2),
        "estimated_tesseract_core_hours": round(tesseract_core_hours, 1),
        "mean_pages_by_verdict": {
            v: round(statistics.mean([r["pages"] for r in results
                                      if r["verdict"] == v and r.get("pages")]), 1)
            for v in ("text_layer", "needs_ocr", "sparse_text")
            if any(r["verdict"] == v and r.get("pages") for r in results)},
        "by_stratum": {name: dict(counts) for name, counts in strata_counts.items()},
        "by_bucket": {b: dict(c) for b, c in sorted(
            ((b, Counter(r["verdict"] for r in results if r["bucket"] == b))
             for b in {r["bucket"] for r in results}),
            key=lambda x: -sum(x[1].values()))},
        "top_producers": dict(Counter(
            r["producer"] for r in results if r.get("producer")).most_common(12)),
        "failures": [r for r in results
                     if r["verdict"] in {"corrupt", "download_failed", "timeout", "encrypted"}],
        "results": results,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")

    print(f"\nSample of {len(results)} PDFs from {population:,}")
    for verdict, count in verdicts.most_common():
        share = weighted[verdict] / weighted_total if weighted_total else 0
        print(f"  {verdict:<20} {count:>4}  ({share:6.1%} of corpus, weighted)")
    print(f"\nOCR required:      {ocr_share:.1%} of PDFs "
          f"(95% CI {ci95[0]:.1%}-{ci95[1]:.1%}, n={len(results)})")
    print(f"Mean pages/PDF:    {mean_pages:.1f}  "
          f"-> ~{est_corpus_pages:,} pages corpus-wide")
    print(f"Pages needing OCR: ~{est_pages_needing_ocr:,}")
    print(f"Textract estimate: ~${summary['estimated_textract_usd']:,.2f} "
          f"at ${TEXTRACT_USD_PER_1K_PAGES}/1k pages (verify current pricing)")
    print(f"Tesseract instead: ~{tesseract_core_hours:,.0f} core-hours "
          f"(~{tesseract_core_hours / 16:,.0f}h on 16 cores), $0 licence")
    print(f"\nWrote {rel(args.out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
