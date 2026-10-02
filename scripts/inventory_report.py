#!/usr/bin/env python3
"""Analyze an S3 listing and answer the countable Phase 0 questions.

CLAUDE.md §3 asks seven questions. This script answers the ones that are arithmetic
on a key listing: volume, composition, prefix structure, and duplication candidates.
Questions 5-7 (completeness against the six known sites, access model, PDF integrity)
need judgment and spot-checks, so this script only flags what to look at.

Input is the JSON produced by scripts/phase0_s3_discovery.sh, i.e. a list of
{Key, Size, Modified, Class, ETag}. Reads only; writes nothing unless --json is given.

    python3 scripts/inventory_report.py --input data/raw/s3-inventory.json
    python3 scripts/inventory_report.py --input data/raw/s3-inventory.json \
        --json data/manifests/inventory-summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from pipeline.records import content_type_for  # noqa: E402

# Extensions whose text we can index directly vs. those that will need OCR or are
# not text at all. Phase 0 question 2 turns on this split.
TEXT_EXTRACTABLE = {"pdf", "html", "text", "office"}
NEEDS_OCR_MAYBE = {"pdf", "image"}


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n):,} B"
        n /= 1024
    return f"{n:.1f} TB"


def percentile(sorted_values: list[int], p: float) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, int(round(p / 100 * (len(sorted_values) - 1))))
    return sorted_values[index]


def load(path: Path) -> list[dict]:
    data = json.loads(path.read_text())
    if data is None:
        return []
    if isinstance(data, dict) and "Contents" in data:
        data = data["Contents"]
    if not isinstance(data, list):
        raise SystemExit(f"Expected a JSON list of objects in {path}")
    return data


def extension_of(key: str) -> str:
    suffix = Path(key).suffix.lower()
    return suffix if 1 < len(suffix) <= 6 else "(none)"


def analyze(objects: list[dict]) -> dict:
    sizes = sorted(int(o.get("Size") or 0) for o in objects)
    total_bytes = sum(sizes)

    by_extension: Counter[str] = Counter()
    bytes_by_extension: Counter[str] = Counter()
    by_content_type: Counter[str] = Counter()
    bytes_by_content_type: Counter[str] = Counter()
    by_storage_class: Counter[str] = Counter()
    by_top_prefix: Counter[str] = Counter()
    by_year: Counter[str] = Counter()
    # Duplication candidates: same basename + same size (CLAUDE.md §3 Q4 says check
    # name+size first, then ETag for the candidates).
    name_size_groups: dict[tuple[str, int], list[str]] = defaultdict(list)
    etag_groups: dict[str, list[str]] = defaultdict(list)
    zero_byte: list[str] = []

    for obj in objects:
        key = obj.get("Key", "")
        size = int(obj.get("Size") or 0)
        extension = extension_of(key)
        content_type = content_type_for(key)

        by_extension[extension] += 1
        bytes_by_extension[extension] += size
        by_content_type[content_type] += 1
        bytes_by_content_type[content_type] += size
        by_storage_class[obj.get("Class") or "STANDARD"] += 1
        by_top_prefix[key.split("/")[0] if "/" in key else "(root)"] += 1
        modified = str(obj.get("Modified") or "")
        if len(modified) >= 4 and modified[:4].isdigit():
            by_year[modified[:4]] += 1

        if size == 0:
            zero_byte.append(key)
        else:
            name_size_groups[(Path(key).name.lower(), size)].append(key)
        etag = (obj.get("ETag") or "").strip('"')
        if etag and "-" not in etag:  # multipart ETags are not content hashes
            etag_groups[etag].append(key)

    name_size_dupes = {f"{k[0]}|{k[1]}": v for k, v in name_size_groups.items() if len(v) > 1}
    etag_dupes = {k: v for k, v in etag_groups.items() if len(v) > 1}
    etag_dupe_objects = sum(len(v) - 1 for v in etag_dupes.values())

    extractable = sum(n for t, n in by_content_type.items() if t in TEXT_EXTRACTABLE)

    return {
        "object_count": len(objects),
        "total_bytes": total_bytes,
        "total_bytes_human": human_bytes(total_bytes),
        "size_distribution": {
            "min": sizes[0] if sizes else 0,
            "p50": percentile(sizes, 50),
            "p90": percentile(sizes, 90),
            "p99": percentile(sizes, 99),
            "max": sizes[-1] if sizes else 0,
            "mean": int(total_bytes / len(sizes)) if sizes else 0,
        },
        "by_content_type": dict(by_content_type.most_common()),
        "bytes_by_content_type": dict(bytes_by_content_type.most_common()),
        "by_extension": dict(by_extension.most_common(25)),
        "by_storage_class": dict(by_storage_class),
        "by_top_prefix": dict(by_top_prefix.most_common(40)),
        "by_modified_year": dict(sorted(by_year.items())),
        "text_extractable_count": extractable,
        "text_extractable_share": round(extractable / len(objects), 4) if objects else 0.0,
        "zero_byte_count": len(zero_byte),
        "zero_byte_sample": zero_byte[:20],
        "duplication": {
            "name_size_collision_groups": len(name_size_dupes),
            "etag_identical_groups": len(etag_dupes),
            "etag_redundant_objects": etag_dupe_objects,
            "largest_etag_groups": sorted(
                ({"etag": k, "count": len(v), "keys": v[:5]} for k, v in etag_dupes.items()),
                key=lambda g: -g["count"],
            )[:10],
        },
    }


def print_report(summary: dict, objects: list[dict], sample_size: int) -> None:
    def section(title: str) -> None:
        print(f"\n{title}\n{'-' * len(title)}")

    print("S3 inventory report")
    print("===================")
    print(f"Objects: {summary['object_count']:,}   Total: {summary['total_bytes_human']}")

    section("1. Volume — size distribution")
    dist = summary["size_distribution"]
    for label in ("min", "p50", "p90", "p99", "max", "mean"):
        print(f"  {label:>5}: {human_bytes(dist[label])}")

    section("2. Composition — by content type")
    for content_type, count in summary["by_content_type"].items():
        share = count / max(1, summary["object_count"])
        size = summary["bytes_by_content_type"].get(content_type, 0)
        print(f"  {content_type:<8} {count:>7,}  ({share:>5.1%})  {human_bytes(size)}")
    print(f"\n  Text-extractable share: {summary['text_extractable_share']:.1%} "
          f"({summary['text_extractable_count']:,} objects)")
    print("  NOTE: 'pdf' counts here do NOT distinguish text-layer PDFs from scans.")
    print("        That needs pdftotext per file — see Phase 0 question 2 and §5 OCR.")

    section("   By extension (top 25)")
    for extension, count in summary["by_extension"].items():
        print(f"  {extension:<10} {count:>7,}")

    section("3. Provenance structure — top-level prefixes")
    for prefix, count in summary["by_top_prefix"].items():
        print(f"  {prefix:<44} {count:>7,}")
    print("\n  Ask: do these prefixes map onto the six known source sites?")
    print("  Cross-check against config/buckets.json before writing docs/s3-inventory.md.")

    section("   Objects by LastModified year")
    for year, count in summary["by_modified_year"].items():
        print(f"  {year}  {count:>7,}  {'#' * min(60, max(1, count * 60 // max(summary['by_modified_year'].values())))}")

    section("4. Duplication candidates")
    dup = summary["duplication"]
    print(f"  Same basename + same size:  {dup['name_size_collision_groups']:,} groups")
    print(f"  Identical ETag (non-multipart): {dup['etag_identical_groups']:,} groups, "
          f"{dup['etag_redundant_objects']:,} redundant objects")
    for group in dup["largest_etag_groups"]:
        print(f"    x{group['count']}  {group['keys'][0]}")
        for key in group["keys"][1:]:
            print(f"          {key}")
    print("\n  ETag equality proves identical bytes. It does NOT catch the same document")
    print("  saved twice with different metadata — that needs the text hash from")
    print("  pipeline.records.text_checksum after extraction.")

    section("Storage classes")
    for storage_class, count in summary["by_storage_class"].items():
        print(f"  {storage_class:<24} {count:>7,}")
    if any("GLACIER" in c or "DEEP" in c for c in summary["by_storage_class"]):
        print("  WARNING: Glacier-tiered objects need restore before they can be read.")
        print("           Restore is a billable operation — check with the project owner first.")

    if summary["zero_byte_count"]:
        section("Zero-byte objects (possible truncation or directory markers)")
        print(f"  {summary['zero_byte_count']:,} found. Sample:")
        for key in summary["zero_byte_sample"]:
            print(f"    {key}")

    section(f"Representative key sample ({sample_size})")
    step = max(1, len(objects) // sample_size) if objects else 1
    for obj in objects[::step][:sample_size]:
        print(f"  {human_bytes(int(obj.get('Size') or 0)):>10}  {obj.get('Key','')}")

    section("Still requires hands-on work (not answerable from a key listing)")
    print("  Q5 Completeness — which of the six sites are actually represented here?")
    print("  Q6 Access model — see data/raw/bucket-config.txt from the discovery script.")
    print("  Q7 Integrity — spot-check PDFs: do they open, is there real text, any truncation?")
    print("     Weight the sample toward the oldest material and the largest files.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=Path("data/raw/s3-inventory.json"))
    parser.add_argument("--json", type=Path, default=None,
                        help="Also write the machine-readable summary here "
                             "(data/manifests/ is committed; data/raw/ is not).")
    parser.add_argument("--sample", type=int, default=30,
                        help="Number of keys in the representative sample.")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"No inventory at {args.input}.", file=sys.stderr)
        print("Run scripts/phase0_s3_discovery.sh first (needs AWS credentials).",
              file=sys.stderr)
        return 1

    objects = load(args.input)
    if not objects:
        print(f"{args.input} contains no objects. Empty bucket, or a bad --prefix?",
              file=sys.stderr)
        return 1

    summary = analyze(objects)
    print_report(summary, objects, args.sample)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
        print(f"\nWrote machine-readable summary to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
