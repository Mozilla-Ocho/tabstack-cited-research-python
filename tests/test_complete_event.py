from __future__ import annotations

import json
from pathlib import Path

from cited_research.tabstack_runner import run_research


def test_complete_writes_report_sources_and_manifest(tmp_path: Path, fake_client_factory) -> None:
    client, factory = fake_client_factory("complete-events.jsonl")
    code = run_research(
        "synthetic question", "fast", True, None, tmp_path, quiet=True, client_factory=factory
    )
    assert code == 0
    assert client.closed is True
    assert client.agent.calls == [{"query": "synthetic question", "mode": "fast", "nocache": True}]

    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Synthetic report")
    sources = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert sources[0]["url"] == "https://example.org/doc"
    assert sources[0]["claims"] == ["A claim"]
    assert "full_text" not in sources[0] and "summary" not in sources[0]

    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["terminal_status"] == "complete"
    assert manifest["cited_page_count"] == 1
    assert manifest["pages_analyzed"] == 5
    assert manifest["event_sequence"][0] == "start" and manifest["event_sequence"][-1] == "complete"
    assert manifest["event_counts"]["complete"] == 1
    assert manifest["tabstack_version"] == "2.8.5"
    assert manifest["sdk_max_retries"] == 2 and manifest["application_retries"] == 0
    assert manifest["duration_ms"] >= 0 and manifest["completed_at_utc"].endswith("Z")
    assert (tmp_path / "question.txt").read_text(encoding="utf-8") == "synthetic question\n"


def test_missing_cited_pages_becomes_empty_list(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-no-cited-pages.jsonl")
    assert run_research("q", "fast", False, None, tmp_path, quiet=True, client_factory=factory) == 0
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8")) == []
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["cited_page_count"] == 0


def test_fetch_timeout_forwarded_only_when_set(tmp_path: Path, fake_client_factory) -> None:
    client, factory = fake_client_factory("complete-events.jsonl")
    run_research("q", "balanced", False, 30, tmp_path, quiet=True, client_factory=factory)
    assert (
        client.agent.calls[0]["fetch_timeout"] == 30 and client.agent.calls[0]["mode"] == "balanced"
    )


def test_outputs_are_utf8_and_deterministic(tmp_path: Path, fake_client_factory) -> None:
    for name in ("a", "b"):
        _, factory = fake_client_factory("complete-events.jsonl")
        run_research("q", "fast", True, None, tmp_path / name, quiet=True, client_factory=factory)
    volatile = {
        "started_at_utc",
        "completed_at_utc",
        "duration_ms",
        "first_event_ms",
        "repository_commit",
    }
    a = json.loads((tmp_path / "a" / "run-manifest.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "b" / "run-manifest.json").read_text(encoding="utf-8"))
    assert {k: v for k, v in a.items() if k not in volatile} == {
        k: v for k, v in b.items() if k not in volatile
    }
    assert (tmp_path / "a" / "report.md").read_bytes() == (
        tmp_path / "b" / "report.md"
    ).read_bytes()
    assert (tmp_path / "a" / "sources.json").read_bytes() == (
        tmp_path / "b" / "sources.json"
    ).read_bytes()
