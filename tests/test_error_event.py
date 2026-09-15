from __future__ import annotations

import json
from pathlib import Path

import pytest

from cited_research.tabstack_runner import run_research


def test_error_event_exits_nonzero_and_prints_message(
    tmp_path: Path, fake_client_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    client, factory = fake_client_factory("error-events.jsonl")
    code = run_research("q", "fast", True, None, tmp_path, quiet=True, client_factory=factory)
    assert code == 2
    assert client.closed is True
    err = capsys.readouterr().err
    assert "synthetic task-level failure for fixture test" in err
    assert "during planning" in err
    assert "secret_internal_frame" not in err

    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["terminal_status"] == "task_error"
    assert manifest["event_sequence"] == ["start", "planning:start", "error"]
    assert not (tmp_path / "report.md").exists()

    log = [
        json.loads(line) for line in (tmp_path / "events.sanitized.jsonl").read_text().splitlines()
    ]
    assert log[-1]["event"] == "error" and log[-1]["error_name"] == "SyntheticError"
    assert "stack" not in json.dumps(log)


def test_stream_without_complete_exits_nonzero(
    tmp_path: Path, fake_client_factory, capsys: pytest.CaptureFixture[str]
) -> None:
    _, factory = fake_client_factory("truncated-events.jsonl")
    code = run_research("q", "fast", True, None, tmp_path, quiet=True, client_factory=factory)
    assert code == 2
    assert "ended without a complete event" in capsys.readouterr().err
    manifest = json.loads((tmp_path / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["terminal_status"] == "task_error"
    assert manifest["event_counts"] == {"start": 1, "searching:start": 1}
