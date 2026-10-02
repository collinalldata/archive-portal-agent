# What was changed for the public version

This repo is an anonymized copy of a private working repository. The aim was to keep the
agent's reasoning, code, and aggregate findings intact while removing anything that could
identify the real organization, its people, or its infrastructure.

## The setting is fictional

The organization, its region, and its subject matter are invented. The real project is not
a forestry nonprofit and is not in the southeastern United States. Every organization name,
domain, place, and topic was replaced to fit the fictional setting, so nothing here points
back to the real one. All domains use the reserved `.example` TLD (RFC 2606) and do not
resolve.

## Removed or replaced

**Git history.** Not carried over. This repo starts from a single fresh commit.

**People.** Referred to by role only: the founder, the project owner.

**AWS.** The account ID is replaced with `000000000000` and the IAM identity with
`project-owner`. Bucket names follow the fictional domains, and regions are normalized.

**S3 object paths.** Every object key in `data/manifests/` is replaced with a stable hash
(`redacted/<sha256-prefix>.<ext>`). Per-bucket prefix trees are dropped from the
manifests, and the prefix section of `docs/s3-inventory.md` is a structural summary rather
than a list of real folder names.

**PDF producer strings.** Collapsed to the tool family (Ghostscript, Acrobat Distiller,
and so on), because some named the institution that scanned the document.

**Scope.** This copy focuses on the S3 assessment. Material about the live source websites
that the assessment didn't depend on was removed.

## Kept as-is

All S3 discovery and analysis code, its tests, and the decision reasoning. All aggregate
numbers: object counts, byte totals, extension mixes, duplication rates, OCR sampling
results, and cost estimates. The agent brief (`CLAUDE.md`) is trimmed and recast to match.

## Not included

Raw S3 listings and document content were never committed to begin with.
