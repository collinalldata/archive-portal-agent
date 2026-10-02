"""Tests for the normalized record format.

The properties under test are the ones CLAUDE.md §5 requires: stable IDs so
ingestion is idempotent, and content-based checksums so dedup works on content
rather than filename.
"""

import json
from pathlib import Path

import pytest

from pipeline.records import (
    DocumentRecord,
    Provenance,
    canonical_text,
    content_type_for,
    normalize_text,
    read_jsonl,
    stable_id,
    text_checksum,
    write_jsonl,
)

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "docs/schema/document-record.schema.json"


def make_record(**overrides) -> DocumentRecord:
    defaults = dict(
        title="Cypress Bend Environmental Assessment",
        source_site="forest_stewards",
        content_type="pdf",
        body_text="The proposed development sits on the floodplain.",
        provenance=Provenance(s3_key="forest-stewards.example/docs/cypress-bend-ea.pdf"),
    )
    defaults.update(overrides)
    return DocumentRecord(**defaults)


class TestStableId:
    def test_is_deterministic(self):
        assert stable_id("forest_stewards", "a/b.pdf") == stable_id("forest_stewards", "a/b.pdf")

    def test_differs_by_site(self):
        assert stable_id("forest_stewards", "a/b.pdf") != stable_id("canopy_coalition", "a/b.pdf")

    def test_differs_by_locator(self):
        assert stable_id("forest_stewards", "a/b.pdf") != stable_id("forest_stewards", "a/c.pdf")

    def test_matches_schema_pattern(self):
        assert stable_id("field_notes", "x").startswith("field_notes-")

    def test_ignores_surrounding_whitespace_in_locator(self):
        assert stable_id("rg", " a/b.pdf ") == stable_id("rg", "a/b.pdf")

    @pytest.mark.parametrize("site,locator", [("", "k"), ("rg", "")])
    def test_rejects_empty_inputs(self, site, locator):
        with pytest.raises(ValueError):
            stable_id(site, locator)


class TestChecksums:
    def test_layout_variants_hash_alike(self):
        # pdftotext with and without -layout produce the same document with different
        # line breaks and indentation. They must dedup.
        plain = "Cypress Bend Environmental Assessment\nSection 1. Introduction"
        layout = "Cypress Bend    Environmental      Assessment\n\n\n   Section 1.  Introduction  "
        assert text_checksum(plain) == text_checksum(layout)

    def test_paragraph_structure_survives_in_stored_text(self):
        # normalize_text is for display and keeps paragraphs; canonical_text is for
        # hashing and does not. Do not conflate them.
        assert normalize_text("a\n\n\n\nb") == "a\n\nb"
        assert canonical_text("a\n\n\n\nb") == "a b"

    def test_different_content_differs(self):
        assert text_checksum("Cypress Bend") != text_checksum("Pine Ridge")

    def test_nbsp_is_normalized(self):
        assert normalize_text("a b") == "a b"

    def test_unicode_composition_is_normalized(self):
        # NFD "é" and NFC "é" are the same document.
        assert text_checksum("Cataract Cañon") == text_checksum("Cataract Cañon")


class TestContentTypeClassification:
    @pytest.mark.parametrize("name,expected", [
        ("a/b.PDF", "pdf"),
        ("index.html", "html"),
        ("legacy/page.cfm", "html"),   # legacy CMS pages
        ("scan.TIFF", "image"),
        ("minutes.doc", "office"),
        ("clip.mov", "video"),
        ("no-extension", "other"),
        ("archive.xyz", "other"),
    ])
    def test_classifies(self, name, expected):
        assert content_type_for(name) == expected


class TestDocumentRecord:
    def test_derives_id_and_checksum(self):
        record = make_record()
        assert record.id.startswith("forest_stewards-")
        assert len(record.text_sha256) == 64
        assert record.extraction.char_count == len(record.body_text)

    def test_prefers_original_url_for_id(self):
        with_url = make_record(provenance=Provenance(
            s3_key="k", original_url="https://forest-stewards.example/cypress-bend"))
        assert with_url.id == stable_id("forest_stewards", "https://forest-stewards.example/cypress-bend")

    def test_reingesting_the_same_object_yields_the_same_id(self):
        assert make_record().id == make_record().id

    def test_roundtrips_through_json(self):
        original = make_record()
        restored = DocumentRecord.from_dict(json.loads(original.to_json()))
        assert restored.to_dict() == original.to_dict()

    def test_jsonl_roundtrip(self, tmp_path):
        records = [make_record(), make_record(provenance=Provenance(s3_key="other.pdf"))]
        path = tmp_path / "records.jsonl"
        assert write_jsonl(records, path) == 2
        assert [r.id for r in read_jsonl(path)] == [r.id for r in records]

    def test_capture_date_is_always_set(self):
        assert make_record().capture_date


class TestSchemaAgreement:
    """The Python dataclasses and the JSON Schema must not drift apart."""

    @pytest.fixture
    def schema(self):
        return json.loads(SCHEMA_PATH.read_text())

    def test_record_keys_match_schema_properties(self, schema):
        assert set(make_record().to_dict()) == set(schema["properties"])

    def test_required_fields_are_populated(self, schema):
        record = make_record().to_dict()
        for field in schema["required"]:
            assert record.get(field) not in (None, ""), f"{field} is required but empty"

    def test_id_matches_schema_pattern(self, schema):
        import re
        assert re.match(schema["properties"]["id"]["pattern"], make_record().id)

    def test_content_type_is_in_schema_enum(self, schema):
        allowed = schema["properties"]["content_type"]["enum"]
        for value in set(__import__("pipeline.records", fromlist=["x"]).CONTENT_TYPE_BY_EXTENSION.values()):
            assert value in allowed
