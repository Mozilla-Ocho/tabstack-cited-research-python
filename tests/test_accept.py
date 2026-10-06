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
