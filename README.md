# Archive Portal Agent — S3 discovery and corpus assessment

This repo shows how I run a Claude Code agent through the discovery phase of an archival
preservation project. The agent started with a brief ([`CLAUDE.md`](CLAUDE.md)) and an AWS
account holding about 780 GB across eight S3 buckets that nobody had inventoried. It came
back with a full corpus assessment, a statistically sampled OCR estimate, and an
architecture recommendation grounded in those numbers. It built nothing past that point
until a human signed off.

> **This is a sanitized copy.** People, organizations, domains, bucket names, the AWS
> account, and every S3 object path have been pseudonymized or removed. Aggregate numbers
> are real. See [`SANITIZATION.md`](SANITIZATION.md).

## The problem

Decades of environmental advocacy material — reports, legal filings, FOIA responses,
newsletters, photos — sat in S3 as backups of several aging nonprofit websites. Nobody knew
how much there was, what formats it was in, how much was duplicated, or how much was
scanned images that search can't read. Those answers decide everything downstream: which
search architecture fits, what OCR will cost, and what to build first.

## How the agent works

- **[`CLAUDE.md`](CLAUDE.md) is the operating brief.** It sets scope and hard constraints
  (read-only S3, no restructuring as a side effect of exploring) and explicitly *forbids*
  choosing a stack until the data is measured.
- **Seven questions before any design.** Volume, composition, provenance, duplication,
  completeness, access model, and integrity. The checklist is in
  [`docs/phase-0-checklist.md`](docs/phase-0-checklist.md).
- **Keep the corpus out of the conversation.** Listings stream to disk; the agent reads
  digests. Re-running discovery costs seconds and no context.
- **Write decisions down.** [`docs/decisions.md`](docs/decisions.md) records each call with
  its reasoning and the alternatives rejected, so the next person (or agent) can pick it up
  cold.

## What the assessment found

| Finding | Detail |
|---|---|
| **Scale** | 169,671 objects, 780 GB, 7 buckets analyzed across 2 AWS regions. 55,832 PDFs (200 GB). |
| **Noise** | 29,341 more objects (~15% of all keys) were macOS filesystem artifacts, filtered before any count. One empty-folder marker alone created a fake 18,145-member "duplicate" group until filtered (decision 0007). |
| **Duplication** | 31,446 groups of byte-identical files; 47,736 redundant copies (28%), 5,041 groups spanning buckets. |
| **Structure** | The "website" buckets were whole-server backups, mixing documents with CMS code and logs. Excluded by prefix rather than deleted (decision 0008). |
| **Consent** | ~29,000 personal emails flagged as a rights question and excluded by default. |
| **OCR need** | Stratified sample of 1,532 PDFs: **18.9% need OCR** (95% CI 17.0–20.8%), ~317,000 pages, ~$476 Textract or ~176 Tesseract core-hours. |
| **Shape** | A small high-value core (2,051 PDFs, ~40k pages) and one bulk archive 46× larger (~1.9M pages). |

The last row drove the recommendation: build phase one on the core sites with a simple
static index that can survive neglect, and treat the bulk archive as its own decision
(decision 0010).

Two analytical catches worth calling out, both in
[`docs/pdf-corpus-assessment.md`](docs/pdf-corpus-assessment.md):

- **Stratum weighting.** Sampling oversampled rare large files on purpose, so every
  corpus-wide figure is reweighted. Unweighted, the OCR share would have been inflated.
- **Sum, don't multiply.** Scanned PDFs run shorter than born-digital ones (38.9 vs 52.4
  pages). The naive `population × share × mean pages` overstated OCR pages by 50%. The
  agent caught it and covered the fix with a test.

## Key artifacts

| Artifact | What it is |
|---|---|
| [`docs/s3-inventory.md`](docs/s3-inventory.md) | The Phase 0 report: totals, per-bucket breakdown, composition, duplication, structure. |
| [`docs/pdf-corpus-assessment.md`](docs/pdf-corpus-assessment.md) | OCR sampling method, results, and what they imply. |
| [`docs/decisions.md`](docs/decisions.md) | Ten decisions, from deferring the stack to the phased scope. |
| [`config/buckets.json`](config/buckets.json) | Bucket registry: region, scope, exclusion prefixes, open questions. |
| [`scripts/`](scripts/) | Resumable, region-aware S3 lister; offline corpus summarizer; stratified PDF sampler. |
| [`data/manifests/`](data/manifests/) | Machine-readable outputs (object paths redacted). |
| [`tests/`](tests/) | Tests for noise filtering, classification, the sampling estimator, and the record format. |

## Status

Phase 0 is complete. Phase 1 scope is proposed and awaiting sign-off. The source data isn't
included, so the S3 scripts have nothing to run against here; the tests run anywhere with
Python 3.11+.

## Layout

| Path | What lives here |
|---|---|
| `CLAUDE.md` | Project brief. The source of truth for scope and constraints. |
| `config/buckets.json` | Registry of S3 buckets: region, scope, and exclusion prefixes. |
| `docs/` | Findings, decisions, and the record schema. |
| `docs/decisions.md` | Running decision log. One entry per architectural call. |
| `docs/s3-inventory.md` | Phase 0 deliverable. |
| `docs/schema/` | JSON Schema for the normalized document record. |
| `pipeline/` | Stack-neutral ingestion library: normalized records and a resumable ledger. |
| `scripts/` | Runnable entry points — Phase 0 discovery, inventory analysis. |
| `data/raw/` | S3 listings, scrape output. Gitignored. |
| `data/originals/` | Downloaded original documents. Gitignored — S3 is the system of record. |
| `data/processed/` | Normalized records. Gitignored. |
| `data/manifests/` | Checksums and manifests. **Committed** — small and worth versioning. |
| `tests/` | Tests for the scripts and the pipeline library. |

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env      # then fill in the AWS block
```

### External prerequisites

Not Python packages; install with your system package manager.

| Tool | Needed for | Install |
|---|---|---|
| AWS CLI v2 | Phase 0 discovery, object fetch | `brew install awscli` |
| `poppler` (`pdftotext`) | PDF text-layer extraction and detection | `brew install poppler` |
| `tesseract` | OCR for scanned PDFs | `brew install tesseract` |
| `jq` | Ad-hoc inspection of inventory JSON | `brew install jq` |

Verify: `make doctor`

## Common tasks

```bash
make doctor        # check prerequisites and AWS identity
make phase0        # run read-only S3 discovery, write listings to data/raw/
make inventory     # analyze the listing, print the numbers Phase 0 must answer
make test          # run tests
```

## Ground rules

- **The source S3 bucket is read-only.** No writes, deletes, lifecycle, or policy
  changes without the project owner's explicit approval (`CLAUDE.md` §9).
- **Exploring is not cleaning.** Nothing in S3 is moved, renamed, or deleted as a side
  effect of discovery.
- **Preserve originals.** The portal serves extracted text but always links to the
  original document. The derived version is never the only copy.
