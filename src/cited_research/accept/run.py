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

import errno
import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from time import monotonic
from typing import Any, Callable, Dict, Iterator, List, Optional, TextIO, Tuple

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
    "implementation_sha256",
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


IMPLEMENTATION_ROOT_FILES = ("pyproject.toml", "uv.lock")


class ImplementationUnknown(RuntimeError):
    """The implementation files could not be read, so no identity can be compared."""


def implementation_identity(
    package_dir: Path = PACKAGE_DIR, project_root: Optional[Path] = None
) -> Tuple[str, List[str]]:
    """sha256 over the content of the implementation only: every file in the installed package
    (no __pycache__, no .pyc) plus pyproject.toml and uv.lock when they sit at the project root.
    Returns (hex digest, covered paths).

    It is read from disk, so it is the same for the same code whatever HEAD is (a docs or
    artifacts commit does not change it), it differs for two different uncommitted edits, it
    covers untracked files under the package, and it needs no git. Raises
    ImplementationUnknown if a file cannot be read.
    """
    root = project_root if project_root is not None else package_dir.parents[1]
    entries: List[Tuple[str, Path]] = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_dir() or "__pycache__" in path.parts or path.suffix in (".pyc", ".pyo"):
            continue
        entries.append(((Path(package_dir.name) / path.relative_to(package_dir)).as_posix(), path))
    if (root / "src" / package_dir.name).resolve() == package_dir.resolve():
        for name in IMPLEMENTATION_ROOT_FILES:
            if (root / name).is_file():
                entries.append((name, root / name))
    digest = hashlib.sha256()
    try:
        for rel, path in sorted(entries):
            digest.update(rel.encode("utf-8") + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).hexdigest().encode("ascii") + b"\n")
    except OSError as exc:
        raise ImplementationUnknown(f"cannot read implementation file: {exc}") from exc
    return digest.hexdigest(), [rel for rel, _ in sorted(entries)]


class LedgerLockError(RuntimeError):
    """The run directory's ledger lock could not be taken."""


LOCK_CONTENTION_ERRNOS = frozenset(
    {errno.EACCES, errno.EDEADLK, getattr(errno, "EDEADLOCK", errno.EDEADLK)}
)
LOCK_MAX_WAIT_SECONDS = 60.0


def lock_with_retry(
    try_lock: Callable[[], None],
    max_wait: float = LOCK_MAX_WAIT_SECONDS,
    clock: Callable[[], float] = monotonic,
) -> None:
    """Call `try_lock` until it succeeds. Retries only on lock contention (EACCES/EDEADLK, which
    msvcrt's LK_LOCK raises after its own ~10 s of retries), for at most `max_wait` seconds in
    total; any other OSError fails at once."""
    started = clock()
    while True:
        try:
            try_lock()
            return
        except OSError as exc:
            if exc.errno not in LOCK_CONTENTION_ERRNOS:
                raise LedgerLockError(f"cannot lock the run ledger: {exc}") from exc
            if clock() - started >= max_wait:
                raise LedgerLockError(
                    f"run ledger still locked by another run-one after {max_wait:g}s; "
                    "wait for it to finish and retry"
                ) from exc


LOCK_NAME = ".attempts.lock"


@contextmanager
def ledger_lock(run_dir: Path) -> Iterator[None]:
    """Exclusive lock around a read-modify-write of attempts.jsonl (and the run manifest).

    An OS-level lock on a sidecar file (fcntl.flock on POSIX, msvcrt.locking on Windows), so a
    crashed process never leaves a stale lock. Parallel run-one processes on one run directory
    take turns; nothing waits on the network while holding it.
    """
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / LOCK_NAME).open("a+b") as fh:
        if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
            import msvcrt

            fh.seek(0)
            lock_with_retry(
                lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)  # pyright: ignore
            )
            try:
                yield
            finally:
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # pyright: ignore
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


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
    try:
        impl_sha, impl_paths = implementation_identity()
    except ImplementationUnknown as exc:
        raise RunRefused(f"cannot identify the implementation, so nothing was sent: {exc}") from exc
    # Provenance only; the comparison uses impl_sha. None means git was unavailable or failed.
    commit, dirty = _git_commit(), _implementation_dirty()
    with ledger_lock(run_dir):
        manifest, attempts, attempt_id, answer_dir = _reserve_attempt(
            run_dir,
            dataset_path,
            freeze_path,
            freeze,
            config,
            impl_sha,
            impl_paths,
            commit,
            dirty,
            question_id,
            another_attempt,
        )
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
        implementation_sha256=impl_sha,
        implementation_commit=commit,
        implementation_dirty=dirty,
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
    # Recorded before the request, so a crash leaves a visible in_progress attempt. The lock was
    # released after reserving; re-read so a parallel process's record is never dropped.
    with ledger_lock(run_dir):
        write_attempts(run_dir, [*read_attempts(run_dir), record])

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
    # Re-read under the lock: another process may have appended attempts while this one ran.
    with ledger_lock(run_dir):
        current = read_attempts(run_dir)
        if not any(a.get("attempt_id") == attempt_id for a in current):
            current.append(record)
        write_attempts(
            run_dir, [record if a.get("attempt_id") == attempt_id else a for a in current]
        )
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


def _reserve_attempt(
    run_dir: Path,
    dataset_path: Path,
    freeze_path: Optional[Path],
    freeze: Dict[str, Any],
    config: Dict[str, Any],
    impl_sha: str,
    impl_paths: List[str],
    commit: Optional[str],
    dirty: Optional[bool],
    question_id: str,
    another_attempt: bool,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], str, Path]:
    """Check the run, allocate the next attempt ID, and create its directory. Call under
    ledger_lock."""
    manifest_path = run_dir / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        changed = [k for k, v in config.items() if manifest.get("config", {}).get(k) != v]
        if changed:
            raise RunRefused(
                f"{run_dir} was started with a different configuration ({', '.join(changed)}); "
                "start a new run directory"
            )
        recorded = manifest.get("implementation_sha256")
        if recorded is None:
            raise RunRefused(
                f"{run_dir} predates implementation identity (no implementation_sha256 in its "
                "manifest), so a new attempt cannot be shown to use the same code; start a new "
                "run directory"
            )
        if recorded != impl_sha:
            raise RunRefused(
                f"{run_dir} was started with implementation sha256 {recorded[:12]}... "
                f"(commit {manifest.get('implementation_commit')}); this checkout's "
                f"implementation is {impl_sha[:12]}... (commit {commit}, dirty={dirty}). Every "
                "attempt in a run must use the same implementation; start a new run directory"
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
            "implementation_sha256": impl_sha,
            "implementation_paths": impl_paths,
            "implementation_commit": commit,
            "implementation_dirty": dirty,
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
    answers = run_dir / "answers"
    if answers.exists():
        # A directory reserved by a parallel process whose ledger line is not written yet.
        for d in sorted(answers.iterdir()):
            m = ATTEMPT_ID.fullmatch(d.name)
            if m and m.group(1) == question_id and d.name not in prior:
                prior.append(d.name)
    if prior and not another_attempt:
        raise RunRefused(
            f"{question_id} already has attempt(s) {', '.join(prior)} in {run_dir}; pass "
            "--another-attempt to record a new, separate attempt (never an overwrite)"
        )
    attempt_id = next_attempt_id(run_dir, question_id, attempts)
    answer_dir = run_dir / "answers" / attempt_id
    answer_dir.mkdir(parents=True, exist_ok=False)

    return manifest, attempts, attempt_id, answer_dir


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
    if record.get("implementation_commit") is None or record.get("implementation_dirty") is None:
        missing.append(
            "implementation_commit/implementation_dirty unavailable (git failed, timed out, or "
            "this is not a checkout); implementation_sha256 still identifies the code"
        )
    missing.append("usage unavailable")
    record["review_status"] = "unreviewed" if status == "complete" else "not_applicable"
    record["missing_data"] = missing
