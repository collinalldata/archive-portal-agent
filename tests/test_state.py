"""Tests for the resumable ingestion ledger.

CLAUDE.md §5: re-running ingestion must not duplicate records, and a failure at
document 4,000 of 6,000 must not restart from zero.
"""

import pytest

from pipeline.state import Ledger


@pytest.fixture
def ledger(tmp_path):
    with Ledger(tmp_path / "ledger.sqlite3") as l:
        yield l


class TestResumability:
    def test_unseen_key_is_not_done(self, ledger):
        assert not ledger.is_done("never/seen.pdf")

    def test_completed_key_is_skipped_on_rerun(self, ledger):
        ledger.mark_done("a.pdf", "rg-000000000001", "a" * 64, etag='"e1"', byte_size=10)
        assert ledger.is_done("a.pdf")

    def test_changed_etag_forces_reprocessing(self, ledger):
        ledger.mark_done("a.pdf", "rg-000000000001", "a" * 64, etag='"e1"')
        assert ledger.is_done("a.pdf", etag="e1")
        assert not ledger.is_done("a.pdf", etag="e2")

    def test_etag_quoting_is_ignored(self, ledger):
        ledger.mark_done("a.pdf", "rg-000000000001", "a" * 64, etag="e1")
        assert ledger.is_done("a.pdf", etag='"e1"')

    def test_failed_key_is_retried(self, ledger):
        ledger.mark_failed("a.pdf", "pdftotext returned nonzero")
        assert not ledger.is_done("a.pdf")

    def test_failure_then_success_clears_the_failure(self, ledger):
        ledger.mark_failed("a.pdf", "transient")
        ledger.mark_done("a.pdf", "rg-000000000001", "a" * 64)
        assert ledger.is_done("a.pdf")
        assert ledger.counts() == {"done": 1}

    def test_attempts_accumulate(self, ledger):
        for _ in range(3):
            ledger.mark_failed("a.pdf", "boom")
        assert ledger.get("a.pdf").attempts == 3

    def test_survives_reopen(self, tmp_path):
        path = tmp_path / "l.sqlite3"
        with Ledger(path) as first:
            first.mark_done("a.pdf", "rg-000000000001", "a" * 64)
        with Ledger(path) as second:
            assert second.is_done("a.pdf")


class TestDeduplication:
    def test_unseen_text_returns_none(self, ledger):
        assert ledger.seen_text("f" * 64) is None

    def test_same_text_under_a_different_key_is_found(self, ledger):
        digest = "c" * 64
        ledger.mark_done("site-a/report.pdf", "sa-000000000001", digest)
        assert ledger.seen_text(digest) == "sa-000000000001"
        assert not ledger.is_done("site-b/report-final.pdf")

    def test_failed_records_do_not_count_as_seen_text(self, ledger):
        ledger.mark_failed("a.pdf", "boom")
        assert ledger.seen_text("a" * 64) is None


class TestBookkeeping:
    def test_counts_by_status(self, ledger):
        ledger.mark_done("a.pdf", "id1", "a" * 64)
        ledger.mark_failed("b.pdf", "boom")
        ledger.mark_skipped("c.gif", "nav thumbnail")
        assert ledger.counts() == {"done": 1, "failed": 1, "skipped": 1}

    def test_failures_are_listable_for_triage(self, ledger):
        ledger.mark_failed("a.pdf", "encrypted")
        ledger.mark_failed("b.pdf", "truncated")
        assert {f.s3_key for f in ledger.failures()} == {"a.pdf", "b.pdf"}

    def test_long_errors_are_truncated(self, ledger):
        ledger.mark_failed("a.pdf", "x" * 5000)
        assert len(ledger.get("a.pdf").error) == 2000
