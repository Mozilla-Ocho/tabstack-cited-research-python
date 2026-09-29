"""Replay tests for the Week 3 trace: lifecycle timeline, exit states, cited pages, review sheet."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List

import httpx
import pytest
import tabstack
from conftest import FakeClient, load_events

from cited_research.cli import command_line, main
from cited_research.review import REVIEW_COLUMNS, candidate_claims, markers_in
from cited_research.tabstack_runner import run_research
from cited_research.urls import check_public_url


def _run(tmp_path: Path, factory: Any, **kw: Any) -> int:
    kw.setdefault("quiet", True)
    kw.setdefault("post_terminal_grace", 0.2)
    return run_research("q", "fast", True, None, tmp_path, client_factory=factory, **kw)


def _manifest(d: Path) -> Dict[str, Any]:
    return json.loads((d / "run-manifest.json").read_text(encoding="utf-8"))


def _log(d: Path) -> List[Dict[str, Any]]:
    return [json.loads(x) for x in (d / "events.sanitized.jsonl").read_text().splitlines()]


def _sheet(d: Path) -> List[Dict[str, str]]:
    return list(csv.DictReader(io.StringIO((d / "review-sheet.csv").read_text(encoding="utf-8"))))


# --- timeline ------------------------------------------------------------------------------


def test_timeline_has_order_elapsed_and_raw_timestamp_type(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    assert _run(tmp_path, factory) == 0
    log = _log(tmp_path)
    assert [r["seq"] for r in log] == list(range(1, len(log) + 1))
    elapsed = [r["elapsed_ms"] for r in log]
    assert elapsed == sorted(elapsed) and all(isinstance(e, int) and e >= 0 for e in elapsed)
    # Every server timestamp in this fixture is identical; order comes from seq, not timestamp.
    assert len({r["timestamp_raw"] for r in log}) == 1
    for r in log:
        assert "timestamp" not in r
        assert r["timestamp_type"] == type(r["timestamp_raw"]).__name__
        assert r["known_event"] is True
    assert _manifest(tmp_path)["timestamp_types"] == [log[0]["timestamp_type"]]


def test_timeline_drops_non_allowlisted_payloads(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    blob = (tmp_path / "events.sanitized.jsonl").read_text()
    assert "secret-ish query text" not in blob
    assert "First sentence" not in blob and "zeta.example" not in blob


def test_sensitive_messages_are_redacted(tmp_path: Path, fake_client_factory, monkeypatch) -> None:
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_should_not_leak_123456")
    _, factory = fake_client_factory("sensitive-messages.jsonl")
    assert _run(tmp_path, factory) == 2
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file())
    for leaked in (
        "private.example",
        "internal.example",
        "token=abc",
        "abc.def.ghi",
        "zzz.yyy",
        "ops@example.com",
        "sk_live_ABCDEFGHIJKLMNOP",
        "secret_internal_frame",
        "sk_test_should_not_leak_123456",
    ):
        assert leaked not in blob, leaked
    searching = _log(tmp_path)[1]
    assert "[url]" in searching["message"] and "[email]" in searching["message"]


# --- exit states ---------------------------------------------------------------------------


def _status_error(code: int) -> tabstack.APIStatusError:
    req = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
    resp = httpx.Response(code, request=req)
    return tabstack.APIStatusError("Unauthorized", response=resp, body=None)


def _raise(exc: BaseException):
    def opener() -> Iterator[Any]:
        raise exc

    return opener


@pytest.mark.parametrize(
    "fixture, code, status",
    [
        ("complete-events.jsonl", 0, "complete"),
        ("error-events.jsonl", 2, "task_error"),
        ("truncated-events.jsonl", 6, "premature_close"),
        ("duplicate-complete.jsonl", 8, "protocol_error"),
        ("complete-then-error.jsonl", 8, "protocol_error"),
    ],
)
def test_replay_exit_states(
    tmp_path: Path, fake_client_factory, fixture: str, code: int, status: str
) -> None:
    client, factory = fake_client_factory(fixture)
    assert _run(tmp_path, factory) == code
    assert len(client.agent.calls) == 1, "never retried"
    assert client.closed
    m = _manifest(tmp_path)
    assert m["terminal_status"] == status and m["application_retries"] == 0
    assert (tmp_path / "events.sanitized.jsonl").exists()
    assert (tmp_path / "trace-diagram.md").exists()
    assert (tmp_path / "review-sheet.csv").exists() == (status == "complete")
    if status == "protocol_error":
        assert not (tmp_path / "report.md").exists()


@pytest.mark.parametrize(
    "exc, code, status",
    [
        (_status_error(401), 3, "http_error"),
        (
            tabstack.APIConnectionError(request=httpx.Request("POST", "https://x")),
            4,
            "transport_error",
        ),
    ],
)
def test_request_open_failures(tmp_path: Path, exc: BaseException, code: int, status: str) -> None:
    client = FakeClient(_raise(exc))
    assert _run(tmp_path, lambda: client) == code
    assert len(client.agent.calls) == 1
    m = _manifest(tmp_path)
    assert m["terminal_status"] == status and m["event_sequence"] == []
    assert (tmp_path / "events.sanitized.jsonl").read_text() == ""


def test_default_client_disables_sdk_retries(tmp_path: Path, monkeypatch) -> None:
    import cited_research.tabstack_runner as runner

    made: List[Dict[str, Any]] = []
    events = load_events("complete-events.jsonl")

    class RecordingClient(FakeClient):
        def __init__(self, **kwargs: Any):
            super().__init__(events)
            made.append(kwargs)
            self.max_retries = kwargs.get("max_retries", 2)

    monkeypatch.setattr(runner, "Tabstack", RecordingClient)
    assert run_research("q", "fast", True, None, tmp_path, quiet=True, post_terminal_grace=0.2) == 0
    assert made == [{"max_retries": 0}]
    m = _manifest(tmp_path)
    assert m["sdk_max_retries"] == 0 and m["application_retries"] == 0


def test_iteration_end_keeps_is_last_and_stop_reason() -> None:
    from cited_research.sanitize import sanitize_event

    rec = sanitize_event(
        "iteration:end",
        {"iteration": 1, "is_last": True, "stop_reason": "max_iterations", "queries": ["q"]},
    )
    assert rec["is_last"] is True and rec["stop_reason"] == "max_iterations"
    assert "queries" not in rec


def test_silence_timeout_exits_7_without_retry(tmp_path: Path, capsys) -> None:
    release = threading.Event()
    first = load_events("truncated-events.jsonl")[0]

    def stalls() -> Iterator[Any]:
        yield first
        release.wait(10)

    client = FakeClient(stalls)
    try:
        assert _run(tmp_path, lambda: client, silence_timeout=0.2) == 7
    finally:
        release.set()
    assert len(client.agent.calls) == 1
    m = _manifest(tmp_path)
    assert m["terminal_status"] == "silence_timeout" and m["silence_timeout_seconds"] == 0.2
    assert m["event_sequence"] == ["start"]
    assert "may still be running" in capsys.readouterr().err


def test_silence_before_first_event_also_times_out(tmp_path: Path) -> None:
    release = threading.Event()

    def never() -> Iterator[Any]:
        release.wait(10)
        return iter(())

    client = FakeClient(lambda: never())
    try:
        assert _run(tmp_path, lambda: client, silence_timeout=0.2) == 7
    finally:
        release.set()
    assert _manifest(tmp_path)["first_event_ms"] is None


def test_open_stream_after_complete_keeps_result(tmp_path: Path) -> None:
    release = threading.Event()
    events = load_events("complete-events.jsonl")

    def lingers() -> Iterator[Any]:
        yield from events
        release.wait(10)

    client = FakeClient(lingers)
    try:
        assert _run(tmp_path, lambda: client, post_terminal_grace=0.2) == 0
    finally:
        release.set()
    m = _manifest(tmp_path)
    assert m["stream_closed_after_terminal"] is False
    assert (tmp_path / "report.md").exists()


def test_clean_close_after_complete_is_recorded(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-events.jsonl")
    assert _run(tmp_path, factory) == 0
    assert _manifest(tmp_path)["stream_closed_after_terminal"] is True


def test_progress_after_complete_is_a_caveat_not_a_failure(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-then-progress.jsonl")
    assert _run(tmp_path, factory) == 0
    m = _manifest(tmp_path)
    assert any("arrived after complete" in c for c in m["caveats"])
    assert m["event_sequence"][-1] == "writing:end"


# --- cited pages ---------------------------------------------------------------------------


def test_sources_keep_returned_order(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    sources = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert [(s["position"], s["id"]) for s in sources] == [(1, "z1"), (2, "a2"), (3, "m3")]
    assert all(s["link_ok"] for s in sources)
    assert set(sources[0]) >= {"id", "url", "claims", "source_queries", "title", "position"}


def test_malformed_and_non_public_sources_are_flagged_not_dropped(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-malformed-sources.jsonl")
    assert _run(tmp_path, factory) == 0
    sources = json.loads((tmp_path / "sources.json").read_text(encoding="utf-8"))
    assert [s["position"] for s in sources] == list(range(1, 10))
    by_pos = {s["position"]: s for s in sources}
    assert by_pos[1]["link_ok"] and by_pos[9]["link_ok"]
    assert by_pos[2]["duplicate_of_position"] == 1
    assert by_pos[3]["link_issue"] == "scheme_not_http"
    assert by_pos[4]["link_issue"] == "non_public_ip"
    assert by_pos[5]["link_issue"] == "embedded_credentials"
    assert by_pos[5]["url"] == "https://[REDACTED]@example.com/x"
    assert by_pos[6]["link_issue"] == "private_hostname"
    assert by_pos[7]["url"] is None
    assert set(by_pos[7]["malformed_fields"]) == {"id", "url", "claims"}
    assert by_pos[8]["link_issue"] == "non_public_ip"
    m = _manifest(tmp_path)
    assert m["rejected_link_count"] == 6 and m["cited_page_count"] == 9

    assert "user:pw" not in "".join(p.read_text() for p in tmp_path.iterdir() if p.is_file())
    sheet_urls = {r["source_position"]: r["source_url"] for r in _sheet(tmp_path)}
    assert sheet_urls["3"] == sheet_urls["4"] == "" and sheet_urls["9"] == "https://example.net/ok"

    diagram = (tmp_path / "trace-diagram.md").read_text(encoding="utf-8")
    assert "javascript:" not in diagram and "(http://127.0.0.1" not in diagram
    assert "](https://example.net/ok)" in diagram
    assert "not linked (embedded_credentials)" in diagram


def test_complete_without_citations_is_review_needed(tmp_path: Path, fake_client_factory) -> None:
    _, factory = fake_client_factory("complete-no-cited-pages.jsonl")
    assert _run(tmp_path, factory) == 0
    assert json.loads((tmp_path / "sources.json").read_text(encoding="utf-8")) == []
    m = _manifest(tmp_path)
    assert m["review_state"] == "review_needed_no_sources"
    assert "verified" not in json.dumps(m).lower().replace("not verify", "")
    assert "no cited_pages returned" in (tmp_path / "trace-diagram.md").read_text()


# --- review sheet --------------------------------------------------------------------------


def test_review_sheet_maps_markers_to_positions_and_leaves_judgments_blank(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    header = (tmp_path / "review-sheet.csv").read_text(encoding="utf-8").splitlines()[0]
    assert header.split(",") == list(REVIEW_COLUMNS)
    rows = _sheet(tmp_path)
    pairs = [(r["claim_id"], r["citation_marker"], r["source_id"]) for r in rows]
    assert pairs == [
        ("C01", "[3]", "m3"),
        ("C02", "[1]", "z1"),
        ("C03", "", ""),
        ("C04", "[2]", "a2"),
        ("C04", "[3]", "m3"),
    ]
    assert rows[2]["notes"] == "no inline citation"
    for r in rows:
        assert r["supporting_passage"] == r["judgment"] == r["reviewer"] == ""
    # The trailing Sources block and the heading are not claims.
    assert not any("zeta.example" in r["report_excerpt"] for r in rows)


def test_review_sheet_flags_markers_without_a_cited_page(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-no-cited-pages.jsonl")
    _run(tmp_path, factory)
    assert _sheet(tmp_path) == []  # the fixture report is only a heading


def test_review_sheet_neutralises_spreadsheet_formulas() -> None:
    from cited_research.review import review_sheet_csv

    rows = list(csv.DictReader(io.StringIO(review_sheet_csv("=HYPERLINK(1) [1].\n", []))))
    assert rows[0]["report_excerpt"].startswith("'=")


def test_candidate_claims_and_markers() -> None:
    report = "# H\n\nOne [1][2]. Two [3, 1]! Three?\n\n**Sources**\n[1] x\n"
    assert candidate_claims(report) == ["One [1][2].", "Two [3, 1]!", "Three?"]
    assert markers_in("a [1][2] b [2, 3]") == [1, 2, 3]


# --- URL check -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url, ok",
    [
        ("https://docs.ollama.com/capabilities/web-search", True),
        ("http://example.org", True),
        ("https://8.8.8.8/x", True),
        ("ftp://example.org/x", False),
        ("file:///etc/passwd", False),
        ("https://localhost/x", False),
        ("https://printer.local/", False),
        ("https://[::1]/", False),
        ("https://192.168.1.1/", False),
        ("https://169.254.169.254/latest", False),
        ("https://intranet/", False),
        ("https://exa mple.org/", False),
        ("https://example.org:99999/", False),
        ("", False),
        (None, False),
    ],
)
def test_check_public_url(url: Any, ok: bool) -> None:
    assert check_public_url(url)[0] is ok


# --- secrets -------------------------------------------------------------------------------


SECRET_SHAPES = re.compile(r"(sk_(live|test)_[A-Za-z0-9]{8,}|Bearer\s+\S+|Authorization:|Cookie:)")


def test_cli_run_writes_command_and_no_secrets(
    tmp_path: Path, fake_client_factory, monkeypatch, capsys
) -> None:
    secret = "sk_live_cli_secret_value_0123456789"
    monkeypatch.setenv("TABSTACK_API_KEY", secret)
    _, factory = fake_client_factory("complete-events.jsonl")
    argv = ["--query", "q", "--mode", "fast", "--nocache", "--output", str(tmp_path)]
    monkeypatch.setattr(
        "cited_research.cli.run_research",
        lambda **kw: run_research(**kw, client_factory=factory, post_terminal_grace=0.2),
    )
    assert main(argv) == 0
    command = (tmp_path / "command.txt").read_text(encoding="utf-8")
    assert command.startswith("cited-research \\\n  --query q")
    out = capsys.readouterr()
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file())
    for text in (blob, out.out, out.err):
        assert secret not in text
        assert not SECRET_SHAPES.search(text)


def test_command_line_quotes_arguments() -> None:
    assert command_line(["--query", "it's", "--nocache"]) == (
        "cited-research \\\n  --query 'it'\"'\"'s' \\\n  --nocache"
    )


def test_silence_timeout_flag_must_be_positive(capsys) -> None:
    with pytest.raises(SystemExit):
        main(["--query", "q", "--output", "x", "--silence-timeout", "0"])
    assert "must be greater than 0" in capsys.readouterr().err
