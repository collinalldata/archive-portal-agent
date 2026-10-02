# PDF Corpus Assessment — How Much of This Needs OCR

**Date:** 2026-09-07
**Method:** `scripts/sample_pdfs.py`, stratified random sample, seed 20260907
**Raw results:** `data/manifests/pdf-sample.json`, `data/manifests/pdf-sample-small-buckets.json`

Phase 0 question 2 asks what fraction of the corpus is text-extractable versus scanned.
At ~51,000 ingestable PDFs the answer decides real money and a real architecture choice,
so it was worth measuring rather than guessing.

## Headline

**About one PDF in five needs OCR. Both OCR routes are affordable.** The cost question
turns out not to be the constraint — the constraint is that the corpus splits cleanly
into a small high-value core and a very large secondary archive.

| | Core sites | forest-library.example | Total |
|---|---:|---:|---:|
| Ingestable PDFs | 2,051 | 49,078 | 51,129 |
| Estimated pages | 40,348 | ~1,870,000 | ~1,910,613 |
| Share needing OCR | 26.5% | 18.5% | 18.9% |
| Pages needing OCR | ~11,754 | ~305,000 | ~317,005 |
| Textract cost | **~$18** | ~$458 | ~$476 |
| Tesseract time | ~7 core-hours | ~169 core-hours | ~176 core-hours |

"Core sites" means canopycoalition.example, forest-stewards.example, woodlands-council.example, and
clearcut-watch.example — the buckets that map to the advocacy sites the portal is actually
about. 706 of their 2,051 PDFs were sampled, so those numbers are close to a census.

## Method

A stratified random sample across five size bands, because file size is the best single
predictor of whether a PDF is a scan. Each sampled file was downloaded to a temp
directory, measured with `pdfinfo` and `pdftotext`, and deleted immediately. Nothing was
cached and nothing was written to S3.

Classification is by characters of extracted text per page, whitespace collapsed:

- **`text_layer`** — 250+ chars/page. Real, searchable text.
- **`sparse_text`** — 50-250 chars/page. Usually a scan carrying only a header, a stamp,
  or a failed auto-OCR pass. Counted as needing OCR.
- **`needs_ocr`** — under 50 chars/page. No usable text layer.

Text is measured over at most 30 pages per document. A 600-page report does not need
full extraction to reveal whether it has a text layer.

Two estimator details matter, because getting them wrong changes the answer materially:

**Weight by stratum.** Proportional allocation with a floor deliberately oversamples the
rare large-file bands, so every corpus-wide figure is weighted back by
`N_stratum / n_stratum`. Unweighted, the large bands would drag the OCR share upward.

**Sum pages per document; do not multiply by a mean.** Documents needing OCR run
*shorter* than text-native ones — 38.9 pages against 52.4. The first version of this
script estimated OCR pages as `population x OCR share x mean pages`, which overstated
the result by 50% (479,000 pages against the correct 317,000). Page counts are now summed
per sampled document and scaled by stratum weight.

## Results

Main sample, n=1,532 of 51,129 PDFs. **18.9% need OCR (95% CI 17.0%-20.8%).**

| Verdict | Sampled | Weighted share of corpus |
|---|---:|---:|
| `text_layer` | 1,228 | 80.8% |
| `needs_ocr` | 214 | 14.0% |
| `sparse_text` | 76 | 4.9% |
| `skipped_too_large` (>200 MB) | 12 | 0.2% |
| `corrupt` | 2 | 0.1% |

### OCR need rises with file size

| Band | Sampled | Needs OCR | Population |
|---|---:|---:|---:|
| tiny (<100 KB) | 490 | 9.4% | 16,701 |
| small (100 KB-1 MB) | 566 | 18.2% | 19,294 |
| medium (1-10 MB) | 348 | 27.0% | 11,855 |
| large (10-100 MB) | 88 | 44.3% | 2,987 |
| huge (>100 MB) | 40 | 20.0% | 292 |

Monotonic up to the `large` band, which is what you would expect: scanned pages are
images and images are big. The `huge` band breaks the pattern because it is mostly
born-digital material with embedded photography, not scans.

### Core sites need more OCR than the bulk archive

Targeted sample, n=706 of 2,051. **26.5% need OCR (95% CI 23.5%-29.5%).**

| Bucket | Sampled | Needs OCR |
|---|---:|---:|
| `www.forest-stewards.example` | 17 | 64.7% |
| `www.woodlands-council.example` | 64 | 43.8% |
| `www.clearcut-watch.example` | 48 | 31.2% |
| `www.canopycoalition.example` | 577 | 23.6% |
| `www.forest-library.example` | 1,466 | 18.5% |

The oldest and most endangered material is the most scanned, which is not a coincidence
— it predates born-digital publishing. forest-stewards.example's 64.7% rests on only 17 files
and should be read as "high, roughly two thirds" rather than a precise figure.

### Integrity

Two of 1,532 PDFs failed to parse (~66 corpus-wide, 0.13%) — one truncated annual report,
one with a missing trailer dictionary. Twelve files exceeded the 200 MB sampling cap and
were not measured. Nothing suggested systematic corruption. Producer strings are ordinary
(Adobe PDF Library, Acrobat Distiller, Ghostscript, Quartz), with no sign of tampering.

## What this implies

**OCR is not the expensive part, so do not design around it.** $476 of Textract, or about
11 hours on 16 cores of free Tesseract, buys the entire corpus. Either is affordable.
Tesseract fits the project's zero-budget constraint better and has no vendor dependency;
Textract is worth it only if quality on the worst scans proves materially better, which
is a decision to make on a sample of the hard cases, not up front.

**The real fork is scope, not technology.** The core sites are 2,051 PDFs and ~40,000
pages: small enough to ingest completely, OCR included, for about $18 and an afternoon.
forest-library.example is 49,000 PDFs and ~1.87 million pages — a different kind of project,
and 46 times larger.

That argues for building the portal against the core sites first, proving the pipeline
end to end, and treating forest-library.example as a second phase with its own decision about
whether the whole thing belongs in a public search index or only a curated subset does.

**A prebuilt static index is out for the full corpus.** ~1.9 million pages of extracted
text is far beyond what Pagefind or a shipped Lunr index can serve to a browser. It is
comfortably within range for the core sites alone, which is another argument for the
phased split: phase one can use the simple, neglectable architecture the project wants,
and phase two can be evaluated on its own terms once we know what the material is worth.

## Reproducing

```bash
make pdf-sample                      # main stratified sample
python3 scripts/sample_pdfs.py --only www.canopycoalition.example --sample 200
```

The seed is fixed, so a re-run against an unchanged corpus draws the same sample.
