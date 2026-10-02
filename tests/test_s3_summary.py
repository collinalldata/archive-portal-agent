"""Tests for the corpus summarizer's classification rules.

The noise filter is the load-bearing part: this corpus was synced from macOS
folders, and without the filter the 18,145 zero-byte directory markers all share
one ETag and dominate the duplication report.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from s3_corpus_summary import classify, cross_bucket_duplicates, is_noise  # noqa: E402


class TestIsNoise:
    def test_directory_marker(self):
        assert is_noise("Resources/History/")

    def test_appledouble_sidecar(self):
        assert is_noise("paperwork/Council/._Meeting_Outline.doc")

    def test_ds_store(self):
        assert is_noise("Resources/.DS_Store")

    def test_macosx_folder(self):
        assert is_noise("__MACOSX/Resources/._thing.pdf")

    def test_apple_bundle_internals(self):
        assert is_noise("ThankYou/KiraKilmer.pages/Contents/PkgInfo")
        assert is_noise("Publications/Deck.key/Data/slide1.jpg")

    def test_real_documents_are_not_noise(self):
        assert not is_noise("Resources/Legal/2013-clearcut-permit-ruling.pdf")
        assert not is_noise("Resources/History/notes.docx")

    def test_bundle_itself_is_not_noise(self):
        # The .pages bundle as a single object is content; only its internals are not.
        assert not is_noise("ThankYou/KiraKilmer.pages")


class TestClassify:
    def test_documents(self):
        assert classify(".pdf") == "document"
        assert classify(".docx") == "document"

    def test_media_and_web(self):
        assert classify(".jpg") == "media"
        assert classify(".php") == "web"

    def test_unknown_extension(self):
        assert classify(".xyz") == "other"

    def test_key_overrides_extension(self):
        assert classify(".pdf", "__MACOSX/._paper.pdf") == "noise"
        assert classify(".pdf", "Resources/paper.pdf") == "document"


class TestCrossBucketDuplicates:
    def test_groups_only_repeated_etags(self):
        result = cross_bucket_duplicates({
            "aaa": ["b1::x.pdf", "b2::y.pdf"],
            "bbb": ["b1::solo.pdf"],
        })
        assert result["identical_content_groups"] == 1
        assert result["groups_spanning_multiple_buckets"] == 1
        assert result["redundant_objects"] == 1

    def test_same_bucket_duplicates_are_not_cross_bucket(self):
        result = cross_bucket_duplicates({"aaa": ["b1::x.pdf", "b1::copy.pdf"]})
        assert result["identical_content_groups"] == 1
        assert result["groups_spanning_multiple_buckets"] == 0
