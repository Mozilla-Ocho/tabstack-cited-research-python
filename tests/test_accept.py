"""Offline tests for the acceptance kit (cited-research-accept). No network, no API key."""

from __future__ import annotations

import csv
import json
import re
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List

import httpx
import pytest
import tabstack
from acceptance_synth import (
    ZeroRetryFake,
    attempt,
    build_synthetic_run,
    fill,
    fixture_client,
    raising_client,
    synthetic_questions,
    write_synthetic_dataset,
)
from conftest import load_events

from cited_research.accept import cli
from cited_research.accept.dataset import (
    CATEGORIES,
    ELEMENT_KEYS,
    MAX_ELEMENTS,
    MIN_ELEMENTS,
    RECORD_KEYS,
    SOURCE_KEYS,
    freeze_dataset,
    load_dataset,
    validate_dataset,
)
from cited_research.accept.reviews import (
    CLAIM_COLUMNS,
    COVERAGE_COLUMNS,
    RELEASE_COLUMNS,
    RELEASE_GATES,
    prepare_review,
    read_review_sheet,
)
from cited_research.accept.run import ATTEMPT_COLUMNS, RunRefused, read_attempts
from cited_research.accept.summary import SheetError, summarize

ROOT = Path(__file__).resolve().parents[1]
KIT = ROOT / "acceptance-evals"
CANDIDATE = KIT / "questions.candidate.jsonl"
PILOT = KIT / "pilot" / "questions.pilot-q05.jsonl"


def _write(path: Path, rows: List[Any]) -> Path:
    path.write_text(
        "".join((r if isinstance(r, str) else json.dumps(r)) + "\n" for r in rows),
        encoding="utf-8",
    )
    return path


def _candidate_rows() -> List[Dict[str, Any]]:
    return [json.loads(x) for x in CANDIDATE.read_text(encoding="utf-8").splitlines()]


def _messages(path: Path, **kw: Any) -> List[str]:
    return [str(i) for i in validate_dataset(load_dataset(path), **kw)]


# --- dataset validation ----------------------------------------------------------------------


def test_candidate_set_is_valid_structure_and_not_runnable() -> None:
    ds = load_dataset(CANDIDATE)
    assert validate_dataset(ds) == []
    assert len(ds.records) == 20
    assert [r["category"] for r in ds.records].count("exact_contract") == 4
    assert ds.pending_ids() == [f"Q{i:02d}" for i in range(1, 21) if i != 5]
    live = _messages(CANDIDATE, for_run=True)
    assert len(live) == 19 and all("pending" in m for m in live)


def test_q05_matches_the_article_record() -> None:
    q05 = next(r for r in _candidate_rows() if r["id"] == "Q05")
    assert q05["question"] == (
        "What fields does Ollama's web-search API return for each result, and does its content "
        "field contain the full page?"
    )
    assert [e["criterion"] for e in q05["required_elements"]] == [
        "Name title, url, and content as the result fields.",
        "Describe content as a relevant snippet, not the full page.",
        "Cite the official response definition.",
    ]
    src = q05["reference_sources"][0]
    assert src["passage"] == "content (string): relevant content snippet from the web page"
    assert q05["critical_failure_conditions"] == [
        "States that web_search returns full page content."
    ]
    assert q05["evidence_status"] == "reviewed"
    pilot = json.loads(PILOT.read_text(encoding="utf-8"))
    assert pilot == q05


def test_pilot_set_is_frozen_with_matching_hash() -> None:
    ds = load_dataset(PILOT)
    freeze = json.loads((PILOT.parent / (PILOT.name + ".freeze.json")).read_text())
    assert freeze["dataset_sha256"] == ds.sha256
    assert freeze["question_ids"] == ["Q05"] and freeze["design"] == "subset"
    assert validate_dataset(ds, allow_subset=True) == []


def test_schema_file_matches_the_validator() -> None:
    schema = json.loads((KIT / "dataset-schema.json").read_text(encoding="utf-8"))
    assert set(schema["required"]) == RECORD_KEYS == set(schema["properties"])
    assert schema["additionalProperties"] is False
    assert tuple(schema["properties"]["category"]["enum"]) == tuple(CATEGORIES)
    els = schema["properties"]["required_elements"]
    assert (els["minItems"], els["maxItems"]) == (MIN_ELEMENTS, MAX_ELEMENTS)
    assert set(els["items"]["required"]) == ELEMENT_KEYS
    srcs = schema["properties"]["reference_sources"]["items"]
    assert set(srcs["required"]) == SOURCE_KEYS


def test_malformed_json_is_reported_with_its_line(tmp_path: Path) -> None:
    rows: List[Any] = _candidate_rows()
    rows[3] = '{"id": "Q04", "category": '
    msgs = _messages(_write(tmp_path / "d.jsonl", rows))
    assert any(m.startswith("line 4: malformed JSON") for m in msgs)


def test_duplicate_ids_are_rejected(tmp_path: Path) -> None:
    rows = _candidate_rows()
    rows[1]["id"] = "Q01"
    msgs = _messages(_write(tmp_path / "d.jsonl", rows))
    assert any("duplicate question id Q01" in m for m in msgs)


def test_wrong_total_and_category_counts(tmp_path: Path) -> None:
    rows = _candidate_rows()
    msgs = _messages(_write(tmp_path / "short.jsonl", rows[:19]))
    assert "dataset: 19 questions; this design needs 20" in msgs
    assert any("limits_uncertainty has 3" in m for m in msgs)
    rows[0]["category"] = "exact_contract"
    msgs = _messages(_write(tmp_path / "skew.jsonl", rows))
    assert any("source_discovery has 3" in m for m in msgs)
    assert any("exact_contract has 5" in m for m in msgs)
    rows[0]["category"] = "trivia"
    msgs = _messages(_write(tmp_path / "bad.jsonl", rows))
    assert any("unexpected category 'trivia'" in m for m in msgs)


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda r: r.update(question="   "), "question is blank"),
        (lambda r: r["required_elements"][0].update(criterion=""), "criterion is blank"),
        (
            lambda r: r["required_elements"].__setitem__(1, dict(r["required_elements"][0])),
            "duplicate element id E1",
        ),
        (lambda r: r.update(required_elements=r["required_elements"][:1]), "1 required elements"),
        (
            lambda r: r.update(
                required_elements=[{"id": f"E{i}", "criterion": "x" * 12} for i in range(1, 7)]
            ),
            "6 required elements",
        ),
        (lambda r: r.update(extra=True), "unknown property 'extra'"),
        (lambda r: r["reference_sources"][0].update(note="x"), "unknown property 'note'"),
        (
            lambda r: r.pop("critical_failure_conditions"),
            "missing property 'critical_failure_conditions'",
        ),
        (
            lambda r: r["reference_sources"][0].update(retrieved_at_utc="2026-10-06 17:37"),
            "not a valid UTC date-time",
        ),
        (
            lambda r: r["reference_sources"][0].update(retrieved_at_utc="2026-02-30T00:00:00Z"),
            "not a valid UTC date-time",
        ),
        (
            lambda r: r["reference_sources"][0].update(
                retrieved_at_utc="2026-10-06T17:37:54-05:00"
            ),
            "not a valid UTC date-time",
        ),
        (lambda r: r["reference_sources"][0].update(passage=None), "needs the inspected passage"),
        (
            lambda r: r["reference_sources"][0].update(retrieved_at_utc=None),
            "needs retrieved_at_utc",
        ),
        (lambda r: r["reference_sources"][0].update(date_or_version=""), "needs date_or_version"),
        (
            lambda r: r["reference_sources"][0].update(url="http://docs.ollama.com/x"),
            "must use https",
        ),
        (
            lambda r: r["reference_sources"][0].update(url="https://user:pw@docs.ollama.com/"),
            "embedded_credentials",
        ),
        (lambda r: r["reference_sources"][0].update(url="https://localhost/x"), "private_hostname"),
        (lambda r: r["reference_sources"][0].update(url="https://10.0.0.8/x"), "non_public_ip"),
        (
            lambda r: r["reference_sources"][0].update(url="https://169.254.169.254/latest"),
            "non_public_ip",
        ),
        (lambda r: r["reference_sources"][0].update(url="file:///etc/passwd"), "scheme_not_http"),
        (lambda r: r.update(evidence_status="done"), "evidence_status must be one of"),
    ],
)
def test_record_rules(tmp_path: Path, mutate: Any, expected: str) -> None:
    rows = _candidate_rows()
    mutate(rows[4])  # Q05, the reviewed row
    msgs = _messages(_write(tmp_path / "d.jsonl", rows))
    assert any(expected in m for m in msgs), msgs


def test_live_run_rules_reject_stale_and_future_evidence(tmp_path: Path) -> None:
    row = json.loads(PILOT.read_text(encoding="utf-8"))
    row["reference_sources"][0]["retrieved_at_utc"] = "2020-01-01T00:00:00Z"
    path = _write(tmp_path / "old.jsonl", [row])
    assert any("older than 30 days" in m for m in _messages(path, allow_subset=True, for_run=True))
    row["reference_sources"][0]["retrieved_at_utc"] = "2999-01-01T00:00:00Z"
    path = _write(tmp_path / "future.jsonl", [row])
    assert any("in the future" in m for m in _messages(path, allow_subset=True, for_run=True))


def test_validate_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["validate", "--dataset", str(CANDIDATE)]) == 0
    assert "1 reviewed, 19 pending" in capsys.readouterr().out
    assert cli.main(["validate", "--dataset", str(CANDIDATE), "--for-run"]) == 1
    assert "INVALID: 19 issue(s)" in capsys.readouterr().err


def test_freeze_refuses_pending_rows_and_existing_freeze(tmp_path: Path, capsys) -> None:
    rows = _candidate_rows()
    path = _write(tmp_path / "c.jsonl", rows)
    assert cli.main(["freeze", "--dataset", str(path), "--version", "v1"]) == 1
    assert "not frozen: 19 issue(s)" in capsys.readouterr().err
    assert not (tmp_path / "c.jsonl.freeze.json").exists()
    synth = write_synthetic_dataset(tmp_path / "s")
    with pytest.raises(FileExistsError):
        freeze_dataset(load_dataset(synth), "v2", True, 30)


# --- run-one ---------------------------------------------------------------------------------


@pytest.fixture
def synth_dataset(tmp_path: Path) -> Path:
    return write_synthetic_dataset(tmp_path / "data")


def _attempts(run: Path) -> List[Dict[str, Any]]:
    return read_attempts(run)


def test_success_with_sources_records_everything(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    assert attempt(synth_dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl")) == 0
    (a,) = _attempts(run)
    assert set(ATTEMPT_COLUMNS) <= set(a)
    assert a["attempt_id"] == "Q01-a1" and a["terminal_status"] == "complete"
    assert a["failure_class"] == "completed" and a["exit_code"] == 0
    assert a["sdk_max_retries"] == 0 and a["application_retries"] == 0
    assert a["dataset_version"] == "synthetic-v1" and len(a["dataset_sha256"]) == 64
    assert a["source_count"] == 3
    assert a["source_urls_in_order"] == [
        "https://zeta.example/one",
        "https://alpha.example/two",
        "https://mid.example/three",
    ]
    assert a["usage_status"] == "unavailable" and a["usage_value"] is None
    assert "usage unavailable" in a["missing_data"]
    assert a["review_status"] == "unreviewed" and a["synthetic"] is True
    assert isinstance(a["first_event_ms"], int) and isinstance(a["terminal_elapsed_ms"], int)
    assert (run / a["report_path"]).exists() and (run / a["sources_path"]).exists()
    m = json.loads((run / "manifest.json").read_text())
    assert m["config"]["sdk_max_retries"] == 0 and m["config"]["synthetic"] is True
    assert m["intended_question_ids"] == ["Q01", "Q02", "Q03"]
    # The provider gets the question and the neutral instruction only.
    sent = (run / a["answer_dir"] / "question.txt").read_text()
    assert "Example Widgets API list" in sent and m["config"]["output_instruction"] in sent
    for leaked in ("Name the documented field", "Invents a field", "Synthetic passage"):
        assert leaked not in sent


def test_query_sent_to_the_client_has_no_reviewer_fields(
    tmp_path: Path, synth_dataset: Path
) -> None:
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    attempt(synth_dataset, tmp_path / "run", "Q01", lambda: client)
    (call,) = client.agent.calls
    assert set(call) == {"query", "mode", "nocache"}
    assert "criterion" not in call["query"] and "Invents" not in call["query"]


def test_success_without_sources(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    assert attempt(synth_dataset, run, "Q02", fixture_client("complete-no-cited-pages.jsonl")) == 0
    (a,) = _attempts(run)
    assert a["terminal_status"] == "complete" and a["source_count"] == 0
    assert "complete carried no cited pages" in a["missing_data"]


def _status_error(code: int) -> tabstack.APIStatusError:
    req = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
    return tabstack.APIStatusError(
        "Unauthorized", response=httpx.Response(code, request=req), body=None
    )


@pytest.mark.parametrize(
    "factory, code, status, failure_class, task_state",
    [
        (
            lambda: fixture_client("error-events.jsonl"),
            2,
            "task_error",
            "stream_error_event",
            "terminal_event_received",
        ),
        (
            lambda: raising_client(_status_error(429)),
            3,
            "http_error",
            "http_rejection",
            "rejected_before_stream",
        ),
        (
            lambda: raising_client(
                tabstack.APIConnectionError(request=httpx.Request("POST", "https://x"))
            ),
            4,
            "transport_error",
            "transport_error_before_stream",
            "unknown_connection_failed_before_stream",
        ),
        (
            lambda: fixture_client("truncated-events.jsonl"),
            6,
            "premature_close",
            "missing_terminal_event",
            "unknown",
        ),
        (
            lambda: fixture_client("duplicate-complete.jsonl"),
            8,
            "protocol_error",
            "duplicate_terminal_event",
            "unknown",
        ),
    ],
)
def test_failures_are_recorded_not_retried(
    tmp_path: Path,
    synth_dataset: Path,
    factory: Any,
    code: int,
    status: str,
    failure_class: str,
    task_state: str,
) -> None:
    run = tmp_path / "run"
    made = factory()
    assert attempt(synth_dataset, run, "Q03", made) == code
    assert len(made().agent.calls) == 1, "one request, no retry"
    (a,) = _attempts(run)
    assert (a["terminal_status"], a["failure_class"]) == (status, failure_class)
    assert a["provider_task_state"] == task_state and a["exit_code"] == code
    assert a["error"] and a["client_stopped_waiting"] is False
    # Partial artifacts are preserved.
    answer = run / a["answer_dir"]
    assert (answer / "events.sanitized.jsonl").exists() and (answer / "run-manifest.json").exists()
    assert a["source_count"] is None and a["review_status"] == "not_applicable"


def test_transport_interruption_mid_stream(tmp_path: Path, synth_dataset: Path) -> None:
    events = load_events("truncated-events.jsonl")

    def breaks() -> Iterator[Any]:
        yield from events
        raise httpx.RemoteProtocolError("peer closed connection")

    client = ZeroRetryFake(breaks)
    assert attempt(synth_dataset, tmp_path / "run", "Q01", lambda: client) == 9
    (a,) = _attempts(tmp_path / "run")
    assert a["failure_class"] == "transport_error_mid_stream"
    assert a["client_stopped_waiting"] is True and a["provider_task_state"] == "unknown"
    assert "provider cancellation not established" in " ".join(a["missing_data"])
    log = (tmp_path / "run" / a["events_path"]).read_text().splitlines()
    assert [json.loads(x)["event"] for x in log] == ["start", "searching:start"]


def test_silence_timeout_is_not_cancellation(tmp_path: Path, synth_dataset: Path) -> None:
    release = threading.Event()
    first = load_events("truncated-events.jsonl")[0]

    def stalls() -> Iterator[Any]:
        yield first
        release.wait(10)

    client = ZeroRetryFake(stalls)
    try:
        code = attempt(synth_dataset, tmp_path / "run", "Q01", lambda: client, silence_timeout=0.2)
    finally:
        release.set()
    assert code == 7
    (a,) = _attempts(tmp_path / "run")
    assert a["failure_class"] == "client_timeout_silence"
    assert a["provider_task_state"] == "unknown" and a["client_stopped_waiting"] is True
    assert "cancel" not in a["terminal_status"]
    assert "may still be running" in a["error"]


def test_overall_deadline_stops_a_healthy_stream(tmp_path: Path, synth_dataset: Path) -> None:
    release = threading.Event()
    progress = load_events("truncated-events.jsonl")[1]

    def keeps_talking() -> Iterator[Any]:
        while not release.is_set():
            yield progress
            release.wait(0.05)

    client = ZeroRetryFake(keeps_talking)
    try:
        code = attempt(
            synth_dataset,
            tmp_path / "run",
            "Q01",
            lambda: client,
            silence_timeout=5,
            deadline=0.3,
        )
    finally:
        release.set()
    assert code == 12
    (a,) = _attempts(tmp_path / "run")
    assert a["failure_class"] == "client_timeout_deadline" and a["deadline_seconds"] == 0.3
    assert a["provider_task_state"] == "unknown"
    assert a["terminal_elapsed_ms"] >= 300


def test_citation_order_is_kept_with_duplicate_urls(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-malformed-sources.jsonl"))
    (a,) = _attempts(run)
    sources = json.loads((run / a["sources_path"]).read_text())
    assert [s["position"] for s in sources] == list(range(1, 10))
    assert sources[1]["duplicate_of_position"] == 1, "duplicate flagged, not removed"
    assert len(a["source_urls_in_order"]) == 9
    assert a["source_urls_in_order"][:2] == ["https://example.org/a", "http://example.org/a/"]
    prepare_review(run)
    rows = read_review_sheet(run / "reviews" / "claims.csv")
    assert any("likely same page as cited page 1" in r["auto_flags"] for r in rows)


def test_rerun_never_reuses_or_overwrites_an_attempt(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    first = (run / "answers" / "Q01-a1" / "report.md").read_bytes()
    with pytest.raises(RunRefused, match="already has attempt"):
        attempt(synth_dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl"))
    assert len(_attempts(run)) == 1
    attempt(synth_dataset, run, "Q01", fixture_client("error-events.jsonl"), another=True)
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1", "Q01-a2"]
    assert (run / "answers" / "Q01-a1" / "report.md").read_bytes() == first
    # An answers/ directory without a ledger line still blocks its id.
    (run / "answers" / "Q01-a3").mkdir()
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"), another=True)
    assert _attempts(run)[-1]["attempt_id"] == "Q01-a4"


def test_run_refuses_edited_unfrozen_or_mismatched(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    with pytest.raises(RunRefused, match="different configuration"):
        attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"), mode="balanced")
    with pytest.raises(RunRefused, match="not in the frozen dataset"):
        attempt(synth_dataset, tmp_path / "r2", "Q09", fixture_client("complete-events.jsonl"))
    synth_dataset.write_text(synth_dataset.read_text() + "\n", encoding="utf-8")
    with pytest.raises(RunRefused, match="does not match the freeze record"):
        attempt(synth_dataset, tmp_path / "r3", "Q01", fixture_client("complete-events.jsonl"))
    unfrozen = _write(tmp_path / "u.jsonl", synthetic_questions("2026-10-06T00:00:00Z"))
    with pytest.raises(FileNotFoundError, match="freeze the dataset first"):
        attempt(unfrozen, tmp_path / "r4", "Q01", fixture_client("complete-events.jsonl"))


def test_run_one_cli_needs_env_key_and_takes_no_key_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("TABSTACK_API_KEY", raising=False)
    run = tmp_path / "run"
    argv = ["run-one", "--dataset", str(PILOT), "--question", "Q05", "--run", str(run)]
    assert cli.main(argv) == 5
    assert "never accepts it as a flag" in capsys.readouterr().err
    assert not run.exists()
    with pytest.raises(SystemExit):
        cli.main([*argv, "--api-key", "x"])
    help_text = cli.build_parser().format_help()
    assert "key" not in help_text.lower()


def test_run_one_cli_passes_safe_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_not_a_real_key_000000")
    seen: Dict[str, Any] = {}
    monkeypatch.setattr(cli, "run_one", lambda **kw: seen.update(kw) or 0)
    argv = ["run-one", "--dataset", str(PILOT), "--question", "Q05", "--run", str(tmp_path / "r")]
    assert cli.main([*argv, "--pilot", "--nocache", "--silence-timeout", "120"]) == 0
    assert seen["pilot_only"] is True and seen["another_attempt"] is False
    assert seen["mode"] == "fast" and seen["silence_timeout"] == 120.0
    assert "synthetic" not in seen and "client_factory" not in seen


# --- review sheets ---------------------------------------------------------------------------


def test_templates_match_generated_columns() -> None:
    def header(name: str) -> List[str]:
        with (KIT / name).open(encoding="utf-8-sig") as fh:
            return next(csv.reader(fh))

    assert header("coverage-review-template.csv") == list(COVERAGE_COLUMNS)
    assert header("claim-review-template.csv") == list(CLAIM_COLUMNS)
    assert header("attempts-template.csv") == list(ATTEMPT_COLUMNS)
    assert header("release-review-template.csv") == list(RELEASE_COLUMNS)
    with (KIT / "release-review-template.csv").open(encoding="utf-8") as fh:
        gates = [r["gate"] for r in csv.DictReader(fh)]
    assert gates == [g for g, _, _ in RELEASE_GATES] and len(gates) == 7


def test_prepare_review_writes_blank_sheets(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl"))
    attempt(synth_dataset, run, "Q02", fixture_client("error-events.jsonl"))
    result = prepare_review(run)
    assert result["coverage.csv"] == (0, 2) and result["claims.csv"] == (0, 4)
    assert result["decisions.csv"] == (0, 1) and result["usage.csv"] == (0, 2)
    cov = read_review_sheet(run / "reviews" / "coverage.csv")
    assert [(r["attempt_id"], r["element_id"]) for r in cov] == [("Q01-a1", "E1"), ("Q01-a1", "E2")]
    assert all(r["score"] == "" and r["answer_excerpt"] == "" for r in cov)
    claims = read_review_sheet(run / "reviews" / "claims.csv")
    assert [r["citation_present"] for r in claims] == ["yes", "yes", "no", "yes"]
    assert all(r["support"] == "" for r in claims)
    dec = read_review_sheet(run / "reviews" / "decisions.csv")
    assert dec[0]["critical_failure_conditions"] == "CF1: Invents a field."
    usage = read_review_sheet(run / "reviews" / "usage.csv")
    assert [r["terminal_status"] for r in usage] == ["complete", "task_error"]


def test_prepare_review_never_overwrites_completed_reviews(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl"))
    prepare_review(run)
    fill(run, "coverage.csv", {"element_id": "E1"}, {"score": "2", "reason": "kept"})
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    result = prepare_review(run)
    assert result["coverage.csv"] == (2, 2)
    cov = read_review_sheet(run / "reviews" / "coverage.csv")
    assert cov[0]["score"] == "2" and cov[0]["reason"] == "kept"
    assert prepare_review(run)["coverage.csv"] == (4, 0)
    assert cli.main(["prepare-review", "--run", str(run)]) == 0
    assert read_review_sheet(run / "reviews" / "coverage.csv")[0]["score"] == "2"
    assert cli.main(["prepare-review", "--run", str(run), "--force"]) == 0
    assert read_review_sheet(run / "reviews" / "coverage.csv")[0]["score"] == ""


# --- summary ---------------------------------------------------------------------------------


@pytest.fixture
def synth_run(tmp_path: Path) -> Path:
    return build_synthetic_run(tmp_path / "ex")


def test_summary_counts_each_score_state(synth_run: Path) -> None:
    b = summarize(synth_run)["scopes"]["synthetic"]
    assert b["coverage"] == {
        "full": 2,
        "partial": 1,
        "missing": 1,
        "U": 1,
        "blank": 1,
        "inspected": 4,
        "full_rate": {"status": "available", "numerator": 2, "denominator": 4, "value": 0.5},
    }
    cl = b["claims"]
    assert (cl["supported"], cl["partial"], cl["unsupported"], cl["U"], cl["blank"]) == (
        2,
        1,
        1,
        0,
        1,
    )
    assert (
        cl["inspected"] == 4 and cl["enumeration_locked"] == 2 and cl["enumeration_unlocked"] == 1
    )


def test_failures_stay_in_the_denominator(synth_run: Path) -> None:
    s = summarize(synth_run)
    b = s["scopes"]["synthetic"]
    assert s["attempts"]["total"] == 6 and b["attempts"] == 6
    assert b["by_terminal_status"] == {
        "complete": 3,
        "task_error": 1,
        "http_error": 1,
        "premature_close": 1,
    }
    assert b["accepted_over_attempts"]["denominator"] == 6
    assert b["failure_classes"] == [
        "http_rejection",
        "missing_terminal_event",
        "stream_error_event",
    ]


def test_acceptance_is_not_transport_completion(synth_run: Path) -> None:
    r = summarize(synth_run)["scopes"]["synthetic"]["responses"]
    assert r["completed"] == 3
    assert (r["fully_reviewed"], r["accepted"], r["rejected"], r["unreviewed_or_undecided"]) == (
        2,
        1,
        1,
        1,
    )
    assert "Q01-a2" in r["open_review_items"]


def test_critical_failures_by_question(synth_run: Path) -> None:
    b = summarize(synth_run)["scopes"]["synthetic"]
    assert b["critical_failures"] == [
        {"question_id": "Q02", "attempt_id": "Q02-a1", "id": "CF1", "condition": "Invents a limit."}
    ]
    assert b["critical_failure_check_missing"] == ["Q01-a2"]


def test_accept_with_a_triggered_critical_failure_is_a_conflict(synth_run: Path) -> None:
    fill(
        synth_run, "decisions.csv", {"attempt_id": "Q01-a1"}, {"critical_failures_triggered": "CF1"}
    )
    r = summarize(synth_run)["scopes"]["synthetic"]["responses"]
    assert r["accepted"] == 0
    assert "critical failure" in r["decision_conflicts"]["Q01-a1"]


def test_missing_cost_and_missing_latency_are_independent(synth_run: Path) -> None:
    b = summarize(synth_run)["scopes"]["synthetic"]
    timing = {t["attempt_id"]: t for t in b["timing"]}
    known = {u["attempt_id"] for u in b["usage"]["known"]}
    # Q02-a1: latency measured, usage missing.
    assert timing["Q02-a1"]["first_event_ms"] is not None and "Q02-a1" in b["usage"]["missing"]
    # Q03-a2 (HTTP 401): usage known (0 credits, not blank), first event not measured.
    assert timing["Q03-a2"]["first_event_ms"] is None and "Q03-a2" in known
    assert b["usage"]["missing_count"] == 4
    cost = b["cost_per_accepted_answer"]
    assert cost["status"] == "unavailable"
    assert "usage missing for 4 of 6 attempts" in cost["reasons"]


def test_cost_ratio_only_for_a_complete_batch(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    attempt(synth_dataset, run, "Q02", raising_client(_status_error(500)))
    prepare_review(run)
    fill(run, "coverage.csv", {}, {"score": "2"})
    fill(run, "claims.csv", {}, {"support": "2"})
    fill(
        run,
        "decisions.csv",
        {},
        {
            "critical_failures_triggered": "none",
            "claim_enumeration_locked": "yes",
            "decision": "accept",
        },
    )
    fill(
        run,
        "usage.csv",
        {"attempt_id": "Q01-a1"},
        {"usage_value": "4", "usage_unit": "credits", "usage_receipt_ref": "r1"},
    )
    assert (
        summarize(run)["scopes"]["synthetic"]["cost_per_accepted_answer"]["status"] == "unavailable"
    )
    fill(
        run,
        "usage.csv",
        {"attempt_id": "Q02-a1"},
        {"usage_value": "2", "usage_unit": "credits", "usage_receipt_ref": "r2"},
    )
    cost = summarize(run)["scopes"]["synthetic"]["cost_per_accepted_answer"]
    assert cost == {
        "status": "available",
        "total_usage": 6.0,
        "unit": "credits",
        "accepted": 1,
        "value": 6.0,
        "covers_attempts": 2,
    }


def test_no_inspection_means_unavailable_not_zero(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    prepare_review(run)
    fill(run, "coverage.csv", {}, {"score": "U"})
    b = summarize(run)["scopes"]["synthetic"]
    assert b["coverage"]["full_rate"] == {"status": "unavailable", "reason": "nothing inspected"}
    assert b["claims"]["supported_rate"]["status"] == "unavailable"
    assert b["coverage"]["U"] == 2 and b["coverage"]["inspected"] == 0
    reasons = b["cost_per_accepted_answer"]["reasons"]
    assert "no accepted answers" in reasons


def test_pilot_and_synthetic_are_excluded_from_live_totals(
    tmp_path: Path, synth_dataset: Path
) -> None:
    pilot_run = tmp_path / "pilot"
    attempt(
        synth_dataset,
        pilot_run,
        "Q01",
        fixture_client("complete-events.jsonl"),
        synthetic=False,
        pilot=True,
    )
    s = summarize(pilot_run)
    assert s["provenance"] == {"live_scored": 0, "pilot": 1, "synthetic": 0}
    assert s["scopes"]["live_scored"]["attempts"] == 0
    assert s["scopes"]["live_scored"]["accepted_over_attempts"]["status"] == "unavailable"
    assert s["scopes"]["pilot"]["responses"]["completed"] == 1
    synth = build_synthetic_run(tmp_path / "ex")
    assert summarize(synth)["scopes"]["live_scored"]["attempts"] == 0


def test_invalid_review_values_are_refused(synth_run: Path, capsys) -> None:
    fill(synth_run, "coverage.csv", {"attempt_id": "Q01-a1", "element_id": "E1"}, {"score": "3"})
    fill(
        synth_run, "decisions.csv", {"attempt_id": "Q01-a1"}, {"critical_failures_triggered": "CF9"}
    )
    with pytest.raises(SheetError):
        summarize(synth_run)
    assert cli.main(["summarize", "--run", str(synth_run)]) == 1
    err = capsys.readouterr().err
    assert "score '3'" in err and "CF9" in err


def test_summarize_cli_writes_summary_json(synth_run: Path, capsys) -> None:
    assert cli.main(["summarize", "--run", str(synth_run)]) == 0
    out = capsys.readouterr().out
    assert "6 synthetic attempts" in out and "cost per accepted answer: unavailable" in out
    assert json.loads((synth_run / "summary.json").read_text())["provenance"]["synthetic"] == 6


# --- committed artifacts ---------------------------------------------------------------------

SECRET_SHAPES = re.compile(
    r"(sk_(live|test)_[A-Za-z0-9]{8,}|tsk[-_][A-Za-z0-9]{12,}|Bearer\s+[A-Za-z0-9._-]{8,}|"
    r"Authorization:\s*\S|Cookie:\s*\S|TABSTACK_API_KEY=(?!\.\.\.)\S)"
)


def _artifact_files() -> List[Path]:
    return [
        p
        for p in KIT.rglob("*")
        if p.is_file() and p.suffix in {".json", ".jsonl", ".csv", ".md", ".txt"}
    ]


def test_committed_kit_files_carry_no_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    files = _artifact_files()
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8-sig")
        assert not SECRET_SHAPES.search(text), path


def test_synthetic_outputs_carry_no_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sk_live_acceptance_secret_0123456789"
    monkeypatch.setenv("TABSTACK_API_KEY", secret)
    run = build_synthetic_run(tmp_path / "ex")
    summarize(run)
    for path in run.rglob("*"):
        if path.is_file():
            text = path.read_text(encoding="utf-8-sig")
            assert secret not in text and not SECRET_SHAPES.search(text), path


def test_committed_synthetic_example_summary() -> None:
    run = KIT / "examples" / "synthetic-run"
    if not run.exists():
        pytest.skip("synthetic example not generated")
    s = summarize(run)
    assert s["provenance"] == {"live_scored": 0, "pilot": 0, "synthetic": 6}
    assert s["scopes"]["synthetic"]["responses"]["accepted"] == 1
    assert all(a["synthetic"] for a in read_attempts(run))


def test_a_crash_mid_request_leaves_a_visible_attempt(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    def dies(**_: Any) -> int:
        raise KeyboardInterrupt

    monkeypatch.setattr(run_mod, "run_research", dies)
    run = tmp_path / "run"
    with pytest.raises(KeyboardInterrupt):
        attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (a,) = _attempts(run)
    # Interrupted mid-request: recorded as outcome_unrecorded (not silently in_progress), then
    # the interrupt is re-raised. Only a hard kill leaves the pre-request in_progress line.
    assert a["terminal_status"] == "outcome_unrecorded"
    assert a["failure_class"] == "post_request_recording_failed"
    assert "the request may have been sent" in a["error"]
    assert (run / "answers" / "Q01-a1" / "attempt.json").exists()
    b = summarize(run)["scopes"]["synthetic"]
    assert b["attempts"] == 1 and b["by_terminal_status"] == {"outcome_unrecorded": 1}
    assert b["accepted_over_attempts"]["denominator"] == 1


# --- review fixes (2026-10-06 code review) ----------------------------------------------------


def test_summarize_refuses_an_edited_dataset(synth_run: Path, capsys) -> None:
    dataset = synth_run.parent / "synthetic-dataset.jsonl"
    dataset.write_text(dataset.read_text() + "\n", encoding="utf-8")
    with pytest.raises(SheetError, match="dataset_sha256"):
        summarize(synth_run)
    assert cli.main(["summarize", "--run", str(synth_run)]) == 1
    assert "frozen set was edited" in capsys.readouterr().err


@pytest.mark.parametrize(
    "sheet, match",
    [
        ("coverage.csv", {"attempt_id": "Q01-a1", "element_id": "E1"}),
        ("claims.csv", {"attempt_id": "Q01-a1", "claim_id": "C01"}),
        ("decisions.csv", {"attempt_id": "Q01-a1"}),
        ("usage.csv", {"attempt_id": "Q01-a1"}),
    ],
)
def test_duplicate_review_rows_are_errors(
    synth_run: Path, sheet: str, match: Dict[str, str]
) -> None:
    from cited_research.accept.reviews import SHEETS, sheet_csv
    from cited_research.review import write_review_sheet

    path = synth_run / "reviews" / sheet
    rows = read_review_sheet(path)
    dup = next(r for r in rows if all(r.get(k) == v for k, v in match.items()))
    write_review_sheet(path, sheet_csv(SHEETS[sheet], [*rows, dict(dup)]))
    with pytest.raises(SheetError, match="duplicate row"):
        summarize(synth_run)


@pytest.mark.parametrize("value", ["nan", "inf", "-inf", "1e999"])
def test_non_finite_usage_is_refused(synth_run: Path, value: str) -> None:
    fill(synth_run, "usage.csv", {"attempt_id": "Q01-a1"}, {"usage_value": value})
    with pytest.raises(SheetError, match="finite non-negative|is not a number"):
        summarize(synth_run)


def test_prepare_review_appends_without_rewriting_existing_bytes(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl"))
    prepare_review(run)
    cov = run / "reviews" / "coverage.csv"
    # A reviewer's spreadsheet: semicolon delimiter, CRLF, no BOM, an extra column, reordered
    # header, and a cell that _csv_safe would have prefixed.
    original = (
        b"attempt_id;run_id;question_id;element_id;criterion;answer_excerpt;score;reason;"
        b"reviewer;reviewed_at_utc;my_note\r\n"
        b"Q01-a1;run;Q01;E1;crit;=SUM(A1);2;ok;me;2026-10-06T00:00:00Z;keep me\r\n"
        b"Q01-a1;run;Q01;E2;crit;;;;;;\r\n"
    )
    cov.write_bytes(original)
    assert prepare_review(run)["coverage.csv"] == (2, 0)
    assert cov.read_bytes() == original, "nothing added, nothing rewritten"
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    assert prepare_review(run)["coverage.csv"] == (2, 2)
    after = cov.read_bytes()
    assert after.startswith(original)
    added = after[len(original) :].decode("utf-8").split("\r\n")
    assert added[0].startswith("Q02-a1;run;Q02;E1;") and added[0].endswith(";")
    assert len([x for x in added if x]) == 2


def test_prepare_review_refuses_a_sheet_missing_columns(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    prepare_review(run)
    (run / "reviews" / "coverage.csv").write_text("attempt_id,score\nQ01-a1,2\n", encoding="utf-8")
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    with pytest.raises(ValueError, match="has no column"):
        prepare_review(run)
    assert (run / "reviews" / "coverage.csv").read_text() == "attempt_id,score\nQ01-a1,2\n"


def test_prepare_review_checks_the_hash_before_reading_attempts(
    tmp_path: Path, synth_dataset: Path, capsys
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    rows = [json.loads(x) for x in synth_dataset.read_text().splitlines()]
    _write(synth_dataset, [r for r in rows if r["id"] != "Q01"])  # would KeyError in rows
    assert cli.main(["prepare-review", "--run", str(run)]) == 1
    assert "does not match the dataset hash" in capsys.readouterr().err


def test_final_ledger_write_keeps_attempts_added_during_the_request(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    real = run_mod.run_research
    run = tmp_path / "run"

    def concurrent(**kw: Any) -> int:
        # Another process records an attempt while this request is in flight.
        ledger = run / "attempts.jsonl"
        other = {
            "attempt_id": "Q02-a1",
            "question_id": "Q02",
            "run_id": "run",
            "terminal_status": "in_progress",
            "failure_class": "no_terminal_record",
            "answer_dir": "answers/Q02-a1",
        }
        ledger.write_text(ledger.read_text() + json.dumps(other) + "\n")
        return real(**kw)

    monkeypatch.setattr(run_mod, "run_research", concurrent)
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    ids = [a["attempt_id"] for a in _attempts(run)]
    assert ids == ["Q01-a1", "Q02-a1"]
    assert _attempts(run)[0]["terminal_status"] == "complete"


@pytest.mark.parametrize(
    "url, issue",
    [
        ("https://router.home.arpa/", "private_hostname"),
        ("https://[64:ff9b::a00:1]/", "non_public_ip"),
        ("https://[64:ff9b::808:808]/", "non_public_ip"),
    ],
)
def test_more_private_destinations_are_refused(url: str, issue: str) -> None:
    from cited_research.urls import check_public_url

    assert check_public_url(url) == (False, issue)


# --- review fixes, round 2 (PR #3 review) -----------------------------------------------------


@pytest.mark.parametrize("silence", [None, 200.0])
def test_capped_wait_that_returns_early_is_a_deadline_hit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, silence: Any
) -> None:
    """queue.get and perf_counter use different clocks; on coarse clocks an Empty can come back
    before perf_counter says the deadline passed. That must still be deadline_exceeded."""
    import queue as queue_mod

    import cited_research.tabstack_runner as runner
    from cited_research.tabstack_runner import run_research

    release = threading.Event()

    def stalls() -> Iterator[Any]:
        release.wait(10)
        return iter(())

    def early_empty(self: Any, timeout: Any) -> Any:
        raise queue_mod.Empty  # returns "too early", before the deadline by perf_counter

    monkeypatch.setattr(runner.EventPump, "get", early_empty)
    client = ZeroRetryFake(lambda: stalls())
    try:
        code = run_research(
            "q",
            "fast",
            True,
            None,
            tmp_path,
            quiet=True,
            client_factory=lambda: client,  # pyright: ignore[reportArgumentType]
            silence_timeout=silence,
            deadline=100.0,
        )
    finally:
        release.set()
    m = json.loads((tmp_path / "run-manifest.json").read_text())
    assert (code, m["terminal_status"]) == (12, "deadline_exceeded")
    assert "None" not in m["error"]


@pytest.mark.parametrize(
    "flag, value",
    [
        ("--deadline", "nan"),
        ("--deadline", "inf"),
        ("--deadline", "-inf"),
        ("--deadline", "0"),
        ("--silence-timeout", "nan"),
        ("--silence-timeout", "inf"),
        ("--fetch-timeout", "0"),
        ("--fetch-timeout", "-5"),
        ("--fetch-timeout", "nan"),
    ],
)
def test_run_one_rejects_non_finite_or_non_positive_flags(
    tmp_path: Path, flag: str, value: str, capsys
) -> None:
    argv = ["run-one", "--dataset", str(PILOT), "--question", "Q05", "--run", str(tmp_path / "r")]
    with pytest.raises(SystemExit):
        cli.main([*argv, f"{flag}={value}"])
    err = capsys.readouterr().err
    assert "greater than 0" in err or "not a whole number" in err
    assert not (tmp_path / "r").exists()


@pytest.mark.parametrize("flag, value", [("--silence-timeout", "nan"), ("--fetch-timeout", "0")])
def test_trace_cli_rejects_the_same_flags(flag: str, value: str, capsys) -> None:
    from cited_research.cli import main as trace_main

    with pytest.raises(SystemExit):
        trace_main(["--query", "q", "--output", "x", flag, value])
    assert "greater than 0" in capsys.readouterr().err


def test_max_evidence_age_must_be_positive(capsys) -> None:
    with pytest.raises(SystemExit):
        cli.main(["validate", "--dataset", str(CANDIDATE), "--max-evidence-age-days", "-1"])
    assert "greater than 0" in capsys.readouterr().err


def _fake_package(root: Path) -> Path:
    pkg = root / "src" / "cited_research"
    (pkg / "accept").mkdir(parents=True)
    (pkg / "__init__.py").write_text("x = 1\n")
    (pkg / "accept" / "run.py").write_text("y = 2\n")
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "uv.lock").write_text("lock\n")
    (root / "README.md").write_text("docs\n")
    return pkg


def test_implementation_identity_follows_code_content_only(tmp_path: Path) -> None:
    from cited_research.accept.run import implementation_identity

    pkg = _fake_package(tmp_path)
    base, paths = implementation_identity(pkg)
    assert paths == [
        "cited_research/__init__.py",
        "cited_research/accept/run.py",
        "pyproject.toml",
        "uv.lock",
    ]
    # Non-implementation changes (docs, artifacts, a new commit) and bytecode do not count.
    (tmp_path / "README.md").write_text("more docs\n")
    (tmp_path / "acceptance-evals").mkdir()
    (tmp_path / "acceptance-evals" / "x.json").write_text("{}")
    (pkg / "__pycache__").mkdir()
    (pkg / "__pycache__" / "run.cpython-312.pyc").write_bytes(b"\0")
    assert implementation_identity(pkg)[0] == base
    # Two different uncommitted edits differ from each other and from the base.
    (pkg / "accept" / "run.py").write_text("y = 3\n")
    edit_a = implementation_identity(pkg)[0]
    (pkg / "accept" / "run.py").write_text("y = 4\n")
    edit_b = implementation_identity(pkg)[0]
    assert len({base, edit_a, edit_b}) == 3
    (pkg / "accept" / "run.py").write_text("y = 2\n")
    assert implementation_identity(pkg)[0] == base, "same content, same identity"
    # Untracked files under the package and lockfile changes count.
    (pkg / "accept" / "new.py").write_text("z = 1\n")
    assert implementation_identity(pkg)[0] != base
    (pkg / "accept" / "new.py").unlink()
    (tmp_path / "uv.lock").write_text("lock2\n")
    assert implementation_identity(pkg)[0] != base


def test_implementation_identity_unreadable_file_is_an_error(tmp_path: Path) -> None:
    from cited_research.accept.run import ImplementationUnknown, implementation_identity

    pkg = _fake_package(tmp_path)
    (pkg / "accept" / "run.py").chmod(0)
    try:
        with pytest.raises(ImplementationUnknown, match="cannot read"):
            implementation_identity(pkg)
    finally:
        (pkg / "accept" / "run.py").chmod(0o644)


def test_run_compares_implementation_content_not_head(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    run = tmp_path / "run"
    ident = {"sha": "1" * 64}
    monkeypatch.setattr(run_mod, "implementation_identity", lambda: (ident["sha"], ["p"]))
    monkeypatch.setattr(run_mod, "_git_commit", lambda: "a" * 40)
    monkeypatch.setattr(run_mod, "_implementation_dirty", lambda: False)
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    first = _attempts(run)[0]
    assert first["implementation_sha256"] == "1" * 64
    assert first["implementation_commit"] == "a" * 40
    # A docs-only commit moves HEAD; the implementation content is the same: allowed.
    monkeypatch.setattr(run_mod, "_git_commit", lambda: "b" * 40)
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    assert _attempts(run)[1]["implementation_commit"] == "b" * 40, "HEAD recorded per attempt"
    # Different code (committed or not): refused, nothing reserved.
    ident["sha"] = "2" * 64
    with pytest.raises(RunRefused, match="same implementation"):
        attempt(synth_dataset, run, "Q03", fixture_client("complete-events.jsonl"))
    assert len(_attempts(run)) == 2 and not (run / "answers" / "Q03-a1").exists()


def test_git_failure_is_provenance_only_never_a_silent_pass(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    run = tmp_path / "run"
    monkeypatch.setattr(run_mod, "implementation_identity", lambda: ("1" * 64, ["p"]))
    monkeypatch.setattr(run_mod, "_git_commit", lambda: None)  # git error or 5 s timeout
    monkeypatch.setattr(run_mod, "_implementation_dirty", lambda: None)
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    a = _attempts(run)[0]
    assert a["implementation_commit"] is None and a["implementation_dirty"] is None
    assert any("git failed" in m for m in a["missing_data"])
    # The comparison never relies on git: different code is still refused with git down.
    monkeypatch.setattr(run_mod, "implementation_identity", lambda: ("2" * 64, ["p"]))
    with pytest.raises(RunRefused, match="same implementation"):
        attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))


def test_unreadable_implementation_refuses_before_any_request(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    def broken() -> Any:
        raise run_mod.ImplementationUnknown("cannot read implementation file: denied")

    monkeypatch.setattr(run_mod, "implementation_identity", broken)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    with pytest.raises(RunRefused, match="cannot identify the implementation"):
        attempt(synth_dataset, tmp_path / "run", "Q01", lambda: client)
    assert client.agent.calls == [] and not (tmp_path / "run").exists()


def test_run_dir_without_identity_is_refused(tmp_path: Path, synth_dataset: Path) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    m = json.loads((run / "manifest.json").read_text())
    del m["implementation_sha256"]
    (run / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(RunRefused, match="predates implementation identity"):
        attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))


def test_lock_retry_waits_only_on_contention_and_is_bounded() -> None:
    import errno

    from cited_research.accept.run import LedgerLockError, lock_with_retry

    calls = {"n": 0}

    def busy_twice() -> None:
        calls["n"] += 1
        if calls["n"] <= 2:
            raise OSError(errno.EDEADLK, "Resource deadlock avoided")

    lock_with_retry(busy_twice, max_wait=60, clock=lambda: 0.0)
    assert calls["n"] == 3

    now = {"t": 0.0}

    def clock() -> float:
        now["t"] += 10.0
        return now["t"]

    def always_busy() -> None:
        raise OSError(errno.EACCES, "Permission denied")

    with pytest.raises(LedgerLockError, match="still locked by another run-one after 60s"):
        lock_with_retry(always_busy, max_wait=60, clock=clock)

    tries = {"n": 0}

    def bad_handle() -> None:
        tries["n"] += 1
        raise OSError(errno.EBADF, "Bad file descriptor")

    with pytest.raises(LedgerLockError, match="cannot lock the run ledger"):
        lock_with_retry(bad_handle, max_wait=60, clock=lambda: 0.0)
    assert tries["n"] == 1, "non-contention errors fail at once"


def test_fetch_timeout_help_says_whole_seconds() -> None:
    from cited_research.cli import build_parser as trace_parser

    run_help = " ".join(
        cli.build_parser()._subparsers._group_actions[0].choices["run-one"].format_help().split()  # type: ignore[union-attr]
    )
    assert "whole number of seconds greater than 0" in run_help
    assert "whole number of seconds greater than 0" in " ".join(
        trace_parser().format_help().split()
    )


def test_two_processes_on_one_run_lose_no_attempts(tmp_path: Path, synth_dataset: Path) -> None:
    import multiprocessing

    from acceptance_synth import parallel_worker

    run = tmp_path / "run"
    ctx = multiprocessing.get_context("spawn")
    start = ctx.Event()
    procs = [
        ctx.Process(target=parallel_worker, args=(str(synth_dataset), str(run), q, 4, start))
        for q in ("Q01", "Q02")
    ]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0
    attempts = _attempts(run)
    ids = sorted(a["attempt_id"] for a in attempts)
    assert ids == sorted([f"Q01-a{i}" for i in range(1, 5)] + [f"Q02-a{i}" for i in range(1, 5)])
    assert all(a["terminal_status"] == "complete" for a in attempts)
    assert sorted(p.name for p in (run / "answers").iterdir()) == ids


# --- review fixes, round 4 ---------------------------------------------------------------------


def _git_repo_with_package(root: Path) -> Path:
    import subprocess

    pkg = _fake_package(root)
    (pkg / "models.py").write_text("m = 1\n")

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
            cwd=root,
            check=True,
            capture_output=True,
        )

    git("init", "-q")
    git("add", "-A")
    git("commit", "-q", "-m", "init")
    return pkg


def test_dirty_check_runs_from_the_repo_root(tmp_path: Path) -> None:
    from cited_research.accept.run import _implementation_dirty

    pkg = _git_repo_with_package(tmp_path)
    assert _implementation_dirty(pkg) is False
    (pkg / "models.py").write_text("m = 2\n")
    assert _implementation_dirty(pkg) is True, "edited package file"
    (pkg / "models.py").write_text("m = 1\n")
    assert _implementation_dirty(pkg) is False
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert _implementation_dirty(pkg) is True, "edited root pyproject.toml"
    (tmp_path / "pyproject.toml").write_text("[project]\n")
    (tmp_path / "uv.lock").write_text("lock2\n")
    assert _implementation_dirty(pkg) is True, "edited root uv.lock"
    (tmp_path / "uv.lock").write_text("lock\n")
    (pkg / "accept" / "new.py").write_text("n = 1\n")
    assert _implementation_dirty(pkg) is True, "untracked package file"
    (pkg / "accept" / "new.py").unlink()
    (tmp_path / "README.md").write_text("edited docs\n")
    assert _implementation_dirty(pkg) is False, "docs are not implementation"


def test_dirty_check_is_none_outside_the_packages_own_checkout(tmp_path: Path) -> None:
    from cited_research.accept.run import _implementation_dirty

    pkg = tmp_path / "elsewhere" / "cited_research"
    pkg.mkdir(parents=True)
    assert _implementation_dirty(pkg) is None


@pytest.mark.parametrize(
    "junk",
    [".DS_Store", "accept/.run.py.swp", "accept/run.py~", "accept/run.py.orig", ".hidden/x.py"],
)
def test_identity_ignores_junk_files(tmp_path: Path, junk: str) -> None:
    from cited_research.accept.run import implementation_identity

    pkg = _fake_package(tmp_path)
    base = implementation_identity(pkg)[0]
    (pkg / junk).parent.mkdir(parents=True, exist_ok=True)
    (pkg / junk).write_text("junk\n")
    assert implementation_identity(pkg)[0] == base
    (pkg / "accept" / "run.py").write_text("y = 99\n")
    assert implementation_identity(pkg)[0] != base, "a .py edit still changes it"


def test_every_shipped_package_file_is_hashed() -> None:
    """Fails if the package gains a file type the identity would skip; add it to
    PACKAGE_SUFFIXES or PACKAGE_NAMES deliberately."""
    from cited_research.accept.run import PACKAGE_DIR, implementation_identity, is_package_file

    files = [
        p
        for p in PACKAGE_DIR.rglob("*")
        if p.is_file()
        and "__pycache__" not in p.parts
        and not any(part.startswith(".") for part in p.relative_to(PACKAGE_DIR).parts)
        and p.suffix != ".pyc"
    ]
    assert files and all(is_package_file(p) for p in files), [
        p for p in files if not is_package_file(p)
    ]
    hashed = set(implementation_identity()[1])
    assert {f"cited_research/{p.relative_to(PACKAGE_DIR).as_posix()}" for p in files} <= hashed


def _failing_lock_on(call: int, monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    import cited_research.accept.run as run_mod

    real = run_mod.ledger_lock
    count = {"n": 0}

    @contextlib.contextmanager
    def lock(run_dir: Path) -> Iterator[None]:
        count["n"] += 1
        if count["n"] == call:
            raise run_mod.LedgerLockError("run ledger still locked by another run-one after 60s")
        with real(run_dir):
            yield

    monkeypatch.setattr(run_mod, "ledger_lock", lock)


@pytest.mark.parametrize("call", [1, 2])
def test_lock_failure_before_the_request_sends_nothing(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    _failing_lock_on(call, monkeypatch)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    with pytest.raises(RunRefused, match="nothing was sent"):
        attempt(synth_dataset, run, "Q01", lambda: client)
    assert client.agent.calls == []
    assert not (run / "answers" / "Q01-a1").exists(), "no reserved directory left behind"
    assert _attempts(run) == []


def test_lock_failure_after_the_request_keeps_the_outcome(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import cited_research.accept.run as run_mod

    _failing_lock_on(3, monkeypatch)
    run = tmp_path / "run"
    code = attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    assert code == run_mod.EXIT_LEDGER_NOT_UPDATED == 13
    err = capsys.readouterr().err
    assert "finished with status complete (exit 0)" in err and "could not be updated" in err
    raw = (run / "attempts.jsonl").read_text()
    assert '"terminal_status": "in_progress"' in raw, "the ledger itself was not updated"
    durable = json.loads((run / "answers" / "Q01-a1" / "attempt.json").read_text())
    assert durable["terminal_status"] == "complete"
    (a,) = _attempts(run)
    assert a["terminal_status"] == "complete"
    assert a["ledger_recovered_from"] == "answers/Q01-a1/attempt.json"
    s = summarize(run)
    assert s["ledger_recovered"] == ["Q01-a1"]
    assert s["scopes"]["synthetic"]["by_terminal_status"] == {"complete": 1}
    # The next run-one (lock working again) writes the recovered record into the ledger.
    monkeypatch.undo()
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    lines = [json.loads(x) for x in (run / "attempts.jsonl").read_text().splitlines()]
    assert [x["terminal_status"] for x in lines] == ["complete", "complete"]


def test_cli_turns_a_lock_error_into_a_clean_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import cited_research.accept.run as run_mod

    def raises(**_: Any) -> int:
        raise run_mod.LedgerLockError("cannot lock the run ledger: [Errno 9] Bad file descriptor")

    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_not_a_real_key_000000")
    monkeypatch.setattr(cli, "run_one", raises)
    argv = ["run-one", "--dataset", str(PILOT), "--question", "Q05", "--run", str(tmp_path / "r")]
    assert cli.main(argv) == 1
    assert capsys.readouterr().err.startswith("refused: cannot lock the run ledger")


# --- review fixes, round 5: one boundary between "nothing was sent" and "the request was sent" ---


def _patch_lock_open_error(monkeypatch: pytest.MonkeyPatch, on_call: int, exc: OSError) -> None:
    import cited_research.accept.run as run_mod

    real_acquire = run_mod._acquire
    count = {"n": 0}

    def acquire(fh: Any) -> None:
        count["n"] += 1
        if count["n"] == on_call:
            raise exc
        real_acquire(fh)

    monkeypatch.setattr(run_mod, "_acquire", acquire)


@pytest.mark.parametrize(
    "exc",
    [PermissionError(13, "Permission denied"), OSError(37, "No locks available")],
)
def test_posix_lock_oserrors_become_ledger_lock_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: OSError
) -> None:
    import cited_research.accept.run as run_mod

    _patch_lock_open_error(monkeypatch, 1, exc)
    with pytest.raises(run_mod.LedgerLockError, match="cannot lock the run ledger"):
        with run_mod.ledger_lock(tmp_path / "run"):
            pass


def test_lock_file_open_error_is_a_ledger_lock_error(tmp_path: Path) -> None:
    import cited_research.accept.run as run_mod

    run = tmp_path / "run"
    run.mkdir()
    (run / run_mod.LOCK_NAME).mkdir()  # cannot be opened as a file
    with pytest.raises(run_mod.LedgerLockError, match="cannot open the run ledger lock"):
        with run_mod.ledger_lock(run):
            pass


def test_errors_inside_the_lock_body_are_not_relabelled(tmp_path: Path) -> None:
    import cited_research.accept.run as run_mod

    with pytest.raises(FileNotFoundError):
        with run_mod.ledger_lock(tmp_path / "run"):
            raise FileNotFoundError("body error")


@pytest.mark.parametrize("call", [1, 2])
def test_pre_request_lock_oserror_refuses_and_sends_nothing(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch, call: int
) -> None:
    _patch_lock_open_error(monkeypatch, call, PermissionError(13, "Permission denied"))
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    with pytest.raises(RunRefused, match="nothing was sent"):
        attempt(synth_dataset, run, "Q01", lambda: client)
    assert client.agent.calls == [] and not (run / "answers" / "Q01-a1").exists()


def _post_request_failure_case(name: str, run: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import cited_research.accept.run as run_mod

    if name == "lock_oserror":
        _patch_lock_open_error(monkeypatch, 3, OSError(37, "No locks available"))
    elif name == "corrupt_ledger":
        real = run_mod.run_research

        def corrupts(**kw: Any) -> int:
            code = real(**kw)
            (run / "attempts.jsonl").write_text("{not json\n")
            return code

        monkeypatch.setattr(run_mod, "run_research", corrupts)
    elif name == "missing_request_manifest":
        real = run_mod.run_research

        def loses_manifest(**kw: Any) -> int:
            code = real(**kw)
            (kw["output_dir"] / "run-manifest.json").unlink()
            return code

        monkeypatch.setattr(run_mod, "run_research", loses_manifest)
    elif name == "runner_raises":

        def raises(**_: Any) -> int:
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(run_mod, "run_research", raises)


@pytest.mark.parametrize(
    "case, status",
    [
        ("lock_oserror", "complete"),
        ("corrupt_ledger", "complete"),
        ("missing_request_manifest", "outcome_unrecorded"),
        ("runner_raises", "outcome_unrecorded"),
    ],
)
def test_every_post_request_failure_is_exit_13_with_the_record_kept(
    tmp_path: Path,
    synth_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: str,
    status: str,
) -> None:
    import cited_research.accept.run as run_mod

    run = tmp_path / "run"
    _post_request_failure_case(case, run, monkeypatch)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    code = attempt(synth_dataset, run, "Q01", lambda: client)
    assert code == run_mod.EXIT_LEDGER_NOT_UPDATED
    err = capsys.readouterr().err
    sent = "the request may have been sent" if case == "runner_raises" else "the request was sent"
    assert sent in err and "Do not re-run the request" in err
    assert "refused" not in err and "nothing was sent" not in err
    durable = json.loads((run / "answers" / "Q01-a1" / "attempt.json").read_text())
    assert durable["terminal_status"] == status
    assert "attempt.json" in err


def test_post_request_failure_through_the_cli_is_exit_13_not_refused(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:

    _patch_lock_open_error(monkeypatch, 3, OSError(37, "No locks available"))
    monkeypatch.setenv("TABSTACK_API_KEY", "sk_test_not_a_real_key_000000")
    real = cli.run_one

    def with_fake(**kw: Any) -> int:
        return real(**kw, client_factory=fixture_client("complete-events.jsonl"))

    monkeypatch.setattr(cli, "run_one", with_fake)
    run = tmp_path / "run"
    argv = ["run-one", "--dataset", str(synth_dataset), "--question", "Q01", "--run", str(run)]
    assert cli.main([*argv, "--quiet"]) == 13
    err = capsys.readouterr().err
    assert "the request was sent" in err and not err.startswith("refused")


def test_pre_request_corrupt_ledger_refuses_cleanly(
    tmp_path: Path, synth_dataset: Path, capsys
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "attempts.jsonl").write_text("{not json\n")
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    with pytest.raises(RunRefused, match="line 1 is not valid JSON.*nothing was sent"):
        attempt(synth_dataset, run, "Q02", lambda: client)
    assert client.agent.calls == [] and not (run / "answers" / "Q02-a1").exists()
    assert cli.main(["summarize", "--run", str(run)]) == 1
    assert "line 1 is not valid JSON" in capsys.readouterr().err


def test_any_pre_request_write_failure_removes_the_reserved_dir(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    real = run_mod.write_attempts
    calls = {"n": 0}

    def fails_once(run_dir: Path, rows: List[Dict[str, Any]]) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(28, "No space left on device")
        real(run_dir, rows)

    monkeypatch.setattr(run_mod, "write_attempts", fails_once)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    with pytest.raises(RunRefused, match="No space left.*nothing was sent"):
        attempt(synth_dataset, run, "Q01", lambda: client)
    assert client.agent.calls == [] and not (run / "answers" / "Q01-a1").exists()
    # The next run is not blocked by a leftover reservation.
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1"]


def test_unreadable_attempt_json_does_not_break_reading_the_ledger(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_lock_on(3, monkeypatch)
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q01-a1" / "attempt.json").write_text("{broken")
    (a,) = _attempts(run)
    assert a["terminal_status"] == "in_progress"
    assert any("unreadable" in m for m in a["missing_data"])


def test_untracked_files_are_dirty_whatever_the_git_config(tmp_path: Path) -> None:
    import subprocess

    from cited_research.accept.run import _implementation_dirty

    pkg = _git_repo_with_package(tmp_path)
    subprocess.run(["git", "config", "status.showUntrackedFiles", "no"], cwd=tmp_path, check=True)
    assert _implementation_dirty(pkg) is False
    (pkg / "accept" / "brand_new.py").write_text("n = 1\n")
    assert _implementation_dirty(pkg) is True


@pytest.mark.parametrize("key", ["python_version", "tabstack_version"])
def test_runtime_change_in_one_run_is_refused(
    tmp_path: Path, synth_dataset: Path, key: str
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    m = json.loads((run / "manifest.json").read_text())
    m[key] = "0.0.1"
    (run / "manifest.json").write_text(json.dumps(m))
    with pytest.raises(RunRefused, match=f"different runtime \\({key} 0.0.1 ->"):
        attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))


def test_recovered_row_is_healed_and_stops_being_reported(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cited_research.accept.summary import render_text

    _failing_lock_on(3, monkeypatch)
    run = tmp_path / "run"
    assert attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl")) == 13
    s = summarize(run)
    assert s["ledger_recovered"] == ["Q01-a1"]
    assert "ledger not updated for Q01-a1" in render_text(s)
    monkeypatch.undo()
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    raw = (run / "attempts.jsonl").read_text()
    assert "ledger_recovered_from" not in raw
    healed = _attempts(run)[0]
    assert healed["terminal_status"] == "complete" and "ledger_recovered_from" not in healed
    assert any("restored from attempt.json" in m for m in healed["missing_data"])
    s = summarize(run)
    assert "ledger_recovered" not in s and "ledger not updated" not in render_text(s)


# --- delta review fixes ------------------------------------------------------------------------


def test_unreadable_attempt_json_note_is_not_duplicated(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_lock_on(3, monkeypatch)
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q01-a1" / "attempt.json").write_text("{broken")
    monkeypatch.undo()
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    attempt(synth_dataset, run, "Q03", fixture_client("complete-events.jsonl"))
    row = next(a for a in _attempts(run) if a["attempt_id"] == "Q01-a1")
    notes = [n for n in row["missing_data"] if "exists but is unreadable" in n]
    assert len(notes) == 1, notes
    raw = (run / "attempts.jsonl").read_text()
    assert raw.count("exists but is unreadable") == 1


def _post_request_records_fail(
    monkeypatch: pytest.MonkeyPatch, durable: bool, ledger: bool
) -> None:
    import cited_research.accept.run as run_mod

    if durable:
        monkeypatch.setattr(
            run_mod, "_write_durable", lambda *_: OSError(28, "disk full (durable)")
        )
    if ledger:
        _failing_lock_on(3, monkeypatch)


@pytest.mark.parametrize(
    "durable, ledger, says, never",
    [
        (False, True, "report it as recovered", "nothing can recover"),
        (True, False, "was updated with this outcome", "report it as recovered"),
        (True, True, "still shows this attempt as in_progress", "report it as recovered"),
    ],
)
def test_post_request_message_matches_what_was_saved(
    tmp_path: Path,
    synth_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    durable: bool,
    ledger: bool,
    says: str,
    never: str,
) -> None:
    _post_request_records_fail(monkeypatch, durable, ledger)
    run = tmp_path / "run"
    assert attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl")) == 13
    err = " ".join(capsys.readouterr().err.split())
    assert says in err and never not in err
    if durable and ledger:
        assert "disk full (durable)" in err and "still locked" in err, "both errors named"
        assert "nothing can recover its outcome automatically" in err
        assert _attempts(run)[0]["terminal_status"] == "in_progress"
    if durable and not ledger:
        assert _attempts(run)[0]["terminal_status"] == "complete"


def test_closed_stdout_after_a_recorded_success_keeps_exit_code(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writing to a closed stream raises ValueError, not OSError. After the outcome is recorded,
    run-one's own summary lines must not turn a success into a refusal."""
    import io as io_mod

    import cited_research.accept.run as run_mod
    from cited_research.accept.run import run_one

    real = run_mod.run_research
    # The runner itself stays quiet so only run-one's final lines hit the closed stream.
    monkeypatch.setattr(run_mod, "run_research", lambda **kw: real(**{**kw, "quiet": True}))
    closed = io_mod.StringIO()
    closed.close()
    run = tmp_path / "run"
    code = run_one(
        synth_dataset,
        "Q01",
        run,
        quiet=False,
        stdout=closed,
        client_factory=fixture_client("complete-events.jsonl"),
        synthetic=True,
        post_terminal_grace=0.2,
    )
    assert code == 0
    assert [a["terminal_status"] for a in _attempts(run)] == ["complete"]


def test_closed_stderr_during_a_post_request_report_is_still_exit_13(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io as io_mod
    import sys as sys_mod

    _failing_lock_on(3, monkeypatch)
    closed = io_mod.StringIO()
    closed.close()
    monkeypatch.setattr(sys_mod, "stderr", closed)
    run = tmp_path / "run"
    assert attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl")) == 13


@pytest.mark.parametrize("exc", [OSError(5, "close failed"), KeyboardInterrupt()])
def test_phase_one_failure_after_the_in_progress_write_removes_the_row(
    tmp_path: Path,
    synth_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    exc: BaseException,
) -> None:
    import contextlib

    import cited_research.accept.run as run_mod

    real = run_mod.ledger_lock
    count = {"n": 0}

    @contextlib.contextmanager
    def lock(run_dir: Path) -> Iterator[None]:
        count["n"] += 1
        with real(run_dir):
            yield
        if count["n"] == 2:  # the in_progress write succeeded; the lock exit then fails
            raise exc

    monkeypatch.setattr(run_mod, "ledger_lock", lock)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    expected = RunRefused if isinstance(exc, Exception) else KeyboardInterrupt
    with pytest.raises(expected) as info:
        attempt(synth_dataset, run, "Q01", lambda: client)
    if expected is RunRefused:
        assert "nothing was sent" in str(info.value)
    assert client.agent.calls == []
    assert _attempts(run) == [], "no stray in_progress row"
    assert not (run / "answers" / "Q01-a1").exists()
    monkeypatch.undo()
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1"], "question not blocked"


# --- delta review 2 ----------------------------------------------------------------------------


@pytest.mark.parametrize("exc", [OSError(5, "close failed"), KeyboardInterrupt()])
def test_unremovable_phase_one_row_is_named_with_how_to_clear_it(
    tmp_path: Path,
    synth_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exc: BaseException,
) -> None:
    import contextlib

    import cited_research.accept.run as run_mod

    real = run_mod.ledger_lock
    count = {"n": 0}

    @contextlib.contextmanager
    def lock(run_dir: Path) -> Iterator[None]:
        count["n"] += 1
        with real(run_dir):
            yield
        if count["n"] == 2:
            raise exc

    monkeypatch.setattr(run_mod, "ledger_lock", lock)
    monkeypatch.setattr(run_mod, "_remove_ledger_row", lambda *_: False)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    if isinstance(exc, Exception):
        with pytest.raises(RunRefused) as info:
            attempt(synth_dataset, run, "Q01", lambda: client)
        msg = str(info.value)
    else:
        with pytest.raises(KeyboardInterrupt):
            attempt(synth_dataset, run, "Q01", lambda: client)
        msg = capsys.readouterr().err
    assert "nothing was sent" in msg or "interrupted before the request" in msg
    assert "the in_progress row for Q01-a1 could not be removed" in msg
    assert "it was never sent" in msg and "delete that line" in msg
    assert client.agent.calls == []
    assert (run / "answers" / "Q01-a1").is_dir(), "directory kept while its row remains"
    assert [a["terminal_status"] for a in _attempts(run)] == ["in_progress"]


def test_corrupt_ledger_message_is_specific_and_its_promise_holds(
    tmp_path: Path,
    synth_dataset: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cited_research.accept.summary import render_text

    run = tmp_path / "run"
    _post_request_failure_case("corrupt_ledger", run, monkeypatch)
    assert attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl")) == 13
    err = " ".join(capsys.readouterr().err.split())
    assert "is corrupt" in err and "line 1 is not valid JSON" in err
    assert "Fix that line; do not delete it" in err and "report it as recovered" not in err
    # As promised: refusal until fixed...
    with pytest.raises(ValueError, match="line 1 is not valid JSON"):
        summarize(run)
    monkeypatch.undo()
    # ...then, after deleting the corrupt line, the attempt is recovered from attempt.json.
    (run / "attempts.jsonl").write_text("")
    s = summarize(run)
    assert s["ledger_recovered"] == ["Q01-a1"]
    assert s["scopes"]["synthetic"]["by_terminal_status"] == {"complete": 1}
    assert "ledger not updated for Q01-a1" in render_text(s)
    # The next run-one writes it back into the ledger and the note goes away.
    attempt(synth_dataset, run, "Q02", fixture_client("complete-events.jsonl"))
    rows = [json.loads(x) for x in (run / "attempts.jsonl").read_text().splitlines()]
    assert sorted(r["attempt_id"] for r in rows) == ["Q01-a1", "Q02-a1"]
    assert all("ledger_recovered_from" not in r for r in rows)
    assert "ledger_recovered" not in summarize(run)


def test_unlisted_answer_dirs_are_counted_or_skipped_never_dropped(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q02-a1").mkdir()  # reserved, request not started: skipped
    (run / "answers" / "Q03-a1").mkdir()
    (run / "answers" / "Q03-a1" / "attempt.json").write_text("{broken")
    (run / "answers" / "Q03-a2").mkdir()
    (run / "answers" / "Q03-a2" / "attempt.json").write_text(json.dumps({"attempt_id": "Q09-a1"}))
    rows = {a["attempt_id"]: a for a in _attempts(run)}
    assert sorted(rows) == ["Q01-a1", "Q03-a1", "Q03-a2"]
    for aid in ("Q03-a1", "Q03-a2"):
        assert rows[aid]["terminal_status"] == "in_progress"
        assert rows[aid]["failure_class"] == "no_terminal_record"
        assert "the request may have been sent" in rows[aid]["missing_data"][0]
    assert "invalid" in rows["Q03-a2"]["missing_data"][1]


# --- orphan recovery review --------------------------------------------------------------------


def test_sent_attempt_whose_ledger_line_was_deleted_stays_in_the_denominator(
    tmp_path: Path, synth_dataset: Path
) -> None:
    """Sent, killed mid-request (artifacts but no attempt.json), then its ledger line removed:
    it must still count, as in_progress, not vanish while its directory blocks the question."""
    from cited_research.accept.summary import render_text

    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    killed = run / "answers" / "Q02-a1"
    killed.mkdir()
    (killed / "question.txt").write_text("q\n")
    (killed / "events.sanitized.jsonl").write_text('{"event": "start"}\n')
    s = summarize(run)
    b = s["scopes"]["synthetic"]
    assert b["attempts"] == 2 and b["by_terminal_status"] == {"complete": 1, "in_progress": 1}
    assert b["accepted_over_attempts"]["denominator"] == 2
    assert s["ledger_placeholders"] == ["Q02-a1"] and "ledger_recovered" not in s
    assert "the request may have been sent" in render_text(s)
    prepare_review(run)  # the placeholder gets a usage row, no review rows, no crash
    assert [r["attempt_id"] for r in read_review_sheet(run / "reviews" / "usage.csv")] == [
        "Q01-a1",
        "Q02-a1",
    ]
    # The next run-one writes the placeholder into the ledger with an accurate note.
    attempt(synth_dataset, run, "Q03", fixture_client("complete-events.jsonl"))
    rows = [json.loads(x) for x in (run / "attempts.jsonl").read_text().splitlines()]
    placeholder = next(r for r in rows if r["attempt_id"] == "Q02-a1")
    assert placeholder["terminal_status"] == "in_progress"
    assert "ledger_placeholder_for" not in placeholder
    assert any(
        "re-created from a non-empty answers directory" in m for m in placeholder["missing_data"]
    )
    assert "ledger_placeholders" not in summarize(run)


def test_invalid_attempt_json_for_an_in_progress_row_is_noted_not_adopted(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _failing_lock_on(3, monkeypatch)
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q01-a1" / "attempt.json").write_text(json.dumps({"attempt_id": "Q01-a1"}))
    s = summarize(run)  # no KeyError
    (a,) = _attempts(run)
    assert a["terminal_status"] == "in_progress"
    assert any("exists but is invalid" in m for m in a["missing_data"])
    assert s["scopes"]["synthetic"]["by_terminal_status"] == {"in_progress": 1}


@pytest.mark.parametrize("record", [{"attempt_id": "Q09-a1"}, {"attempt_id": "Q03-a1"}, [1, 2]])
def test_invalid_orphan_attempt_json_never_crashes_a_reader(
    tmp_path: Path, synth_dataset: Path, record: Any
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q03-a1").mkdir()
    (run / "answers" / "Q03-a1" / "attempt.json").write_text(json.dumps(record))
    s = summarize(run)
    prepare_review(run)
    assert s["scopes"]["synthetic"]["by_terminal_status"] == {"complete": 1, "in_progress": 1}
    assert s["ledger_placeholders"] == ["Q03-a1"]


def _foreign_copy(src_run: Path, dst_run: Path, aid: str, **overrides: Any) -> None:
    import shutil

    shutil.copytree(src_run / "answers" / aid, dst_run / "answers" / aid)
    rec = json.loads((dst_run / "answers" / aid / "attempt.json").read_text())
    rec.update(overrides)
    (dst_run / "answers" / aid / "attempt.json").write_text(json.dumps(rec))


@pytest.mark.parametrize("field", ["dataset_sha256", "implementation_sha256"])
def test_copied_answers_dir_from_another_run_is_foreign_not_adopted(
    tmp_path: Path, synth_dataset: Path, capsys: pytest.CaptureFixture[str], field: str
) -> None:
    from cited_research.accept.summary import render_text

    other, run = tmp_path / "other", tmp_path / "run"
    attempt(synth_dataset, other, "Q02", fixture_client("complete-events.jsonl"))
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    _foreign_copy(other, run, "Q02-a1", **{field: "f" * 64})
    s = summarize(run)
    assert s["scopes"]["synthetic"]["attempts"] == 1, "not counted"
    assert s["foreign_answer_dirs"] == [
        {
            "dir": "answers/Q02-a1",
            "reason": "attempt.json is from a different dataset or implementation",
        }
    ]
    assert "not counted: answers/Q02-a1" in render_text(s)
    capsys.readouterr()
    attempt(synth_dataset, run, "Q03", fixture_client("complete-events.jsonl"))
    assert "is not this run's attempt" in capsys.readouterr().err
    raw = (run / "attempts.jsonl").read_text()
    assert '"Q02-a1"' not in raw, "a foreign dir is never written into the ledger"


def test_same_run_orphan_with_matching_identity_is_adopted(
    tmp_path: Path, synth_dataset: Path
) -> None:
    other, run = tmp_path / "other", tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    attempt(synth_dataset, other, "Q02", fixture_client("complete-events.jsonl"))
    _foreign_copy(other, run, "Q02-a1", run_id="run")  # same dataset and implementation
    s = summarize(run)
    assert s["ledger_recovered"] == ["Q02-a1"] and "foreign_answer_dirs" not in s


def test_ledger_row_with_an_escaping_answer_dir_is_corrupt(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    rows = [json.loads(x) for x in (run / "attempts.jsonl").read_text().splitlines()]
    rows[0]["answer_dir"] = "../../elsewhere"
    (run / "attempts.jsonl").write_text(json.dumps(rows[0]) + "\n")
    with pytest.raises(ValueError, match="answer_dir"):
        summarize(run)


@pytest.mark.parametrize("missing", ["report.md", "sources.json"])
def test_prepare_review_refuses_cleanly_when_a_complete_answer_lost_files(
    tmp_path: Path, synth_dataset: Path, capsys: pytest.CaptureFixture[str], missing: str
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    (run / "answers" / "Q01-a1" / missing).unlink()
    assert cli.main(["prepare-review", "--run", str(run)]) == 1
    assert "report or sources cannot be read" in capsys.readouterr().err


# --- dotfile emptiness --------------------------------------------------------------------------


def test_reservation_with_only_dotfiles_is_empty_not_a_placeholder(
    tmp_path: Path, synth_dataset: Path
) -> None:
    run = tmp_path / "run"
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    reserved = run / "answers" / "Q02-a1"
    reserved.mkdir()
    (reserved / ".DS_Store").write_bytes(b"\0")
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1"]
    s = summarize(run)
    assert "ledger_placeholders" not in s and s["scopes"]["synthetic"]["attempts"] == 1
    (reserved / "report.md").write_text("partial\n")
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1", "Q02-a1"]
    assert summarize(run)["ledger_placeholders"] == ["Q02-a1"]


def test_dot_directory_makes_a_reservation_non_empty(tmp_path: Path) -> None:
    from cited_research.accept.run import is_effectively_empty

    d = tmp_path / "Q01-a1"
    (d / ".hidden").mkdir(parents=True)
    assert is_effectively_empty(d) is False
    assert is_effectively_empty(tmp_path / "missing") is False


def test_failed_reservation_with_a_dotfile_is_still_cleaned_up(
    tmp_path: Path, synth_dataset: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import cited_research.accept.run as run_mod

    real = run_mod.write_attempts

    def dotfile_then_fail(run_dir: Path, rows: List[Dict[str, Any]]) -> None:
        (run_dir / "answers" / "Q01-a1" / ".DS_Store").write_bytes(b"\0")
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(run_mod, "write_attempts", dotfile_then_fail)
    client = ZeroRetryFake(load_events("complete-events.jsonl"))
    run = tmp_path / "run"
    with pytest.raises(RunRefused, match="nothing was sent"):
        attempt(synth_dataset, run, "Q01", lambda: client)
    assert not (run / "answers" / "Q01-a1").exists(), "dotfile removed, then the directory"
    monkeypatch.setattr(run_mod, "write_attempts", real)
    attempt(synth_dataset, run, "Q01", fixture_client("complete-events.jsonl"))
    assert [a["attempt_id"] for a in _attempts(run)] == ["Q01-a1"]


def test_cleanup_never_removes_a_reservation_with_real_content(tmp_path: Path) -> None:
    from cited_research.accept.run import _remove_if_empty

    d = tmp_path / "Q01-a1"
    d.mkdir()
    (d / ".DS_Store").write_bytes(b"\0")
    (d / "question.txt").write_text("q\n")
    _remove_if_empty(d)
    assert (d / "question.txt").exists() and (d / ".DS_Store").exists()
