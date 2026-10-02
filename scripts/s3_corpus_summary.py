#!/usr/bin/env python3
"""Turn the per-bucket listings into the Phase 0 corpus report. READ-ONLY, offline.

Reads data/raw/inventory/*.jsonl.gz (written by scripts/s3_list_buckets.py), runs the
per-bucket analysis from scripts/inventory_report.py, adds the cross-bucket questions
that only make sense corpus-wide, and writes everything to disk.

The point is token economy: the full detail lands in files, and stdout gets a digest
short enough to read in one screen.

Writes:
    data/manifests/inventory-summary.json      per-bucket + corpus totals, committed
    data/manifests/duplicate-candidates.json   cross-bucket ETag collisions, committed
    docs/s3-inventory.md                       the CLAUDE.md §3 deliverable (with --markdown)

    python3 scripts/s3_corpus_summary.py
    python3 scripts/s3_corpus_summary.py --markdown docs/s3-inventory.md
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from inventory_report import analyze, human_bytes  # noqa: E402

DEFAULT_INDIR = REPO_ROOT / "data" / "raw" / "inventory"
DEFAULT_CONFIG = REPO_ROOT / "config" / "buckets.json"
DEFAULT_MANIFESTS = REPO_ROOT / "data" / "manifests"

# Extensions that carry indexable prose vs. the ones that are media or site plumbing.
# Phase 0 question 2 is really "how much of this corpus is actually documents?"
DOCUMENT_EXT = {".pdf", ".doc", ".docx", ".rtf", ".txt", ".odt", ".pages", ".eml"}
WEB_EXT = {".html", ".htm", ".php", ".cfm", ".js", ".css", ".xml", ".asp", ".mo", ".po"}
MEDIA_EXT = {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".mp3", ".m4a", ".wav",
             ".mov", ".mp4", ".avi", ".psd", ".dvf", ".key", ".ppt", ".pptx"}

# Filesystem noise. This corpus was assembled by syncing macOS folders to S3, so it
# carries AppleDouble sidecars, .DS_Store files, zero-byte directory markers, and the
# internals of Apple package bundles (a .pages "file" is really a directory of parts).
# None of it is content. Counting it wrecks both the duplication numbers — 18,145
# zero-byte directory markers share one ETag — and any estimate of ingestion volume.
NOISE_NAMES = {".DS_Store", "Thumbs.db", ".localized", "Icon\r"}
BUNDLE_MARKERS = (".pages/", ".key/", ".numbers/", ".app/", ".sparsebundle/")


def is_noise(key: str) -> bool:
    """True for keys that are filesystem artifacts rather than archived material."""
    if key.endswith("/"):            # zero-byte directory marker
        return True
    if "__MACOSX/" in key:
        return True
    if any(marker in key for marker in BUNDLE_MARKERS):
        return True
    name = key.rsplit("/", 1)[-1]
    return name.startswith("._") or name in NOISE_NAMES


def rel(path: Path) -> str:
    """Display paths relative to the repo when possible; --markdown may be absolute."""
    try:
        return str(path.resolve().relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def read_listing(path: Path) -> list[dict]:
    """Read a (possibly multi-member, from a resumed run) gzipped JSONL listing."""
    objects = []
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                objects.append(json.loads(line))
    return objects


def classify(extension: str, key: str | None = None) -> str:
    if key is not None and is_noise(key):
        return "noise"
    if extension in DOCUMENT_EXT:
        return "document"
    if extension in WEB_EXT:
        return "web"
    if extension in MEDIA_EXT:
        return "media"
    return "other"


def prefix_tree(objects: list[dict], depth: int = 2, top: int = 25) -> dict:
    """Provenance structure (CLAUDE.md §3 Q3): where does the corpus actually live?"""
    counts: Counter[str] = Counter()
    sizes: Counter[str] = Counter()
    for obj in objects:
        parts = obj["Key"].split("/")
        prefix = "/".join(parts[:depth]) if len(parts) > depth else "/".join(parts[:-1]) or "(root)"
        counts[prefix] += 1
        sizes[prefix] += obj["Size"]
    return {p: {"objects": n, "bytes": sizes[p], "human": human_bytes(sizes[p])}
            for p, n in counts.most_common(top)}


def summarize_bucket(name: str, objects: list[dict], meta: dict) -> dict:
    summary = analyze(objects)
    by_class: Counter[str] = Counter()
    bytes_by_class: Counter[str] = Counter()
    for obj in objects:
        suffix = Path(obj["Key"]).suffix.lower()
        extension = suffix if 1 < len(suffix) <= 6 else "(none)"
        material = classify(extension, obj["Key"])
        by_class[material] += 1
        bytes_by_class[material] += obj["Size"]
    summary["bucket"] = name
    summary["region"] = meta.get("region")
    summary["listing_complete"] = meta.get("complete", False)
    summary["by_material_class"] = dict(by_class)
    summary["bytes_by_material_class"] = dict(bytes_by_class)
    summary["prefix_tree"] = prefix_tree(objects)
    return summary


def cross_bucket_duplicates(all_etags: dict[str, list[str]]) -> dict:
    """The same PDF archived from several sites is the expected case (CLAUDE.md §5).

    Only non-multipart ETags are real MD5 content hashes, so multipart uploads are
    excluded upstream, as are zero-byte objects and filesystem noise — otherwise the
    18,145 empty directory markers all collide on one ETag and swamp the result.
    Groups spanning more than one bucket are the interesting ones.
    """
    groups = {etag: keys for etag, keys in all_etags.items() if len(keys) > 1}
    cross = {etag: keys for etag, keys in groups.items()
             if len({k.split("::", 1)[0] for k in keys}) > 1}
    redundant = sum(len(keys) - 1 for keys in groups.values())
    return {
        "identical_content_groups": len(groups),
        "groups_spanning_multiple_buckets": len(cross),
        "redundant_objects": redundant,
        "largest_groups": sorted(
            ({"etag": e, "copies": len(k), "keys": k[:6]} for e, k in groups.items()),
            key=lambda g: -g["copies"])[:25],
    }


def render_markdown(corpus: dict, buckets: list[dict], dupes: dict, config: dict) -> str:
    lines = [
        "# S3 Inventory — Phase 0 Findings",
        "",
        f"**Run date:** {corpus['generated_at']}  ",
        f"**Account:** {config.get('account_id')} as `{config.get('identity')}`  ",
        "**Generated by:** `scripts/s3_list_buckets.py` then `scripts/s3_corpus_summary.py`",
        "",
        "Regenerate with `make s3-inventory`. Do not hand-edit the tables — edit the",
        "scripts or `config/buckets.json` and re-run. Prose commentary below the tables",
        "is preserved by hand and is where judgment calls belong.",
        "",
        "## Corpus totals",
        "",
        f"- **{corpus['object_count']:,} objects**, {corpus['total_bytes_human']}, "
        f"across {len(buckets)} bucket(s)",
        f"- **{corpus['document_count']:,} document-type objects** "
        f"({corpus['document_bytes_human']}) — the material the portal actually indexes",
        f"- **{corpus['pdf_count']:,} PDFs** ({corpus['pdf_bytes_human']})",
        f"- {corpus['noise_count']:,} filesystem-noise objects "
        f"({corpus['noise_bytes_human']}) excluded from every figure above — "
        "AppleDouble sidecars, `.DS_Store`, zero-byte directory markers, and Apple "
        "package-bundle internals. The corpus was synced from macOS folders.",
        "",
        "## Per bucket",
        "",
        "| Bucket | Region | Objects | Size | Documents | PDFs | Media | Web | Noise |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for bucket in buckets:
        classes = bucket["by_material_class"]
        lines.append(
            f"| `{bucket['bucket']}` | {bucket['region']} | {bucket['object_count']:,} | "
            f"{bucket['total_bytes_human']} | {classes.get('document', 0):,} | "
            f"{bucket['by_extension'].get('.pdf', 0):,} | {classes.get('media', 0):,} | "
            f"{classes.get('web', 0):,} | {classes.get('noise', 0):,} |")

    lines += [
        "",
        "## Duplication (Q4)",
        "",
        f"- {dupes['identical_content_groups']:,} groups of byte-identical objects",
        f"- {dupes['groups_spanning_multiple_buckets']:,} of those span more than one bucket",
        f"- {dupes['redundant_objects']:,} objects are redundant copies",
        "",
        "Detail in `data/manifests/duplicate-candidates.json`. Note these are ETag matches,",
        "which catch byte-identical files only. The same document scanned twice, or saved",
        "under different PDF producers, will not match here — that is what content-hash",
        "dedup on extracted text is for (CLAUDE.md §5).",
        "",
        "## Composition by bucket (Q2)",
        "",
    ]
    for bucket in buckets:
        top = list(bucket["by_extension"].items())[:10]
        rendered = ", ".join(f"`{ext}` {n:,}" for ext, n in top)
        lines += [f"**`{bucket['bucket']}`** — {rendered}", ""]

    lines += [
        "## Prefix structure (Q3)",
        "",
        "Top prefixes per bucket, two levels deep. Full detail in",
        "`data/manifests/inventory-summary.json`.",
        "",
    ]
    for bucket in buckets:
        lines += [f"**`{bucket['bucket']}`**", ""]
        for prefix, stats in list(bucket["prefix_tree"].items())[:12]:
            lines.append(f"- `{prefix}/` — {stats['objects']:,} objects, {stats['human']}")
        lines.append("")

    lines += [
        "## Open questions carried forward",
        "",
    ]
    for question in config.get("open_questions", []):
        lines.append(f"- {question}")
    lines += [
        "",
        "## Not answered by this script",
        "",
        "Q5 completeness, Q6 access model, and Q7 PDF integrity need judgment and",
        "spot-checks, not arithmetic. Record those findings below by hand.",
        "",
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--indir", type=Path, default=DEFAULT_INDIR)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--manifests", type=Path, default=DEFAULT_MANIFESTS)
    parser.add_argument("--markdown", type=Path, default=None,
                        help="Also render the human-readable report here, "
                             "e.g. docs/s3-inventory.md.")
    args = parser.parse_args()

    listings = sorted(args.indir.glob("*.jsonl.gz"))
    if not listings:
        print(f"No listings in {args.indir}. Run scripts/s3_list_buckets.py first.",
              file=sys.stderr)
        return 1

    config = json.loads(args.config.read_text()) if args.config.exists() else {}
    bucket_summaries = []
    all_etags: dict[str, list[str]] = defaultdict(list)
    corpus_ext: Counter[str] = Counter()
    corpus_ext_bytes: Counter[str] = Counter()
    noise_count = 0
    noise_bytes = 0

    for path in listings:
        name = path.name[: -len(".jsonl.gz")]
        meta_path = path.with_name(f"{name}.meta.json")
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        objects = read_listing(path)
        if not objects:
            print(f"  ! {name}: listing is empty, skipping", file=sys.stderr)
            continue
        bucket_summaries.append(summarize_bucket(name, objects, meta))
        for obj in objects:
            key = obj["Key"]
            suffix = Path(key).suffix.lower()
            extension = suffix if 1 < len(suffix) <= 6 else "(none)"
            if is_noise(key):
                noise_count += 1
                noise_bytes += obj["Size"]
                continue
            corpus_ext[extension] += 1
            corpus_ext_bytes[extension] += obj["Size"]
            etag = obj.get("ETag", "")
            # Multipart ETags are not content hashes, and empty files all hash alike.
            if etag and "-" not in etag and obj["Size"] > 0:
                all_etags[etag].append(f"{name}::{key}")
        del objects  # these listings are large; do not hold them all at once

    if not bucket_summaries:
        print("Every listing was empty.", file=sys.stderr)
        return 1

    import datetime
    document_count = sum(n for e, n in corpus_ext.items() if classify(e) == "document")
    document_bytes = sum(b for e, b in corpus_ext_bytes.items() if classify(e) == "document")
    total_bytes = sum(corpus_ext_bytes.values())
    corpus = {
        "generated_at": datetime.datetime.now(datetime.timezone.utc)
                                 .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "buckets_analyzed": [b["bucket"] for b in bucket_summaries],
        "object_count": sum(corpus_ext.values()),
        "total_bytes": total_bytes,
        "total_bytes_human": human_bytes(total_bytes),
        "document_count": document_count,
        "document_bytes": document_bytes,
        "document_bytes_human": human_bytes(document_bytes),
        "pdf_count": corpus_ext.get(".pdf", 0),
        "pdf_bytes": corpus_ext_bytes.get(".pdf", 0),
        "pdf_bytes_human": human_bytes(corpus_ext_bytes.get(".pdf", 0)),
        "noise_count": noise_count,
        "noise_bytes": noise_bytes,
        "noise_bytes_human": human_bytes(noise_bytes),
        "by_extension": dict(corpus_ext.most_common(40)),
        "bytes_by_extension": dict(corpus_ext_bytes.most_common(40)),
    }
    dupes = cross_bucket_duplicates(all_etags)

    args.manifests.mkdir(parents=True, exist_ok=True)
    summary_path = args.manifests / "inventory-summary.json"
    dupes_path = args.manifests / "duplicate-candidates.json"
    summary_path.write_text(json.dumps(
        {"corpus": corpus, "buckets": bucket_summaries}, indent=2, sort_keys=True) + "\n")
    dupes_path.write_text(json.dumps(dupes, indent=2, sort_keys=True) + "\n")

    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(render_markdown(corpus, bucket_summaries, dupes, config))

    # Digest only. Everything else is on disk.
    print(f"Corpus: {corpus['object_count']:,} real objects, {corpus['total_bytes_human']}, "
          f"{len(bucket_summaries)} buckets "
          f"(+{noise_count:,} filesystem-noise objects, {human_bytes(noise_bytes)}, excluded)")
    print(f"Documents: {corpus['document_count']:,} ({corpus['document_bytes_human']}), "
          f"of which {corpus['pdf_count']:,} PDFs ({corpus['pdf_bytes_human']})")
    print(f"Duplicates: {dupes['identical_content_groups']:,} identical-content groups, "
          f"{dupes['redundant_objects']:,} redundant objects "
          f"({dupes['groups_spanning_multiple_buckets']:,} cross-bucket)")
    print()
    for bucket in sorted(bucket_summaries, key=lambda b: -b["total_bytes"]):
        flag = "" if bucket["listing_complete"] else "  [PARTIAL LISTING]"
        print(f"  {bucket['bucket']:<32} {bucket['object_count']:>8,} obj  "
              f"{bucket['total_bytes_human']:>10}  "
              f"{bucket['by_extension'].get('.pdf', 0):>6,} pdf{flag}")
    print(f"\nWrote {rel(summary_path)}, {rel(dupes_path)}"
          + (f", {rel(args.markdown)}" if args.markdown else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
