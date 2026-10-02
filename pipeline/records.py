"""The normalized document record — one record per document.

Schema of record: docs/schema/document-record.schema.json. This module is the
Python-side mirror of it, plus the deterministic ID and checksum helpers that make
ingestion idempotent (CLAUDE.md §5).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = "1"

CONTENT_TYPE_BY_EXTENSION = {
    ".pdf": "pdf",
    ".htm": "html",
    ".html": "html",
    ".shtml": "html",
    ".cfm": "html",
    ".asp": "html",
    ".php": "html",
    ".txt": "text",
    ".md": "text",
    ".rtf": "text",
    ".doc": "office",
    ".docx": "office",
    ".xls": "office",
    ".xlsx": "office",
    ".ppt": "office",
    ".pptx": "office",
    ".jpg": "image",
    ".jpeg": "image",
    ".png": "image",
    ".gif": "image",
    ".tif": "image",
    ".tiff": "image",
    ".bmp": "image",
    ".mp4": "video",
    ".mov": "video",
    ".avi": "video",
    ".wmv": "video",
    ".mp3": "audio",
    ".wav": "audio",
}


def content_type_for(key_or_name: str) -> str:
    """Classify by extension. Returns 'other' for anything unrecognized."""
    suffix = Path(key_or_name).suffix.lower()
    return CONTENT_TYPE_BY_EXTENSION.get(suffix, "other")


def normalize_text(text: str) -> str:
    """Clean up extracted text for storage and display.

    NFC-normalize, collapse horizontal whitespace and non-breaking spaces, trim each
    line, and reduce runs of blank lines to one. Paragraph breaks survive, because
    the stored body_text is what a reader sees in a search snippet.
    """
    text = unicodedata.normalize("NFC", text)
    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return "\n".join(line.strip() for line in text.split("\n")).strip()


def canonical_text(text: str) -> str:
    """Flatten text to one whitespace-normalized line. For hashing only.

    Deliberately lossier than normalize_text. Two copies of the same document
    extracted by different tools — or the same PDF run through `pdftotext` with and
    without `-layout` — differ in line breaks and indentation while being the same
    document. Since dedup is on content, not filename (CLAUDE.md §5), the key has to
    ignore layout. Word boundaries are preserved, so genuinely different documents
    do not collide.
    """
    text = unicodedata.normalize("NFC", text).replace("\u00a0", " ")
    return re.sub(r"\s+", " ", text).strip()


def text_checksum(text: str) -> str:
    """SHA-256 of canonicalized text. The deduplication key (CLAUDE.md §5)."""
    return hashlib.sha256(canonical_text(text).encode("utf-8")).hexdigest()


def file_checksum(path: str | Path, chunk_size: int = 1 << 20) -> str:
    """SHA-256 of raw bytes on disk. Integrity, not dedup."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_id(source_site: str, locator: str) -> str:
    """Deterministic record ID: '<site>-<12 hex>'.

    The locator should be the original URL when we have one, else the S3 key.
    Same inputs always give the same ID, which is what lets ingestion re-run
    without duplicating records. Twelve hex characters is ~48 bits — ample for a
    corpus in the thousands, and short enough to paste into a bug report.
    """
    if not source_site:
        raise ValueError("source_site is required for a stable ID")
    if not locator:
        raise ValueError("locator (original_url or s3_key) is required for a stable ID")
    slug = re.sub(r"[^a-z0-9_-]+", "-", source_site.lower()).strip("-")
    digest = hashlib.sha256(f"{slug}\x00{locator.strip()}".encode("utf-8")).hexdigest()
    return f"{slug}-{digest[:12]}"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Provenance:
    """Where a document came from. Shown on every search result (CLAUDE.md §4)."""

    s3_key: str
    original_url: str | None = None
    s3_bucket: str | None = None
    s3_etag: str | None = None
    byte_size: int | None = None
    file_sha256: str | None = None
    retrieved_from: str | None = None


@dataclass
class Extraction:
    """How the text was obtained and how much to trust it."""

    status: str = "ok"
    method: str | None = None
    char_count: int | None = None
    ocr_confidence: float | None = None
    error: str | None = None
    tool_version: str | None = None


@dataclass
class DocumentRecord:
    title: str
    source_site: str
    content_type: str
    provenance: Provenance
    body_text: str = ""
    id: str = ""
    title_inferred: bool = False
    mime_type: str | None = None
    publication_date: str | None = None
    publication_date_precision: str | None = None
    publication_date_source: str | None = None
    capture_date: str = field(default_factory=utc_now)
    text_sha256: str = ""
    duplicate_of: str | None = None
    extraction: Extraction = field(default_factory=Extraction)
    topics: list[str] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if not self.id:
            locator = self.provenance.original_url or self.provenance.s3_key
            self.id = stable_id(self.source_site, locator)
        if not self.text_sha256:
            self.text_sha256 = text_checksum(self.body_text)
        if self.extraction.char_count is None:
            self.extraction.char_count = len(self.body_text)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        """One record, one line. JSONL keeps the corpus streamable and diffable."""
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> DocumentRecord:
        payload = dict(data)
        payload["provenance"] = Provenance(**payload.get("provenance", {}))
        payload["extraction"] = Extraction(**payload.get("extraction", {}))
        return cls(**payload)


def write_jsonl(records: list[DocumentRecord], path: str | Path) -> int:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        for record in records:
            handle.write(record.to_json() + "\n")
    return len(records)


def read_jsonl(path: str | Path) -> Iterator[DocumentRecord]:
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                yield DocumentRecord.from_dict(json.loads(line))
