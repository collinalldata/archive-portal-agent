"""Tests for PDF sampling and the OCR estimate.

The estimator is the part worth testing. Stratified sampling deliberately
oversamples the rare large-file bands, so every corpus-wide figure has to be
weighted back; and page counts have to be summed per document rather than
derived from an overall mean, because documents needing OCR run shorter than
text-native ones.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from sample_pdfs import allocate, collect_pdfs, stratum_of  # noqa: E402


class TestStratumOf:
    def test_bands(self):
        assert stratum_of(40_000) == "tiny"
        assert stratum_of(500_000) == "small"
        assert stratum_of(5_000_000) == "medium"
        assert stratum_of(50_000_000) == "large"
        assert stratum_of(500_000_000) == "huge"

    def test_boundaries_are_half_open(self):
        assert stratum_of(100_000) == "small"
        assert stratum_of(99_999) == "tiny"


class TestAllocate:
    def test_proportional_to_population(self):
        strata = {"tiny": [{}] * 900, "small": [{}] * 100}
        result = allocate(strata, total=100, minimum=0)
        assert result["tiny"] == 90
        assert result["small"] == 10

    def test_minimum_floor_lifts_rare_strata(self):
        strata = {"tiny": [{}] * 9_990, "huge": [{}] * 10}
        result = allocate(strata, total=100, minimum=5)
        assert result["huge"] == 5

    def test_never_asks_for_more_than_exists(self):
        strata = {"huge": [{}] * 3}
        assert allocate(strata, total=100, minimum=50)["huge"] == 3


class TestCollectPdfs:
    def _write(self, tmp_path, bucket, keys):
        import gzip, json
        path = tmp_path / f"{bucket}.jsonl.gz"
        with gzip.open(path, "wt") as handle:
            for key, size in keys:
                handle.write(json.dumps(
                    {"Key": key, "Size": size, "ETag": "x", "Class": "STANDARD",
                     "Modified": "2020-01-01T00:00:00+00:00"}) + "\n")
        return tmp_path

    def test_filters_to_pdfs_only(self, tmp_path):
        indir = self._write(tmp_path, "b", [("a.pdf", 500), ("b.docx", 500)])
        found = collect_pdfs(indir, {}, [])
        assert [o["Key"] for o in found["tiny"]] == ["a.pdf"]

    def test_honors_exclude_prefixes(self, tmp_path):
        indir = self._write(tmp_path, "b", [
            ("ftpArchive/cms.pdf", 500), ("Resources/real.pdf", 500)])
        found = collect_pdfs(indir, {"b": ["ftpArchive/"]}, [])
        assert [o["Key"] for o in found["tiny"]] == ["Resources/real.pdf"]

    def test_skips_noise_and_zero_byte(self, tmp_path):
        indir = self._write(tmp_path, "b", [
            ("._sidecar.pdf", 500), ("empty.pdf", 0), ("real.pdf", 500)])
        found = collect_pdfs(indir, {}, [])
        assert [o["Key"] for o in found["tiny"]] == ["real.pdf"]

    def test_only_filter_restricts_buckets(self, tmp_path):
        self._write(tmp_path, "keep", [("a.pdf", 500)])
        indir = self._write(tmp_path, "drop", [("b.pdf", 500)])
        found = collect_pdfs(indir, {}, ["keep"])
        assert [o["Bucket"] for o in found["tiny"]] == ["keep"]
