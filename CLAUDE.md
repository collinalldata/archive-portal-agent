# Forest Archive Portal — Project Brief

> **How to use this file:** Drop it in the repo root as `CLAUDE.md` and start a Claude Code
> session. Read it fully before writing code. Sections marked **[NEEDS INPUT]** must be
> resolved with the project owner or by discovery before the work that depends on them begins.

---

## 1. What we're building

A single searchable portal that consolidates environmental advocacy material for the
forested region of the southeastern United States — blog posts, articles, resource pages, and primary source PDFs —
currently scattered across six-plus aging, independently maintained websites.

The material is largely the life's work of a small number of advocates (principally the
organization's founder) documenting old-growth and bottomland hardwood protection, industrial
logging and wood-pellet export, volunteer forest stewardship, and regional development fights. Several of the source sites are unmaintained
or effectively sunset. **The archival risk is real and current** — this project
is as much preservation as it is search.

### Success looks like

A person researching, say, the Cypress Bend timber sale or pellet-mill permits near Oak Hollow can find
every relevant document across all source sites in one search, see where each came from and
when it was published, and open the original PDF — without needing to know that
forest-stewards.example and woodlands-council.example ever existed.

---

## 2. Current state — what already exists

**[NEEDS INPUT] There is an existing S3 archive.** The project owner has indicated the starting point is
existing content already in S3, not a from-scratch scrape. Nothing in the project notes
describes it yet. Before designing anything, complete the Phase 0 discovery below and write
your findings to `docs/s3-inventory.md`.

Fill in whatever is known before the first session:

```
AWS profile / credentials:   [NEEDS INPUT]
Region:                      [NEEDS INPUT]
Bucket name(s):              [NEEDS INPUT]
Who owns / pays for it:      [NEEDS INPUT]
Is it a static-site bucket, a raw dump, or a crawl output?  [NEEDS INPUT]
```

---

## 3. Phase 0 — S3 discovery (do this first)

**Goal:** understand the shape of the corpus before choosing any architecture. Do not pick a
framework, a search engine, or a hosting model until this is written up.

Use the AWS CLI. Read-only commands only in this phase — no writes, no deletes, no lifecycle
or policy changes without asking the project owner first.

Work through roughly this sequence, adapting as you learn:

```bash
aws sts get-caller-identity                      # confirm which account/identity we're on
aws s3 ls                                        # enumerate buckets
aws s3api get-bucket-location --bucket <BUCKET>
aws s3 ls s3://<BUCKET>/ --recursive --summarize # object count + total size
```

Then inventory properly rather than eyeballing. For anything over a few thousand objects,
prefer a paginated listing dumped to disk so it can be analyzed offline:

```bash
aws s3api list-objects-v2 --bucket <BUCKET> \
  --query 'Contents[].{Key:Key,Size:Size,Modified:LastModified,Class:StorageClass}' \
  --output json > data/raw/s3-inventory.json
```

If the bucket is large or Glacier-tiered, consider S3 Inventory or `s3api list-objects-v2`
with `--prefix` sweeps rather than one giant listing.

### Questions Phase 0 must answer

1. **Volume.** How many objects, total bytes, and what's the size distribution? Is this
   thousands of small HTML files or hundreds of large scanned PDFs?
2. **Composition.** Breakdown by extension — PDF, HTML, images, office docs, video. What
   fraction is text-extractable vs. scanned images needing OCR?
3. **Provenance structure.** Do key prefixes encode which source site each object came from?
   Is there a date structure? Is there any existing manifest or metadata file?
4. **Duplication.** How much overlap exists — the same PDF archived from multiple sites, or
   multiple crawl generations of the same page? Check by size+name collisions first, then
   ETag/MD5 for candidates.
5. **Completeness.** Does the S3 content cover all six known sites, or only some? Which sites
   still need to be scraped fresh?
6. **Access model.** Is the bucket public, private, or fronted by CloudFront? Are objects
   already web-addressable, and is versioning or lifecycle expiration configured?
7. **Integrity.** Spot-check several PDFs — do they open, do they contain real text, are any
   corrupt or truncated?

**Deliverable:** `docs/s3-inventory.md` with the numbers, the prefix structure, a
representative sample of keys, and an explicit list of gaps. Plus `data/raw/s3-inventory.json`
committed or gitignored per size.

---

## 4. Architecture — deliberately undecided

The project owner's call: **the stack decision is deferred to Claude Code after Phase 0.** Do not assume
one now. Propose a recommendation with tradeoffs once you know the corpus size, format mix,
and update cadence.

Constraints that should drive the choice:

- **This will be maintained by volunteers, or by nobody, for years.** Every moving part is a
  liability. Several source sites have already gone more than a decade without meaningful
  maintenance. Favor architectures that can be neglected safely.
- **The corpus is archival.** It changes rarely. Rebuild-on-change is acceptable; live
  database writes are probably unnecessary.
- **Cost should trend toward zero.** Assume no reliable ongoing budget.
- **Documents, not just pages.** Full-text search must reach inside PDFs, which is the main
  argument for a real index rather than naive client-side search.
- **Provenance is a first-class feature,** not a footnote. Every result shows source site,
  original URL, publication date where known, and capture date.

Broadly, the fork is: static generation with a prebuilt search index (Astro/11ty + Pagefind
or a prebuilt Lunr/FlexSearch index, hosted on Cloudflare Pages or S3+CloudFront) versus a
hosted search service (Meilisearch/Typesense/OpenSearch) behind a thin app. Corpus size and
PDF text volume decide it. Say which you'd pick and why, and let the project owner confirm before
building.

---

## 5. Content pipeline requirements

Whatever the stack, the ingestion pipeline needs these properties:

**Extract text, never mirror HTML.** Pull clean text and linked documents only. Old CMS
templates carry navigation, scripts, and markup that add nothing to search and age badly.

**Normalize into a stable intermediate format.** One record per document, with at minimum:
stable ID, title, body text, source site, original URL, publication date, capture date,
content type, S3 key, and a checksum. JSON or Markdown-with-frontmatter both work. This
intermediate layer is what makes the stack decision reversible.

**OCR where needed.** Scanned PDFs are likely in a corpus this old. Detect text-layer absence
and route those through OCR (Tesseract or AWS Textract — cost check first if Textract).

**Deduplicate on content, not filename.** The same PDF is likely archived under several names
across sites. Hash the extracted text.

**Preserve the originals.** The portal serves extracted text for search but always links to
the original document. Never let the derived version become the only copy.

**Be idempotent and resumable.** Re-running ingestion should not duplicate records, and a
failure at document 4,000 of 6,000 should not restart from zero.

---

## 6. Source sites — status as of 2026-08-30

| Site | Reachable via | Role |
|---|---|---|
| canopycoalition.example | WebFetch or browser | Canopy Coalition. **Current, active.** Hosted on a commercial site builder. |
| canopy-archive.example | Browser only (https→http redirect loop) | **Archive of the original Canopy Coalition site.** Static HTML, distinct historical news feed. High archival value. |
| field-notes.example | Browser only (TLS cert error) | Active, the founder. Hosts combined legacy PDFs. |
| forest-stewards.example | WebFetch or browser | Regional Forest Stewards Association. |
| clearcut-watch.example | WebFetch or browser | Industrial logging and wood-pellet export advocacy. Active. WordPress. |
| woodlands-council.example | Browser only (TLS cert error) | Regional Woodlands Council. Now a single stub page pointing at legacy PDFs. Effectively sunset. |

### Fetching sites that refuse to load

WebFetch declines any site where it can't verify crawl permission over HTTPS — which breaks
on broken/self-signed certs and on plain-HTTP-only domains. This is common on old nonprofit
sites. **Use a browser tool instead** before concluding a site is dead: navigate to the URL,
request site-scoped access if prompted, then pull page text and inspect the DOM for link
structure.

### Not yet reviewed, possibly in scope

`field-notes-org.example` (distinct from the .com), `forest-library.example` (hosts PDFs referenced by
canopycoalition.example), `devwatch.example`. **[NEEDS INPUT]** The project owner has referred to a seventh
site that hasn't been named — likely one of these.

---

## 7. Scope boundaries

This project preserves and indexes material. It does not maintain, repair, or redesign
the source sites, even where they are clearly aging. Those are real and worthwhile
problems, and they belong to the site owners.

- Treat S3 as the system of record for anything already backed up there; it is often the
  only surviving copy.
- The legacy combined PDFs some sites link to are **unverified**. Nobody has confirmed they
  open or that their download links still resolve. Verify before depending on them, and
  prefer the original documents where both exist.

---

## 8. Open questions for the project owner

1. **[Blocking Phase 0]** S3 details — account, profile, region, bucket(s). What's in there
   and who put it there?
2. What is the seventh site?
3. For Woodlands Council and RFSA material: pull from the combined legacy PDFs, or go back to
   the S3 backups of the original sites?
4. Does the portal need to stay current with canopycoalition.example and clearcut-watch.example as they
   publish new material, or is this a fixed-point archive?
5. Any rights or permissions questions about republishing this material?

---

## 9. Working conventions

- Keep a running `docs/decisions.md` — one short entry per architectural decision with the
  reasoning. This project will be picked up by someone else eventually.
- Raw scrape and S3 output goes in `data/raw/`, normalized records in `data/processed/`.
  Gitignore anything large; commit manifests and checksums.
- Never write to the source S3 bucket without explicit approval.
- Don't clean up, restructure, or delete anything in S3 as a side effect of exploring it.
- Flag scope creep (§7).

---

## 10. Suggested first session

1. Read this file.
2. Ask the project owner for the S3 credentials block in §2 if it's still empty.
3. Run Phase 0 discovery. Write `docs/s3-inventory.md`.
4. Come back with: corpus numbers, a recommended architecture with the tradeoff reasoning,
   and a proposed Phase 1 scope. Get confirmation before building.
