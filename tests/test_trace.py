"""Replay tests for the trace mode: lifecycle timeline, exit states, cited pages, review sheet."""

from __future__ import annotations

import csv
import io
import json
import re
import threading
from pathlib import Path
from types import SimpleNamespace
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
    text = (d / "review-sheet.csv").read_text(encoding="utf-8-sig")
    return list(csv.DictReader(io.StringIO(text)))


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


SENSITIVE_STRINGS = (
    "private.example",
    "internal.example",
    "token=abc",
    "abc.def.ghi",
    "zzz.yyy",
    "ops@example.com",
    "sk_live_ABCDEFGHIJKLMNOP",
    "secret_internal_frame",
    "sk_test_should_not_leak_123456",
)


def test_sensitive_messages_are_redacted(tmp_path: Path, fake_client_factory, monkeypatch) -> None:
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_should_not_leak_123456")
    _, factory = fake_client_factory("sensitive-messages.jsonl")
    assert _run(tmp_path, factory) == 2
    blob = "".join(p.read_text(encoding="utf-8") for p in tmp_path.iterdir() if p.is_file())
    for leaked in SENSITIVE_STRINGS:
        assert leaked not in blob, leaked
    searching = _log(tmp_path)[1]
    assert "[url]" in searching["message"] and "[email]" in searching["message"]


def test_progress_lines_are_redacted_when_not_quiet(
    tmp_path: Path, fake_client_factory, monkeypatch, capsys
) -> None:
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_should_not_leak_123456")
    _, factory = fake_client_factory("sensitive-messages.jsonl")
    stdout = io.StringIO()
    assert _run(tmp_path, factory, quiet=False, stdout=stdout) == 2
    printed, err = stdout.getvalue(), capsys.readouterr().err
    assert "searching:start" in printed and "[url]" in printed and "[email]" in printed
    for text in (printed, err):
        for leaked in SENSITIVE_STRINGS:
            assert leaked not in text, leaked


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
        ("complete-no-report.jsonl", 10, "malformed_complete"),
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
    if status in ("protocol_error", "malformed_complete"):
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


@pytest.mark.parametrize(
    "exc, code, status",
    [
        (httpx.RemoteProtocolError("peer closed connection"), 9, "stream_transport_error"),
        (httpx.ReadTimeout("timed out"), 9, "stream_transport_error"),
        (ValueError("bad frame"), 11, "unexpected_error"),
    ],
)
def test_mid_stream_failure_still_writes_artifacts(
    tmp_path: Path, exc: BaseException, code: int, status: str, capsys
) -> None:
    events = load_events("truncated-events.jsonl")
    assert events[-1].event == "searching:start"

    def breaks() -> Iterator[Any]:
        yield from events
        raise exc

    client = FakeClient(breaks)
    assert _run(tmp_path, lambda: client) == code
    assert len(client.agent.calls) == 1 and client.closed
    m = _manifest(tmp_path)
    assert m["terminal_status"] == status
    assert m["event_sequence"] == ["start", "searching:start"]
    assert type(exc).__name__ in m["error"]
    assert [r["event"] for r in _log(tmp_path)] == ["start", "searching:start"]
    assert f"terminal state {status}" in (tmp_path / "trace-diagram.md").read_text()
    assert not (tmp_path / "review-sheet.csv").exists()
    assert type(exc).__name__ in capsys.readouterr().err


def test_error_activity_is_redacted_in_manifest_and_stderr(
    tmp_path: Path, fake_client_factory, capsys
) -> None:
    _, factory = fake_client_factory("error-sensitive-activity.jsonl")
    assert _run(tmp_path, factory) == 2
    error = _manifest(tmp_path)["error"]
    err = capsys.readouterr().err
    assert "during fetching [url] for [email]" in error
    for text in (error, err):
        for leaked in ("private.example", "token=abc", "ops@example.com", "zzz.yyy"):
            assert leaked not in text, leaked


def test_http_error_message_is_redacted_in_manifest_and_stderr(tmp_path: Path, capsys) -> None:
    req = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
    exc = tabstack.APIStatusError(
        "bad key sk_live_ABCDEFGHIJKLMNOP seen at https://internal.example/debug",
        response=httpx.Response(400, request=req),
        body=None,
    )
    assert _run(tmp_path, lambda: FakeClient(_raise(exc))) == 3
    error = _manifest(tmp_path)["error"]
    err = capsys.readouterr().err
    assert error.startswith("request rejected (HTTP 400): bad key [REDACTED] seen at [url]")
    for text in (error, err):
        assert "sk_live_ABCDEFGHIJKLMNOP" not in text and "internal.example" not in text


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
    sheet = {r["citation_ids"]: r for r in _sheet(tmp_path)}
    assert sheet["[2][3]"]["source_url"] == "http://example.org/a/"
    assert sheet["[2][3]"]["auto_flags"] == (
        "[2] likely same page as cited page 1 ; [3] not linked (scheme_not_http)"
    )
    assert sheet["[4][5]"]["source_url"] == ""
    assert sheet["[9]"]["source_url"] == "https://example.net/ok"
    assert sheet["[9]"]["api_claims"] == "machine claim"

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


def test_review_sheet_uses_rubric_columns_and_leaves_review_fields_blank(
    tmp_path: Path, fake_client_factory
) -> None:
    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    assert (tmp_path / "review-sheet.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    header = (tmp_path / "review-sheet.csv").read_text(encoding="utf-8-sig").splitlines()[0]
    assert header.split(",") == list(REVIEW_COLUMNS)
    assert header.split(",")[:9] == [
        "claim_id",
        "answer_text",
        "citation_ids",
        "source_url",
        "passage",
        "source_date_or_version",
        "retrieved_at_utc",
        "support",
        "reason",
    ]
    rows = _sheet(tmp_path)
    got = [(r["claim_id"], r["citation_ids"], r["cited_page_ids"]) for r in rows]
    assert got == [
        ("C01", "[3]", "m3"),
        ("C02", "[1]", "z1"),
        ("C03", "", ""),
        ("C04", "[2][3]", "a2 ; m3"),
    ]
    assert rows[3]["source_url"] == "https://alpha.example/two ; https://mid.example/three"
    assert rows[2]["auto_flags"] == "no inline citation"
    for r in rows:
        for field in ("passage", "source_date_or_version", "retrieved_at_utc", "support", "reason"):
            assert r[field] == "", field
    # The trailing Sources block and the heading are not claims.
    assert not any("zeta.example" in r["answer_text"] for r in rows)


def test_review_sheet_flags_markers_without_a_cited_page() -> None:
    from cited_research.models import build_cited_pages
    from cited_research.review import review_rows

    page = SimpleNamespace(id="p1", url="https://example.org/1", claims=[], source_queries=[])
    rows = review_rows("Real [1]. Phantom [2].", build_cited_pages([page]))
    assert rows[1]["auto_flags"] == "[2] has no cited page at position 2"
    assert rows[1]["source_url"] == "" and rows[1]["cited_page_ids"] == ""


def test_citations_article_example_flags_one_page_cited_twice() -> None:
    """The first sample run's sentence that the citations article reviews: [1] and [2] are one
    docs page."""
    from cited_research.models import build_cited_pages
    from cited_research.review import review_rows

    urls = [
        "https://docs.ollama.com/capabilities/web-search.md",
        "http://docs.ollama.com/capabilities/web-search",
        "https://ollama.com/blog/web-search",
    ]
    raw = [
        SimpleNamespace(id=f"p{i}", url=u, claims=[], source_queries=[]) for i, u in enumerate(urls)
    ]
    pages = build_cited_pages(raw)
    rows = review_rows("The Ollama Web Search API requires an API key [1][2][3].", pages)
    assert rows[0]["citation_ids"] == "[1][2][3]"
    assert rows[0]["auto_flags"] == "[2] likely same page as [1]"


def test_review_sheet_neutralises_spreadsheet_formulas() -> None:
    from cited_research.review import review_sheet_csv

    rows = list(csv.DictReader(io.StringIO(review_sheet_csv("=HYPERLINK(1) [1].\n", []))))
    assert rows[0]["answer_text"].startswith("'=")


def test_review_command_rebuilds_sheet_without_network(
    tmp_path: Path, fake_client_factory, capsys
) -> None:
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    original = (tmp_path / "review-sheet.csv").read_bytes()
    (tmp_path / "review-sheet.csv").unlink()
    assert review_main([str(tmp_path)]) == 0
    assert (tmp_path / "review-sheet.csv").read_bytes() == original
    assert "4 candidate claims, unreviewed" in capsys.readouterr().out


def test_review_command_refuses_to_overwrite_a_reviewed_sheet(
    tmp_path: Path, fake_client_factory, capsys
) -> None:
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    sheet = tmp_path / "review-sheet.csv"
    rows = _sheet(tmp_path)
    rows[0]["support"] = "2"
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    sheet.write_text(buf.getvalue(), encoding="utf-8")

    assert review_main([str(tmp_path)]) == 1
    assert "already has review entries" in capsys.readouterr().err
    assert _sheet(tmp_path)[0]["support"] == "2"
    assert review_main([str(tmp_path), "--force"]) == 0
    assert _sheet(tmp_path)[0]["support"] == ""


def _write_sheet(path: Path, rows: List[Dict[str, str]], delimiter: str = ",") -> None:
    buf = io.StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=list(REVIEW_COLUMNS), delimiter=delimiter, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(buf.getvalue(), encoding="utf-8")


def test_review_command_refuses_when_rows_were_split_merged_or_deleted(
    tmp_path: Path, fake_client_factory, capsys
) -> None:
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    sheet = tmp_path / "review-sheet.csv"
    rows = _sheet(tmp_path)
    merged = dict(rows[0], answer_text=rows[0]["answer_text"] + " " + rows[1]["answer_text"])
    _write_sheet(sheet, [merged, *rows[2:]])
    assert review_main([str(tmp_path)]) == 1
    assert "edited rows" in capsys.readouterr().err
    assert len(_sheet(tmp_path)) == 3
    assert review_main([str(tmp_path), "--force"]) == 0
    assert len(_sheet(tmp_path)) == 4


def test_review_command_overwrites_an_untouched_sheet(tmp_path: Path, fake_client_factory) -> None:
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    _write_sheet(tmp_path / "review-sheet.csv", _sheet(tmp_path), delimiter=";")
    assert review_main([str(tmp_path)]) == 0


def test_review_command_reads_a_bom_sheet_with_entries(
    tmp_path: Path, fake_client_factory, capsys
) -> None:
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    sheet = tmp_path / "review-sheet.csv"
    rows = _sheet(tmp_path)
    rows[0]["support"] = "U"
    _write_sheet(sheet, rows)
    sheet.write_bytes(b"\xef\xbb\xbf" + sheet.read_bytes())
    assert review_main([str(tmp_path)]) == 1
    assert "already has review entries" in capsys.readouterr().err


def test_review_command_reads_semicolon_sheets(tmp_path: Path, fake_client_factory, capsys) -> None:
    from cited_research.review import has_review_entries
    from cited_research.review import main as review_main

    _, factory = fake_client_factory("complete-ordered-sources.jsonl")
    _run(tmp_path, factory)
    sheet = tmp_path / "review-sheet.csv"
    rows = _sheet(tmp_path)
    rows[3]["support"] = "1"
    _write_sheet(sheet, rows, delimiter=";")
    assert has_review_entries(sheet.read_text(encoding="utf-8"))
    assert review_main([str(tmp_path)]) == 1
    assert "already has review entries" in capsys.readouterr().err


def test_review_command_rejects_schema_1_sources_cleanly(tmp_path: Path, capsys) -> None:
    from cited_research.review import main as review_main

    (tmp_path / "report.md").write_text("One [1].\n", encoding="utf-8")
    schema_1 = [{"id": "p1", "url": "https://example.org/1", "claims": [], "source_queries": []}]
    (tmp_path / "sources.json").write_text(json.dumps(schema_1), encoding="utf-8")
    assert review_main([str(tmp_path)]) == 1
    assert "not a trace-path sources.json (schema 2)" in capsys.readouterr().err
    assert not (tmp_path / "review-sheet.csv").exists()


def test_candidate_claims_and_markers() -> None:
    report = "# H\n\nOne [1][2]. Two [3, 1]! Three?\n\n**Sources**\n[1] x\n"
    assert candidate_claims(report) == ["One [1][2].", "Two [3, 1]!", "Three?"]
    assert markers_in("a [1][2] b [2, 3]") == [1, 2, 3]


def test_leading_marker_group_attaches_to_the_previous_sentence() -> None:
    from cited_research.models import build_cited_pages
    from cited_research.review import review_rows

    report = "One result per call. [2] Next claim [3]."
    assert candidate_claims(report) == ["One result per call. [2]", "Next claim [3]."]
    raw = [
        SimpleNamespace(id=f"p{i}", url=f"https://example.org/{i}", claims=[], source_queries=[])
        for i in (1, 2, 3)
    ]
    pages = build_cited_pages(raw)
    rows = review_rows(report, pages)
    assert [(r["citation_ids"], r["auto_flags"]) for r in rows] == [("[2]", ""), ("[3]", "")]
    assert candidate_claims("Done. [1][2, 3]\nNext [4].") == ["Done. [1][2, 3]", "Next [4]."]


def test_no_sentence_split_after_abbreviations_or_initials() -> None:
    report = (
        "Some tools, e.g. web search, return snippets [1]. The U.S. docs list it [2]. "
        "J. Smith wrote it, i.e. the post [3]. Then a new sentence."
    )
    assert candidate_claims(report) == [
        "Some tools, e.g. web search, return snippets [1].",
        "The U.S. docs list it [2].",
        "J. Smith wrote it, i.e. the post [3].",
        "Then a new sentence.",
    ]


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
        ("https://127.1/", False),
        ("https://0x7f.1/x", False),
        ("https://0177.0.0.1/", False),
        ("https://docs.example2.com/v1", True),
        ("", False),
        (None, False),
    ],
)
def test_check_public_url(url: Any, ok: bool) -> None:
    assert check_public_url(url)[0] is ok


def test_numeric_hostnames_name_their_issue() -> None:
    assert check_public_url("https://127.1/") == (False, "numeric_hostname")


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


def test_question_file_is_scrubbed_like_the_command(
    tmp_path: Path, fake_client_factory, monkeypatch
) -> None:
    secret = "sk_live_pasted_into_the_question_0123"
    monkeypatch.setenv("TABSTACK_API_KEY", secret)
    _, factory = fake_client_factory("complete-events.jsonl")
    run_research(
        f"why does {secret} fail?",
        "fast",
        True,
        None,
        tmp_path,
        quiet=True,
        client_factory=factory,
        post_terminal_grace=0.2,
    )
    assert (tmp_path / "question.txt").read_text(encoding="utf-8") == "why does [REDACTED] fail?\n"


def test_git_commit_is_this_packages_repo_not_the_cwd(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    import cited_research.tabstack_runner as runner

    repo = Path(runner.__file__).resolve().parents[2]
    expected = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    monkeypatch.chdir(tmp_path)
    assert runner._git_commit() == (expected or None)
