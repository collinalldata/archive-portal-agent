#!/usr/bin/env python3
"""List every in-scope S3 bucket to disk, resumably. READ-ONLY.

Phase 0 needs a complete key listing before anything can be analyzed (CLAUDE.md §3).
The corpus is ~250,000 objects across eight buckets in two regions, which is too much
to page through interactively, so this writes the listing to disk and prints only a
few lines. scripts/s3_corpus_summary.py turns those files into the report.

Output, one pair of files per bucket:

    data/raw/inventory/<bucket>.jsonl.gz    one JSON object per line
    data/raw/inventory/<bucket>.meta.json   region, exact counts, completion state

Resumable (CLAUDE.md §5): a partial run leaves a continuation token in the .meta.json
and appends to the same .jsonl.gz on the next run. Gzip members concatenate, so an
appended file stays readable. Re-running a finished bucket is a no-op unless --force.

This script calls only list_buckets, get_bucket_location, and list_objects_v2. It has
no code path that writes to, deletes from, or reconfigures S3 (CLAUDE.md §9).

    python3 scripts/s3_list_buckets.py                  # every in_scope=true bucket
    python3 scripts/s3_list_buckets.py --only www.woodlands-council.example
    python3 scripts/s3_list_buckets.py --include-undecided
"""

from __future__ import annotations

import argparse
import gzip
import json
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = REPO_ROOT / "config" / "buckets.json"
DEFAULT_OUTDIR = REPO_ROOT / "data" / "raw" / "inventory"

# How often to report progress and flush the resume checkpoint. One page is up to
# 1,000 keys, so this checkpoints roughly every 25,000 objects.
CHECKPOINT_EVERY_PAGES = 25


def human_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:,.1f} {unit}" if unit != "B" else f"{int(n):,} B"
        n /= 1024
    return f"{n:.1f} TB"


def load_config(path: Path) -> dict:
    if not path.exists():
        raise SystemExit(f"No bucket registry at {path}. See config/buckets.json.")
    return json.loads(path.read_text())


def select_buckets(config: dict, only: list[str], include_undecided: bool,
                   include_excluded: bool) -> list[dict]:
    buckets = config.get("buckets", [])
    if only:
        wanted = set(only)
        chosen = [b for b in buckets if b["name"] in wanted]
        missing = wanted - {b["name"] for b in chosen}
        if missing:
            raise SystemExit(f"Not in config/buckets.json: {', '.join(sorted(missing))}")
        return chosen
    return [
        b for b in buckets
        if b.get("in_scope") is True
        or (include_undecided and b.get("in_scope") is None)
        or (include_excluded and b.get("in_scope") is False)
    ]


def read_meta(meta_path: Path) -> dict:
    if meta_path.exists():
        try:
            return json.loads(meta_path.read_text())
        except json.JSONDecodeError:
            pass
    return {}


def write_meta(meta_path: Path, meta: dict) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True) + "\n")


def resolve_region(session, bucket: str, fallback: str | None) -> str:
    """Buckets live in different regions (clearcut_watch is us-east-2). Ask, don't guess."""
    try:
        client = session.client("s3")
        location = client.get_bucket_location(Bucket=bucket)["LocationConstraint"]
        return location or "us-east-1"
    except Exception as exc:  # noqa: BLE001 - a permissions gap here is informative, not fatal
        print(f"  ! could not resolve region for {bucket} ({type(exc).__name__});"
              f" falling back to {fallback or 'us-east-1'}", file=sys.stderr)
        return fallback or "us-east-1"


def list_bucket(session, bucket: dict, outdir: Path, force: bool) -> dict:
    import botocore.exceptions

    name = bucket["name"]
    data_path = outdir / f"{name}.jsonl.gz"
    meta_path = outdir / f"{name}.meta.json"
    meta = read_meta(meta_path)

    if meta.get("complete") and not force:
        print(f"  = {name}: already complete "
              f"({meta.get('object_count', 0):,} objects) — skipping")
        return meta

    if force and data_path.exists():
        data_path.unlink()
        meta = {}

    region = meta.get("region") or resolve_region(session, name, bucket.get("region"))
    client = session.client("s3", region_name=region)

    token = meta.get("continuation_token")
    count = int(meta.get("object_count", 0))
    total_bytes = int(meta.get("total_bytes", 0))
    if token:
        print(f"  > {name}: resuming from {count:,} objects already listed")
    else:
        print(f"  > {name}: listing ({region})")

    started = time.time()
    pages = 0
    outdir.mkdir(parents=True, exist_ok=True)

    def checkpoint(next_token: str | None, complete: bool) -> dict:
        state = {
            "bucket": name,
            "region": region,
            "object_count": count,
            "total_bytes": total_bytes,
            "total_bytes_human": human_bytes(total_bytes),
            "complete": complete,
            "continuation_token": next_token,
            "listing_file": str(data_path.relative_to(REPO_ROOT)),
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        write_meta(meta_path, state)
        return state

    # Append mode: a resumed run adds a new gzip member rather than truncating.
    with gzip.open(data_path, "at", encoding="utf-8") as handle:
        while True:
            kwargs = {"Bucket": name, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                page = client.list_objects_v2(**kwargs)
            except botocore.exceptions.ClientError as exc:
                handle.flush()
                code = exc.response.get("Error", {}).get("Code", "Unknown")
                print(f"  ! {name}: {code} after {count:,} objects — checkpointed, rerun to resume",
                      file=sys.stderr)
                state = checkpoint(token, complete=False)
                state["error"] = code
                write_meta(meta_path, state)
                return state

            for obj in page.get("Contents", []):
                record = {
                    "Key": obj["Key"],
                    "Size": obj["Size"],
                    "Modified": obj["LastModified"].isoformat(),
                    "ETag": obj.get("ETag", "").strip('"'),
                    "Class": obj.get("StorageClass", "STANDARD"),
                }
                handle.write(json.dumps(record, separators=(",", ":")) + "\n")
                count += 1
                total_bytes += obj["Size"]

            pages += 1
            token = page.get("NextContinuationToken")
            if pages % CHECKPOINT_EVERY_PAGES == 0:
                handle.flush()
                checkpoint(token, complete=False)
                print(f"    {count:,} objects, {human_bytes(total_bytes)}"
                      f" ({time.time() - started:.0f}s)")
            if not page.get("IsTruncated"):
                break

    state = checkpoint(None, complete=True)
    print(f"  ok {name}: {count:,} objects, {human_bytes(total_bytes)}"
          f" in {time.time() - started:.0f}s")
    return state


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--outdir", type=Path, default=DEFAULT_OUTDIR)
    parser.add_argument("--only", action="append", default=[], metavar="BUCKET",
                        help="List just this bucket. Repeatable. Overrides in_scope.")
    parser.add_argument("--include-undecided", action="store_true",
                        help="Also list buckets with in_scope=null.")
    parser.add_argument("--include-excluded", action="store_true",
                        help="Also list buckets with in_scope=false (e.g. personal-media).")
    parser.add_argument("--force", action="store_true",
                        help="Re-list buckets already marked complete, discarding the old file.")
    args = parser.parse_args()

    try:
        import boto3
    except ImportError:
        print("boto3 is not installed. Run: pip3 install -r requirements.txt", file=sys.stderr)
        return 1

    config = load_config(args.config)
    buckets = select_buckets(config, args.only, args.include_undecided, args.include_excluded)
    if not buckets:
        print("No buckets selected. Try --include-undecided or --only <bucket>.", file=sys.stderr)
        return 1

    session = boto3.Session()
    try:
        identity = session.client("sts").get_caller_identity()
    except Exception as exc:  # noqa: BLE001
        print(f"No usable AWS credentials: {type(exc).__name__}. "
              f"This blocks Phase 0 (CLAUDE.md §8 Q1).", file=sys.stderr)
        return 1

    print(f"Account {identity['Account']} as {identity['Arn'].split('/')[-1]}")
    print(f"Listing {len(buckets)} bucket(s) -> {args.outdir}\n")

    states = [list_bucket(session, bucket, args.outdir, args.force) for bucket in buckets]

    total_objects = sum(int(s.get("object_count", 0)) for s in states)
    total_bytes = sum(int(s.get("total_bytes", 0)) for s in states)
    incomplete = [s["bucket"] for s in states if not s.get("complete")]
    print(f"\n{total_objects:,} objects, {human_bytes(total_bytes)} listed across "
          f"{len(states)} bucket(s).")
    if incomplete:
        print(f"Incomplete (rerun to resume): {', '.join(incomplete)}")
        return 1
    print("Next: python3 scripts/s3_corpus_summary.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
