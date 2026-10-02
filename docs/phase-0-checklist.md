# Phase 0 Checklist

Working checklist for S3 discovery. The deliverable is `docs/s3-inventory.md`.

**Read-only phase.** No writes, no deletes, no lifecycle or policy changes without
asking the project owner first (`CLAUDE.md` §3, §9). Do not clean up or restructure anything in
S3 as a side effect of exploring it.

## Blocked on input

- [ ] AWS account / profile
- [ ] Region
- [ ] Bucket name(s)
- [ ] Who owns and pays for the bucket
- [ ] What the archive actually is — static site, raw dump, or crawl output

Everything below is blocked until the bucket is named. Put the answers in `.env`
(copied from `.env.example`) and in `docs/s3-inventory.md`.

## Run

- [ ] `make doctor` — confirm AWS CLI, `pdftotext`, `tesseract` are installed
- [ ] `./scripts/phase0_s3_discovery.sh` — writes to `data/raw/`
- [ ] `python3 scripts/inventory_report.py --input data/raw/s3-inventory.json --json data/manifests/inventory-summary.json`
- [ ] Commit `data/manifests/inventory-summary.json` (small, and worth versioning)

## Answer the seven questions

- [ ] **Q1 Volume** — object count, total bytes, size distribution. Thousands of
      small HTML files, or hundreds of large scans?
- [ ] **Q2 Composition** — breakdown by extension; what fraction is text-extractable
      vs. scanned. Requires sampling PDFs with `pdftotext`, not just counting `.pdf`.
- [ ] **Q3 Provenance** — do key prefixes encode source site? Is there a date
      structure? Any existing manifest? Can original URLs be reconstructed?
- [ ] **Q4 Duplication** — name+size collisions first, then ETag for candidates.
- [ ] **Q5 Completeness** — all six known sites, or only some? Which need a fresh
      scrape? Cross-check `config/buckets.json`.
- [ ] **Q6 Access model** — public/private/CloudFront, web-addressable, versioning,
      lifecycle expiration.
- [ ] **Q7 Integrity** — spot-check at least 10 PDFs. Do they open, is there real
      text, is anything corrupt or truncated?

## Integrity spot-check, concretely

```bash
aws s3 cp "s3://$ARCHIVE_BUCKET/<key>" data/originals/ --no-progress
pdftotext -l 3 data/originals/<file>.pdf - | head -40   # is there a text layer?
pdfinfo data/originals/<file>.pdf                       # page count, producer, damage
```

Roughly: under ~100 characters of extracted text per page means it is a scan and
needs OCR. Weight the sample toward the oldest material and the largest files, where
scans are most likely.

## Then, and only then

- [ ] Write `docs/s3-inventory.md` with numbers, prefix structure, a representative
      key sample, and an explicit list of gaps
- [ ] Recommend an architecture with the tradeoff reasoning; record it as a new entry
      in `docs/decisions.md`
- [ ] Propose a Phase 1 scope
- [ ] **Get the project owner's confirmation before building anything**

## Guardrails while exploring

- Glacier-tiered objects require a billable restore. Check with the project owner first.
- If lifecycle expiration is configured, flag it immediately — that is a countdown
  on material we are trying to preserve.
- Flag scope creep. Discovery describes the archive; it does not reorganize it
  or fix the source sites (`CLAUDE.md` §7).
