# Decision Log

One short entry per architectural decision, with the reasoning. This project will be
picked up by someone else eventually (`CLAUDE.md` §9), and the reasoning is the part
that does not survive in the code.

Newest last. Status is `accepted`, `superseded`, `proposed`, or `deferred`.

---

## 0001 — Defer the stack decision until Phase 0 is written up

**Date:** 2026-09-07 · **Status:** accepted (the project owner's call, `CLAUDE.md` §4)

The choice between static generation with a prebuilt index (Astro/11ty + Pagefind,
or a prebuilt Lunr/FlexSearch index) and a hosted search service (Meilisearch,
Typesense, OpenSearch) depends on corpus size and PDF text volume, neither of which
is known yet.

Consequence for this repo: nothing here imports a framework, a search library, or a
hosting config. The scaffolding is stack-neutral on purpose.

---

## 0002 — A normalized JSONL record is the intermediate format

**Date:** 2026-09-07 · **Status:** accepted

One record per document, one JSON object per line, schema at
`docs/schema/document-record.schema.json`, Python mirror at `pipeline/records.py`.

**Why JSONL over Markdown-with-frontmatter:** both were acceptable per `CLAUDE.md`
§5. JSONL streams without loading the corpus into memory, diffs line-by-line, feeds
every candidate search backend directly, and needs no parser beyond `json`.
Markdown-with-frontmatter would be friendlier to hand-edit, but this corpus is
machine-ingested and hand-editing thousands of records is not a workflow we want to
encourage. If the stack decision lands on a static site generator that wants
Markdown files, generating them from JSONL is a small script; going the other way
means writing a frontmatter parser.

**Why this matters:** this layer is what makes decision 0001 reversible. Swapping
search backends means regenerating an index from the same records, not re-ingesting
6,000 documents.

---

## 0003 — Deduplicate on extracted-text hash, not filename or ETag

**Date:** 2026-09-07 · **Status:** accepted (`CLAUDE.md` §5)

`text_sha256` over `canonical_text()` — NFC-normalized text flattened to a single
whitespace-collapsed line — is the dedup key. That is deliberately lossier than the
`normalize_text()` used for the stored `body_text`, which keeps paragraph breaks for
readable search snippets. The same PDF run through `pdftotext` with and without
`-layout` differs in line breaks while being the same document, so the dedup key has
to ignore layout while the display text should not.

ETag catches byte-identical objects and is nearly free, so `scripts/inventory_report.py`
uses it for a first-pass estimate during Phase 0. But the same PDF re-saved by a
different tool, or the same article served from two sites, differs in bytes and
matches in text. Filename is worse still — the corpus is expected to have the same
document under several names across sites.

Duplicates are recorded via `duplicate_of`, not deleted. Two copies of one document
from two sites carry two genuine provenance records, and provenance is a first-class
feature.

---

## 0004 — Stdlib-only pipeline until Phase 0 justifies otherwise

**Date:** 2026-09-07 · **Status:** accepted

`pipeline/` uses only the Python standard library. HTML extraction is
`html.parser`; the resumable ledger is `sqlite3`.

**Why:** "this will be maintained by volunteers, or by nobody, for years. Every
moving part is a liability" (`CLAUDE.md` §4). A stdlib pipeline still runs in five
years without a resolvable lockfile.

**Known cost:** `html.parser` extracts *all* prose, including navigation and
boilerplate, where `trafilatura` or `readability-lxml` would isolate the article
body. That hurts search precision on HTML-heavy content. `html_to_text()` is the
single seam to swap if Phase 0 shows the corpus is mostly HTML rather than mostly
PDF. External binaries (`pdftotext`, `tesseract`) are a separate matter — they are
the standard tools for the job and there is no stdlib substitute.

---

## 0005 — SQLite ledger for idempotent, resumable ingestion

**Date:** 2026-09-07 · **Status:** accepted (`CLAUDE.md` §5)

`pipeline/state.py` tracks per-key status, attempt count, ETag, and text hash in a
single WAL-mode SQLite file under `data/`.

**Why SQLite over an append-only JSONL log:** "have I already processed this key?"
is the hot path on every re-run, and an indexed lookup beats an O(n) scan of a
6,000-line log. WAL mode survives an abrupt kill, which is the failure this is
meant to handle.

Storing the ETag alongside the status is what makes re-ingestion notice *changed*
objects rather than blindly skipping everything it has seen. The ledger is derived
state — delete it and a full re-run rebuilds it — so it is gitignored.

---

## 0006 — Inventory S3 with boto3 and per-bucket listings, not one CLI dump

**Date:** 2026-09-07 · **Status:** accepted

Phase 0 discovery runs through `scripts/s3_list_buckets.py` (resumable, region-aware,
writes `data/raw/inventory/<bucket>.jsonl.gz`) and `scripts/s3_corpus_summary.py`
(offline analysis, writes manifests plus `docs/s3-inventory.md`). `make s3-inventory`
runs both. This supersedes the single-bucket `scripts/phase0_s3_discovery.sh`, which is
kept for spot-checks. `config/buckets.json` is the registry of what exists and what is
in scope.

**Why:** the archive is not one bucket. It is eight buckets in two regions holding
~199,000 objects and 780 GB — `www.clearcut-watch.example` alone is in us-east-2, so any
command without an explicit region silently fails against it. A single `aws s3 ls`
dump could not answer the §3 questions and could not survive an interruption partway
through a 165,000-object listing.

The scripts also exist to keep the corpus out of the conversation. Listings go to disk;
stdout is a digest. Re-running discovery costs a few seconds and no context.

**What we chose against:** S3 Inventory (the AWS service) would be cheaper at this scale
and is worth revisiting if we start re-listing often, but it needs a bucket policy write
to configure, and CLAUDE.md §9 forbids that without the project owner's approval.

---

## 0007 — Filter filesystem noise before counting anything

**Date:** 2026-09-07 · **Status:** accepted

`is_noise()` in `scripts/s3_corpus_summary.py` drops zero-byte directory markers,
AppleDouble `._` sidecars, `.DS_Store`, `__MACOSX/`, and Apple package-bundle internals
(`.pages/`, `.key/`) from every figure. They are reported as their own line, not silently
discarded: 29,341 objects, 614 MB.

**Why:** the corpus was synced from macOS folders, so roughly 15% of all keys are
filesystem artifacts. Left in, they corrupt the numbers that the architecture decision
depends on. Worst case was duplication: all 18,145 empty directory markers share one MD5,
so the first run reported a single 18,145-member "identical content" group and 69,351
redundant objects. Filtered, the real figure is 47,736 — still 28% of the corpus, which
is the number that actually justifies content-hash dedup.

---

## 0008 — The buckets are whole-server backups; exclude CMS source from ingestion

**Date:** 2026-09-07 · **Status:** accepted

`config/buckets.json` carries an `exclude_prefixes` map. Anything under it is inventoried
but never ingested. It covers the FTP-backup trees in four buckets and the WordPress
install in `www.timberland-network.example`.

**Why:** the buckets are not content exports. They are backups of entire web servers,
so alongside the published documents they hold the CMS itself: admin pages, legacy
rich-text editor trees (2,623 objects across four buckets), plugins, and server logs.
None of that is archival material, and indexing it would bury real documents under
thousands of code files. Excluding by prefix keeps the inventory complete while keeping
ingestion clean.

The `.eml` files are excluded under the same mechanism for a different reason:
~29,400 pieces of personal correspondence need the author's consent before anything is
indexed. That is a rights question, not a data-quality one, so it stays an open question
rather than a permanent exclusion.

---

## 0009 — Measure the OCR requirement by sampling, and weight the estimate properly

**Date:** 2026-09-07 · **Status:** accepted

`scripts/sample_pdfs.py` draws a stratified random sample of PDFs, measures each one's
text layer with `pdfinfo`/`pdftotext`, and estimates the corpus-wide OCR requirement.
Findings in `docs/pdf-corpus-assessment.md`. Result: **18.9% of PDFs need OCR**
(95% CI 17.0-20.8%, n=1,532), about 317,000 pages, ~$476 of Textract or ~176 Tesseract
core-hours.

**Why sample rather than guess:** at ~51,000 PDFs the text-layer ratio drives both the
OCR budget and the architecture. It was cheap to measure — the full sample runs in about
a minute — and the guess would have been wrong in both directions at once.

**Two estimator choices worth preserving.** Sampling is stratified by file size with a
per-stratum floor, so rare large-file bands are represented; every corpus-wide figure is
therefore weighted by `N_stratum / n_stratum`. And page counts are summed per sampled
document rather than derived as `population x share x mean pages` — documents needing OCR
run shorter than text-native ones (38.9 pages vs 52.4), and the naive product overstated
the OCR page count by 50%. Both are covered by `tests/test_sample_pdfs.py`.

**What we chose against:** a full census. Reading every PDF would cost real egress and
hours, to refine a number whose confidence interval is already under four points wide.

---

## 0010 — Build against the core sites first; treat forest-library.example as phase two

**Date:** 2026-09-07 · **Status:** proposed, pending the project owner

Phase 1 ingests canopycoalition.example, forest-stewards.example, woodlands-council.example, and clearcut-watch.example:
2,051 PDFs, ~40,000 pages, ~$18 of OCR. Phase 2 decides separately what to do with
forest-library.example's 49,078 PDFs and ~1.87 million pages.

**Why:** the corpus is not one thing. The core sites are the material CLAUDE.md §1
describes and are small enough to ingest completely and prove the pipeline against.
forest-library.example is 46x larger and, at ~1.9 million pages of extracted text, rules out
the simple prebuilt-static-index architecture that best fits a project maintained by
volunteers or by nobody. Splitting the phases lets phase 1 use that architecture and
defers the harder tradeoff until we know what the bulk archive is actually worth.

The core sites also need *more* OCR (26.5% vs 18.5%) and are the ones at genuine archival
risk — woodlands-council.example is already sunset and the others are aging and unmaintained. They are both the
higher value and the higher urgency.

**Cost of this choice:** the portal does not cover everything on first launch, which is
in tension with the §1 success criterion of finding material without knowing which site
it came from. Mitigated by making phase 2 a scope decision rather than a rebuild: the
normalized intermediate format (§5) is the same either way.

---

## Template for new entries

```
## NNNN — <decision in one line>

**Date:** YYYY-MM-DD · **Status:** accepted

<What we decided.>

**Why:** <reasoning, including what we chose against and the cost of this choice.>
```
