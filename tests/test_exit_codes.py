from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cited_research.cli import main


def test_missing_api_key_exits_5_without_network(
    tmp_path: Path, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TABSTACK_API_KEY", raising=False)
    code = main(["--query", "q", "--output", str(tmp_path)])
    assert code == 5
    assert "TABSTACK_API_KEY is not set" in capsys.readouterr().err


def test_cli_never_prints_the_api_key(
    tmp_path: Path, fake_client_factory, monkeypatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "sk_live_should_never_appear_0123456789"
    monkeypatch.setenv("TABSTACK_API_KEY", secret)
    _, factory = fake_client_factory("complete-events.jsonl")
    from cited_research.tabstack_runner import run_research

    run_research("q", "fast", True, None, tmp_path, quiet=False, client_factory=factory)
    captured = capsys.readouterr()
    assert secret not in captured.out and secret not in captured.err
    assert secret not in "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir())


def test_cli_rejects_key_as_flag() -> None:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "cited_research.cli",
            "--query",
            "q",
            "--output",
            "/tmp/x",
            "--api-key",
            "abc",
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2
    assert "unrecognized arguments: --api-key" in proc.stderr
