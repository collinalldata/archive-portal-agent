#!/usr/bin/env bash
# Phase 0 S3 discovery — READ-ONLY (CLAUDE.md §3).
#
# Writes raw output to data/raw/ so it can be analyzed offline. Every command here
# is a read. There is no code path in this script that writes, deletes, or changes
# lifecycle or policy on the bucket. Do not add one — CLAUDE.md §9 requires the project owner's
# explicit approval for any write.
#
# Usage:  ARCHIVE_BUCKET=some-bucket ./scripts/phase0_s3_discovery.sh
#         (or fill in .env first — this script sources it if present)

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="$ROOT/data/raw"
mkdir -p "$OUT"

if [[ -f "$ROOT/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/.env"
  set +a
fi

command -v aws >/dev/null 2>&1 || { echo "FATAL: aws CLI not installed. See README prerequisites." >&2; exit 1; }

if [[ -n "${AWS_PROFILE:-}" ]]; then
  AWS=(aws --profile "$AWS_PROFILE")
else
  AWS=(aws)
fi

log() { printf '\n=== %s ===\n' "$*"; }

log "Identity"
"${AWS[@]}" sts get-caller-identity | tee "$OUT/sts-caller-identity.json"

log "Buckets visible to this identity"
"${AWS[@]}" s3api list-buckets --query 'Buckets[].{Name:Name,Created:CreationDate}' \
  --output table | tee "$OUT/buckets.txt"

BUCKET="${ARCHIVE_BUCKET:-}"
if [[ -z "$BUCKET" ]]; then
  cat >&2 <<'MSG'

ARCHIVE_BUCKET is not set, so bucket-level discovery is stopping here.

The bucket list above is the answer to "what do we have access to". Pick the
archive bucket, put it in .env as ARCHIVE_BUCKET, and re-run. This is open
question #1 in CLAUDE.md §8 and it blocks the rest of Phase 0.
MSG
  exit 0
fi

log "Bucket: $BUCKET"
"${AWS[@]}" s3api get-bucket-location --bucket "$BUCKET" | tee "$OUT/bucket-location.json"

# Access model (Phase 0 question 6). Each of these fails on a bucket that simply
# does not have the setting configured, which is itself an answer — so tolerate
# failure rather than aborting the run.
log "Access model, versioning, lifecycle, encryption"
for probe in get-public-access-block get-bucket-policy-status get-bucket-versioning \
             get-bucket-lifecycle-configuration get-bucket-encryption get-bucket-website; do
  printf -- '--- %s ---\n' "$probe"
  "${AWS[@]}" s3api "$probe" --bucket "$BUCKET" 2>&1 | sed 's/^/    /'
done | tee "$OUT/bucket-config.txt"

log "Object count and total size (summarize)"
PREFIX_ARG=()
[[ -n "${ARCHIVE_PREFIX:-}" ]] && PREFIX_ARG=(--prefix "$ARCHIVE_PREFIX")
"${AWS[@]}" s3 ls "s3://$BUCKET/${ARCHIVE_PREFIX:-}" --recursive --summarize \
  | tail -5 | tee "$OUT/s3-summary.txt"

log "Full paginated inventory -> data/raw/s3-inventory.json"
# --output json with --no-cli-pager; the paginator handles >1000 keys transparently.
"${AWS[@]}" s3api list-objects-v2 --bucket "$BUCKET" "${PREFIX_ARG[@]}" \
  --query 'Contents[].{Key:Key,Size:Size,Modified:LastModified,Class:StorageClass,ETag:ETag}' \
  --output json --no-cli-pager > "$OUT/s3-inventory.json"

COUNT=$(python3 -c "import json,sys;print(len(json.load(open('$OUT/s3-inventory.json')) or []))")
echo "Wrote $COUNT objects to $OUT/s3-inventory.json"

cat <<MSG

Next: analyze it.

    python3 scripts/inventory_report.py --input data/raw/s3-inventory.json

Then write up docs/s3-inventory.md — the numbers, the prefix structure, a
representative key sample, and an explicit list of gaps. Phase 0 is not done
until that file answers all seven questions in CLAUDE.md §3.
MSG
