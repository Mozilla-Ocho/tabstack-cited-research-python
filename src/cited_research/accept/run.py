"""Run one frozen question through the traced Research runner and record the attempt.

Layout of a run directory:

    <run>/manifest.json                 dataset version and hash, configuration, provenance
    <run>/attempts.jsonl                one record per attempt, every terminal state
    <run>/answers/<attempt-id>/...      report.md, sources.json, events, per-request manifest
    <run>/reviews/*.csv                 written later by prepare-review
    <run>/summary.json                  written later by summarize

Nothing here retries. An existing attempt is never reused or overwritten.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, TextIO

import tabstack

from ..models import sha256_text
from ..sanitize import scrub_text, utc_now_iso, write_text_atomic
from ..tabstack_runner import EXIT_CODES, PACKAGE_DIR, _git_commit, no_retry_client, run_research
from .dataset import (
    Dataset,
    file_sha256,
    freeze_path_for,
    load_dataset,
    load_freeze,
    validate_dataset,
)

RUN_SCHEMA = "cited-research-accept/run/v1"
ATTEMPT_SCHEMA = "cited-research-accept/attempt/v1"
ATTEMPT_ID = re.compile(r"(Q[0-9]{2})-a([0-9]+)")

# What each runner terminal status means for the ledger.
FAILURE_CLASS = {
    "complete": "completed",
    "task_error": "stream_error_event",
    "http_error": "http_rejection",
    "transport_error": "transport_error_before_stream",
    "stream_transport_error": "transport_error_mid_stream",
    "premature_close": "missing_terminal_event",
    "protocol_error": "duplicate_terminal_event",
    "malformed_complete": "malformed_complete",
    "silence_timeout": "client_timeout_silence",
    "deadline_exceeded": "client_timeout_deadline",
    "unexpected_error": "unexpected_error",
    "in_progress": "no_terminal_record",
}
CLIENT_STOPPED = frozenset({"silence_timeout", "deadline_exceeded", "stream_transport_error"})

USAGE_NOTE = (
    "No per-request credit or billing field in the /research stream (tabstack 2.8.5): "
    "complete.metadata.metrics.tokens has model token counts, not credits. Attach verified usage "
    "in reviews/usage.csv, or leave it unavailable. Missing usage is not zero."
)
RETRY_POLICY = "SDK max_retries=0; no application retries; one POST per attempt"

# Ledger columns, in order. acceptance-evals/attempts-template.csv carries the same header.
ATTEMPT_COLUMNS = (
    "attempt_id",
    "run_id",
    "question_id",
    "category",
    "dataset_version",
    "dataset_sha256",
    "implementation_commit",
    "implementation_dirty",
    "python_version",
    "tabstack_version",
    "mode",
    "nocache",
    "sdk_max_retries",
    "application_retries",
    "retry_policy",
    "silence_timeout_seconds",
    "deadline_seconds",
    "question",
    "output_instruction",
    "query_sha256",
    "dispatched_at_utc",
    "terminal_at_utc",
    "first_event_ms",
    "terminal_elapsed_ms",
    "terminal_status",
    "failure_class",
    "exit_code",
    "error",
    "client_stopped_waiting",
    "provider_task_state",
    "source_count",
    "source_urls_in_order",
    "answer_dir",
    "report_path",
    "sources_path",
    "events_path",
    "request_manifest_path",
    "usage_status",
    "usage_value",
    "usage_unit",
    "usage_receipt_ref",
    "usage_note",
    "pilot_only",
    "synthetic",
    "review_status",
    "missing_data",
)


def _implementation_dirty() -> Optional[bool]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--", "src", "pyproject.toml", "uv.lock"],
            cwd=PACKAGE_DIR,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
        return bool(out.strip())
    except Exception:
        return None


def read_attempts(run_dir: Path) -> List[Dict[str, Any]]:
    path = run_dir / "attempts.jsonl"
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def write_attempts(run_dir: Path, attempts: List[Dict[str, Any]]) -> None:
    write_text_atomic(
        run_dir / "attempts.jsonl",
        "".join(json.dumps(a, ensure_ascii=False, sort_keys=True) + "\n" for a in attempts),
    )


def next_attempt_id(run_dir: Path, question_id: str, attempts: List[Dict[str, Any]]) -> str:
    used = [a["attempt_id"] for a in attempts]
    answers = run_dir / "answers"
    if answers.exists():
        used.extend(p.name for p in answers.iterdir())
    numbers = [
        int(m.group(2))
        for m in (ATTEMPT_ID.fullmatch(u) for u in used)
        if m and m.group(1) == question_id
    ]
    return f"{question_id}-a{max(numbers, default=0) + 1}"


class RunRefused(RuntimeError):
    """The run-one request breaks a rule; nothing was sent."""


def _config(
    ds: Dataset,
    freeze: Dict[str, Any],
    mode: str,
    nocache: bool,
    fetch_timeout: Optional[int],
    silence_timeout: Optional[float],
    deadline: Optional[float],
    pilot_only: bool,
    synthetic: bool,
) -> Dict[str, Any]:
    """The part of the run manifest every attempt in the run must share."""
    return {
        "dataset_file": ds.path.name,
        "dataset_version": freeze["dataset_version"],
        "dataset_sha256": ds.sha256,
        "output_instruction": freeze["output_instruction"],
        "mode": mode,
        "nocache": nocache,
        "fetch_timeout_seconds": fetch_timeout,
        "silence_timeout_seconds": silence_timeout,
        "deadline_seconds": deadline,
        "sdk_max_retries": 0,
        "application_retries": 0,
        "retry_policy": RETRY_POLICY,
        "pilot_only": pilot_only,
        "synthetic": synthetic,
    }


def run_one(
    dataset_path: Path,
    question_id: str,
    run_dir: Path,
    mode: str = "fast",
    nocache: bool = False,
    fetch_timeout: Optional[int] = None,
    silence_timeout: Optional[float] = None,
    deadline: Optional[float] = None,
    pilot_only: bool = False,
    another_attempt: bool = False,
    freeze_path: Optional[Path] = None,
    quiet: bool = False,
    stdout: TextIO = sys.stdout,
    client_factory: Callable[[], Any] = no_retry_client,
    synthetic: bool = False,
    post_terminal_grace: Optional[float] = None,
) -> int:
    """One request for one frozen question. Returns the runner's exit code.

    Raises RunRefused before any request when the dataset is not frozen, its hash changed, it
    has pending rows, the question is unknown, the run's configuration differs, or the question
    already has an attempt in this run and `another_attempt` is False. `synthetic` is for
    offline fixtures only; the CLI never sets it.
    """
    freeze = load_freeze(dataset_path, freeze_path)
    ds = load_dataset(dataset_path)
    if ds.sha256 != freeze.get("dataset_sha256"):
        raise RunRefused(
            f"{dataset_path} sha256 {ds.sha256[:12]}... does not match the freeze record "
            f"({str(freeze.get('dataset_sha256'))[:12]}...); the frozen set was edited"
        )
    issues = validate_dataset(
        ds,
        allow_subset=freeze.get("design") == "subset",
        for_run=True,
        max_evidence_age_days=freeze.get("max_evidence_age_days"),
    )
    if issues:
        raise RunRefused("dataset is not runnable:\n" + "\n".join(f"  {i}" for i in issues))
    question = ds.by_id().get(question_id)
    if question is None or question_id not in freeze.get("question_ids", []):
        raise RunRefused(f"{question_id} is not in the frozen dataset")

    config = _config(
        ds, freeze, mode, nocache, fetch_timeout, silence_timeout, deadline, pilot_only, synthetic
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed = [k for k, v in config.items() if manifest.get("config", {}).get(k) != v]
        if changed:
            raise RunRefused(
                f"{run_dir} was started with a different configuration ({', '.join(changed)}); "
                "start a new run directory"
            )
    else:
        manifest = {
            "schema": RUN_SCHEMA,
            "run_id": run_dir.name,
            "created_at_utc": utc_now_iso(),
            "dataset_path": os.path.relpath(dataset_path.resolve(), run_dir.resolve()),
            "freeze_sha256": file_sha256(freeze_path or freeze_path_for(dataset_path)),
            "intended_question_ids": list(freeze["question_ids"]),
            "config": config,
            "implementation_commit": _git_commit(),
            "implementation_dirty": _implementation_dirty(),
            "python_version": platform.python_version(),
            "tabstack_version": tabstack.__version__,
            "os": f"{platform.system()} {platform.release()} {platform.machine()}",
            "notes": [
                "pilot_only and synthetic attempts are excluded from live scored totals.",
                "A client timeout ends the wait; it does not establish provider cancellation.",
            ],
        }
        write_text_atomic(manifest_path, json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    attempts = read_attempts(run_dir)
    prior = [a["attempt_id"] for a in attempts if a.get("question_id") == question_id]
    if prior and not another_attempt:
        raise RunRefused(
            f"{question_id} already has attempt(s) {', '.join(prior)} in {run_dir}; pass "
            "--another-attempt to record a new, separate attempt (never an overwrite)"
        )
    attempt_id = next_attempt_id(run_dir, question_id, attempts)
    answer_dir = run_dir / "answers" / attempt_id
    answer_dir.mkdir(parents=True, exist_ok=False)

    query = f"{question['question']}\n\n{freeze['output_instruction']}"
    rel = answer_dir.relative_to(run_dir).as_posix()
    record: Dict[str, Any] = dict.fromkeys(ATTEMPT_COLUMNS)
    record.update(
        schema=ATTEMPT_SCHEMA,
        attempt_id=attempt_id,
        run_id=run_dir.name,
        question_id=question_id,
        category=question["category"],
        dataset_version=freeze["dataset_version"],
        dataset_sha256=ds.sha256,
        implementation_commit=manifest.get("implementation_commit"),
        implementation_dirty=manifest.get("implementation_dirty"),
        python_version=platform.python_version(),
        tabstack_version=tabstack.__version__,
        mode=mode,
        nocache=nocache,
        sdk_max_retries=None,
        application_retries=0,
        retry_policy=RETRY_POLICY,
        silence_timeout_seconds=silence_timeout,
        deadline_seconds=deadline,
        question=scrub_text(question["question"]),
        output_instruction=freeze["output_instruction"],
        query_sha256=sha256_text(query),
        dispatched_at_utc=utc_now_iso(),
        terminal_status="in_progress",
        failure_class=FAILURE_CLASS["in_progress"],
        answer_dir=rel,
        usage_status="unavailable",
        usage_note=USAGE_NOTE,
        pilot_only=pilot_only,
        synthetic=synthetic,
        review_status="not_applicable",
        missing_data=["process ended before the attempt recorded a terminal state"],
    )
    # Recorded before the request, so a crash leaves a visible in_progress attempt.
    write_attempts(run_dir, [*attempts, record])

    kwargs: Dict[str, Any] = {}
    if post_terminal_grace is not None:
        kwargs["post_terminal_grace"] = post_terminal_grace
    code = run_research(
        query=query,
        mode=mode,
        nocache=nocache,
        fetch_timeout=fetch_timeout,
        output_dir=answer_dir,
        quiet=quiet,
        stdout=stdout,
        client_factory=client_factory,
        silence_timeout=silence_timeout,
        deadline=deadline,
        **kwargs,
    )
    _finish_record(record, answer_dir, rel, code)
    # Re-read: another process may have appended attempts while this request ran.
    current = read_attempts(run_dir)
    if not any(a.get("attempt_id") == attempt_id for a in current):
        current.append(record)
    write_attempts(run_dir, [record if a.get("attempt_id") == attempt_id else a for a in current])
    if not quiet:
        stdout.write(
            f"attempt {attempt_id}: {record['terminal_status']} (exit {code}), "
            f"{record['source_count'] if record['source_count'] is not None else 'no'} sources "
            f"-> {run_dir / 'attempts.jsonl'}\n"
        )
        if record["terminal_status"] == "complete":
            stdout.write(f"next: cited-research-accept prepare-review --run {run_dir}\n")
        stdout.flush()
    return code


def _finish_record(record: Dict[str, Any], answer_dir: Path, rel: str, code: int) -> None:
    req = json.loads((answer_dir / "run-manifest.json").read_text(encoding="utf-8"))
    status = req.get("terminal_status", "unknown")
    missing: List[str] = []
    record.update(
        sdk_max_retries=req.get("sdk_max_retries"),
        application_retries=req.get("application_retries", 0),
        dispatched_at_utc=req.get("started_at_utc") or record["dispatched_at_utc"],
        terminal_at_utc=req.get("completed_at_utc"),
        first_event_ms=req.get("first_event_ms"),
        terminal_elapsed_ms=req.get("duration_ms"),
        terminal_status=status,
        failure_class=FAILURE_CLASS.get(status, "unexpected_error"),
        exit_code=code,
        error=req.get("error"),
        events_path=f"{rel}/events.sanitized.jsonl",
        request_manifest_path=f"{rel}/run-manifest.json",
    )
    if code != EXIT_CODES.get(status, code):
        missing.append(f"exit code {code} does not match terminal status {status}")
    record["client_stopped_waiting"] = status in CLIENT_STOPPED
    if status == "complete" or status == "task_error":
        record["provider_task_state"] = "terminal_event_received"
    elif status == "http_error":
        record["provider_task_state"] = "rejected_before_stream"
    elif status == "transport_error":
        record["provider_task_state"] = "unknown_connection_failed_before_stream"
    else:
        record["provider_task_state"] = "unknown"
        missing.append("provider task state unknown: the client did not see a terminal event")
    if record["client_stopped_waiting"]:
        missing.append("client stopped waiting; provider cancellation not established")

    sources_file = answer_dir / "sources.json"
    if (answer_dir / "report.md").exists():
        record["report_path"] = f"{rel}/report.md"
    if sources_file.exists():
        pages = json.loads(sources_file.read_text(encoding="utf-8"))
        record["sources_path"] = f"{rel}/sources.json"
        record["source_count"] = len(pages)
        record["source_urls_in_order"] = [p.get("url") for p in pages]
        if not pages:
            missing.append("complete carried no cited pages")
    else:
        record["source_count"] = None  # no answer, so not "zero sources"
        record["source_urls_in_order"] = []
    if record["first_event_ms"] is None:
        missing.append("first_event_ms not measured (no event arrived)")
    missing.append("usage unavailable")
    record["review_status"] = "unreviewed" if status == "complete" else "not_applicable"
    record["missing_data"] = missing
