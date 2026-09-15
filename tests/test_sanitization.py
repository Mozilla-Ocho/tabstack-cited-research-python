from __future__ import annotations

import json
import os
from pathlib import Path

from cited_research.sanitize import sanitize_event, scrub_text
from cited_research.tabstack_runner import run_research


def test_sanitizer_drops_denied_keys_and_nested_payloads() -> None:
    record = sanitize_event(
        "complete",
        {
            "message": "ok",
            "timestamp": 1.0,
            "report": "long report text",
            "metadata": {"total_pages_analyzed": 3, "cited_pages": [{"full_text": "x"}]},
            "authorization": "Bearer abc",
            "api_key": "k",
            "cookie": "c",
            "stack": "trace",
            "full_text": "page",
            "environment": {"HOME": "/x"},
            "reasoning": "model thoughts",
        },
    )
    assert set(record) == {
        "event",
        "received_at_utc",
        "message",
        "timestamp",
        "pages_analyzed",
        "cited_page_count",
        "report_chars",
    }
    assert (
        record["pages_analyzed"] == 3
        and record["cited_page_count"] == 1
        and record["report_chars"] == 16
    )


def test_secret_values_are_scrubbed_from_strings(monkeypatch) -> None:
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_1234567890abcdef")
    assert "[REDACTED]" in scrub_text("failed with key sk_test_1234567890abcdef")
    rec = sanitize_event(
        "error",
        {
            "message": "bad sk_test_1234567890abcdef",
            "error": {"name": "E", "message": "sk_test_1234567890abcdef"},
        },
    )
    assert "sk_test_1234567890abcdef" not in json.dumps(rec)


def test_event_log_never_duplicates_report_or_sources(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-events.jsonl")
    run_research("q", "fast", True, None, tmp_path, quiet=True, client_factory=factory)
    log = (tmp_path / "events.sanitized.jsonl").read_text(encoding="utf-8")
    assert "Synthetic report" not in log
    assert "SHOULD NOT BE PUBLISHED" not in log
    assert "example.org" not in log
    for line in log.splitlines():
        assert "received_at_utc" in json.loads(line)


def test_environment_is_not_dumped(tmp_path: Path, fake_client_factory, monkeypatch) -> None:
    monkeypatch.setenv("SOME_PRIVATE_VAR", "private-marker-value")
    _, factory = fake_client_factory("complete-events.jsonl")
    run_research("q", "fast", True, None, tmp_path, quiet=True, client_factory=factory)
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())
    assert "private-marker-value" not in blob and os.environ["SOME_PRIVATE_VAR"] not in blob
