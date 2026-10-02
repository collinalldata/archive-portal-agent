# Forest Archive Portal — task runner.
# Deliberately thin: these are the commands you'd type anyway, named so they're
# discoverable. See README.md and docs/phase-0-checklist.md.

PYTHON ?= python3
INVENTORY ?= data/raw/s3-inventory.json
SUMMARY ?= data/manifests/inventory-summary.json

.PHONY: help doctor deps phase0 s3-list s3-summary s3-inventory inventory test lint clean-derived

help:
	@echo "doctor          Check prerequisites and AWS identity"
	@echo "deps            Install Python dependencies (boto3, pytest)"
	@echo "s3-list         List every in-scope bucket to data/raw/inventory/ (resumable)"
	@echo "s3-summary      Analyze the listings; write manifests and docs/s3-inventory.md"
	@echo "s3-inventory    s3-list then s3-summary — the whole Phase 0 count"
	@echo "pdf-sample      Sample PDFs to measure how much of the corpus needs OCR"
	@echo "phase0          Single-bucket discovery via the AWS CLI (legacy, needs ARCHIVE_BUCKET)"
	@echo "inventory       Analyze one listing file and write $(SUMMARY)"
	@echo "test            Run the pipeline tests"
	@echo "lint            Compile-check all Python and shell scripts"
	@echo "clean-derived   Delete the local ingest ledger and processed records"

doctor:
	@echo "== Prerequisites =="
	@for tool in aws pdftotext pdfinfo tesseract jq $(PYTHON); do \
	  if command -v $$tool >/dev/null 2>&1; then \
	    printf '  ok      %s\n' "$$tool"; \
	  else \
	    printf '  MISSING %s\n' "$$tool"; \
	  fi; \
	done
	@echo "== .env =="
	@if [ -f .env ]; then echo "  ok      .env present"; else echo "  MISSING .env (cp .env.example .env)"; fi
	@echo "== AWS identity =="
	@aws sts get-caller-identity 2>/dev/null || echo "  no valid AWS credentials — this blocks Phase 0"

deps:
	$(PYTHON) -m pip install -r requirements.txt

# The corpus is ~250k objects across two regions, so listing is resumable and writes
# to disk. Rerun after an interruption and it picks up from the last checkpoint.
s3-list:
	$(PYTHON) scripts/s3_list_buckets.py

s3-summary:
	$(PYTHON) scripts/s3_corpus_summary.py --markdown docs/s3-inventory.md

s3-inventory: s3-list s3-summary

# Downloads a stratified sample, measures the text layer, deletes each file. Needs
# poppler-utils (pdftotext, pdfinfo) — `make doctor` reports whether they are present.
pdf-sample:
	$(PYTHON) scripts/sample_pdfs.py --sample 1500 --min-per-stratum 40 --max-mb 200

phase0:
	./scripts/phase0_s3_discovery.sh

inventory:
	$(PYTHON) scripts/inventory_report.py --input $(INVENTORY) --json $(SUMMARY)

test:
	$(PYTHON) -m pytest tests/ -q

lint:
	$(PYTHON) -m compileall -q pipeline scripts
	@for script in scripts/*.sh; do bash -n "$$script" && echo "  ok  $$script"; done

# Removes derived state only. Never touches data/originals/ or anything in S3.
clean-derived:
	rm -f data/ingest-ledger.sqlite3 data/ingest-ledger.sqlite3-wal data/ingest-ledger.sqlite3-shm
	find data/processed -type f ! -name '.gitkeep' -delete
	@echo "Derived state cleared. Originals and S3 untouched."
