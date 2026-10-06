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
    "outcome_unrecorded": "post_request_recording_failed",
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


def _repo_root(package_dir: Path = PACKAGE_DIR) -> Optional[Path]:
    """Top of this package's own git checkout (the same check `_git_commit` makes), or None."""
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=package_dir,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except Exception:
        return None
    root = Path(top).resolve()
    if (root / "src" / package_dir.name).resolve() != package_dir.resolve():
        return None
    return root


def _implementation_dirty(package_dir: Path = PACKAGE_DIR) -> Optional[bool]:
    """True if the implementation paths differ from HEAD (modified, staged, or untracked), run
    from the repository root so the pathspecs mean the root's src/, pyproject.toml, uv.lock.
    None when git fails, times out, or the package is not in its own checkout."""
    root = _repo_root(package_dir)
    if root is None:
        return None
    try:
        out = subprocess.run(
            [
                "git",
                "status",
                "--porcelain",
                "--untracked-files=all",
                "--ignored=no",
                "--",
                "src",
                "pyproject.toml",
                "uv.lock",
            ],
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout
    except Exception:
        return None
    return bool(out.strip())


IMPLEMENTATION_ROOT_FILES = ("pyproject.toml", "uv.lock")


# File types the package ships. Hatch builds the wheel from src/cited_research, and today that
# directory holds only .py files; a test fails if a file of any other type appears there, so a
# new data file type gets added here deliberately instead of being silently ignored.
PACKAGE_SUFFIXES = (".py",)
PACKAGE_NAMES = ("py.typed",)


def is_package_file(path: Path, package_dir: Path = PACKAGE_DIR) -> bool:
    """A real package file: a shipped type, not a dotfile (.DS_Store), editor backup, or cache."""
    parts = path.relative_to(package_dir).parts
    if any(part.startswith(".") or part == "__pycache__" for part in parts):
        return False
    return path.suffix in PACKAGE_SUFFIXES or path.name in PACKAGE_NAMES


class ImplementationUnknown(RuntimeError):
    """The implementation files could not be read, so no identity can be compared."""


def implementation_identity(
    package_dir: Path = PACKAGE_DIR, project_root: Optional[Path] = None
) -> Tuple[str, List[str]]:
    """sha256 over the content of the implementation only: the package's own files (see
    is_package_file: shipped types, no dotfiles, backups, or caches) plus pyproject.toml and
    uv.lock when they sit at the project root.
    Returns (hex digest, covered paths).

    It is read from disk, so it is the same for the same code whatever HEAD is (a docs or
    artifacts commit does not change it), it differs for two different uncommitted edits, it
    covers untracked files under the package, and it needs no git. Raises
    ImplementationUnknown if a file cannot be read.
    """
    root = project_root if project_root is not None else package_dir.parents[1]
    entries: List[Tuple[str, Path]] = []
    for path in sorted(package_dir.rglob("*")):
        if path.is_dir() or not is_package_file(path, package_dir):
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


def _acquire(fh: Any) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import msvcrt

        fh.seek(0)
        lock_with_retry(lambda: msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1))  # pyright: ignore
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)


def _release(fh: Any) -> None:
    if sys.platform == "win32":  # pragma: no cover - exercised on Windows only
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)  # pyright: ignore
    else:
        import fcntl

        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextmanager
def ledger_lock(run_dir: Path) -> Iterator[None]:
    """Exclusive lock around a read-modify-write of attempts.jsonl (and the run manifest).

    An OS-level lock on a sidecar file (fcntl.flock on POSIX, msvcrt.locking on Windows), so a
    crashed process never leaves a stale lock. Parallel run-one processes on one run directory
    take turns; nothing waits on the network while holding it.

    Every OSError while creating the directory, opening the lock file, or taking the lock
    becomes LedgerLockError, on both platforms. Errors raised inside the `with` body are not
    converted. A failed unlock is ignored: closing the file releases the lock.
    """
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        fh = (run_dir / LOCK_NAME).open("a+b")
    except OSError as exc:
        raise LedgerLockError(f"cannot open the run ledger lock: {exc}") from exc
    try:
        try:
            _acquire(fh)
        except LedgerLockError:
            raise
        except OSError as exc:
            raise LedgerLockError(f"cannot lock the run ledger: {exc}") from exc
        try:
            yield
        finally:
            try:
                _release(fh)
            except OSError:
                pass
    finally:
        fh.close()


ATTEMPT_RECORD = "attempt.json"
EXIT_LEDGER_NOT_UPDATED = 13
RECOVERED_NOTE = "ledger_recovered_from"


class LedgerCorrupt(ValueError):
    """attempts.jsonl holds a line that is not a valid attempt record."""


# Fields every reader (summarize, prepare-review, run-one) indexes directly, and their types.
REQUIRED_RECORD_FIELDS: Dict[str, type] = {
    "attempt_id": str,
    "run_id": str,
    "question_id": str,
    "terminal_status": str,
    "failure_class": str,
    "answer_dir": str,
}
PLACEHOLDER_NOTE = "ledger row lost and no attempt.json; the request may have been sent"
PLACEHOLDER_MARK = "ledger_placeholder_for"
PLACEHOLDER_HEALED_NOTE = (
    "ledger row re-created from a non-empty answers directory with no usable attempt.json"
)


def record_problem(rec: Any, expected_id: Optional[str] = None) -> Optional[str]:
    """None if `rec` is an attempt record every reader can use; otherwise why not."""
    if not isinstance(rec, dict):
        return "not a JSON object"
    for key, kind in REQUIRED_RECORD_FIELDS.items():
        if not isinstance(rec.get(key), kind):
            return f"field {key!r} missing or not a {kind.__name__}"
    m = ATTEMPT_ID.fullmatch(rec["attempt_id"])
    if not m:
        return f"attempt_id {rec['attempt_id']!r} is not of the form Q00-a1"
    if expected_id is not None and rec["attempt_id"] != expected_id:
        return f"attempt_id {rec['attempt_id']!r} does not match {expected_id!r}"
    if rec["answer_dir"] != f"answers/{rec['attempt_id']}":
        return f"answer_dir {rec['answer_dir']!r} is not answers/{rec['attempt_id']}"
    if rec["question_id"] != m.group(1):
        return f"question_id {rec['question_id']!r} does not match attempt_id"
    md = rec.get("missing_data")
    if md is not None and not (isinstance(md, list) and all(isinstance(x, str) for x in md)):
        return "missing_data is not a list of strings"
    return None


def _note_once(row: Dict[str, Any], prefix: str, detail: str) -> Dict[str, Any]:
    """Add a missing_data note unless one with the same prefix is already there."""
    row = dict(row)
    notes = [n for n in (row.get("missing_data") or []) if isinstance(n, str)]
    if not any(n.startswith(prefix) for n in notes):
        notes.append(f"{prefix} ({detail})")
    row["missing_data"] = notes
    return row


def _load_attempt_json(path: Path, expected_id: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """(record, "") if readable and valid; (None, reason) otherwise."""
    try:
        rec = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"unreadable: {exc}"
    problem = record_problem(rec, expected_id)
    if problem:
        return None, f"invalid: {problem}"
    return rec, ""


def read_attempts(run_dir: Path) -> List[Dict[str, Any]]:
    """The ledger, made safe for every reader.

    - Each attempts.jsonl line must be a valid record (LedgerCorrupt names the line otherwise).
    - A row still `in_progress` whose answers/<id>/attempt.json is valid is replaced by that
      terminal record (`ledger_recovered_from`); an unreadable or invalid attempt.json leaves the
      row as it is with one missing_data note.
    - An answers/<id>/ directory missing from the ledger is adopted when its attempt.json is
      valid and belongs to this run (same dataset and implementation sha256); a non-empty one
      with no usable attempt.json becomes an `in_progress` placeholder so a possibly-sent
      attempt never disappears from the denominator. Empty directories (a reservation whose
      request has not started) and foreign directories are not counted; see
      foreign_answer_dirs.
    """
    path = run_dir / "attempts.jsonl"
    out: List[Dict[str, Any]] = []
    if path.exists():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise LedgerCorrupt(f"{path} line {n} is not valid JSON ({exc.msg})") from None
            problem = record_problem(row)
            if problem:
                raise LedgerCorrupt(f"{path} line {n} is not a usable attempt record: {problem}")
            if row["terminal_status"] == "in_progress":
                durable = run_dir / row["answer_dir"] / ATTEMPT_RECORD
                if durable.exists():
                    rec, why = _load_attempt_json(durable, row["attempt_id"])
                    if rec is None:
                        kind = why.split(":", 1)[0]
                        row = _note_once(
                            row,
                            f"{row['answer_dir']}/{ATTEMPT_RECORD} exists but is {kind}",
                            why.split(": ", 1)[1],
                        )
                    else:
                        row = rec
                        row[RECOVERED_NOTE] = f"{row['answer_dir']}/{ATTEMPT_RECORD}"
            out.append(row)
    adopted, _ = _scan_unlisted(run_dir, {a["attempt_id"] for a in out})
    return out + adopted


def foreign_answer_dirs(run_dir: Path) -> List[Dict[str, str]]:
    """answers/ directories not in the ledger that cannot be adopted as this run's attempts."""
    known: set = set()
    path = run_dir / "attempts.jsonl"
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and isinstance(row.get("attempt_id"), str):
                known.add(row["attempt_id"])
    return _scan_unlisted(run_dir, known)[1]


def _run_identity(run_dir: Path) -> Tuple[Optional[str], Optional[str], Dict[str, Any]]:
    try:
        manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, None, {}
    if not isinstance(manifest, dict):
        return None, None, {}
    raw_config = manifest.get("config")
    config: Dict[str, Any] = raw_config if isinstance(raw_config, dict) else {}
    return (
        config.get("dataset_sha256"),
        manifest.get("implementation_sha256"),
        {
            "run_id": manifest.get("run_id") if isinstance(manifest.get("run_id"), str) else None,
            "pilot_only": config.get("pilot_only"),
            "synthetic": config.get("synthetic"),
        },
    )


def _scan_unlisted(run_dir: Path, known: set) -> Tuple[List[Dict[str, Any]], List[Dict[str, str]]]:
    answers = run_dir / "answers"
    if not answers.is_dir():
        return [], []
    dataset_sha, impl_sha, run_info = _run_identity(run_dir)
    adopted: List[Dict[str, Any]] = []
    foreign: List[Dict[str, str]] = []
    for d in sorted(answers.iterdir()):
        if not d.is_dir() or d.name in known:
            continue
        if not ATTEMPT_ID.fullmatch(d.name):
            foreign.append({"dir": f"answers/{d.name}", "reason": "not an attempt ID"})
            continue
        if is_effectively_empty(d):
            continue  # reserved; the request has not started
        rec, why = (None, "missing")
        durable = d / ATTEMPT_RECORD
        if durable.is_file():
            rec, why = _load_attempt_json(durable, d.name)
        if rec is not None:
            if impl_sha is None or dataset_sha is None:
                foreign.append(
                    {
                        "dir": f"answers/{d.name}",
                        "reason": "ownership cannot be checked: the run manifest has no "
                        "dataset or implementation sha256",
                    }
                )
            elif (
                rec.get("dataset_sha256") != dataset_sha
                or rec.get("implementation_sha256") != impl_sha
            ):
                foreign.append(
                    {
                        "dir": f"answers/{d.name}",
                        "reason": "attempt.json is from a different dataset or implementation",
                    }
                )
            else:
                rec[RECOVERED_NOTE] = f"answers/{d.name}/{ATTEMPT_RECORD}"
                adopted.append(rec)
            continue
        placeholder: Dict[str, Any] = dict.fromkeys(ATTEMPT_COLUMNS)
        placeholder.update(
            schema=ATTEMPT_SCHEMA,
            attempt_id=d.name,
            run_id=run_info.get("run_id") or run_dir.name,
            question_id=d.name.split("-", 1)[0],
            answer_dir=f"answers/{d.name}",
            terminal_status="in_progress",
            failure_class=FAILURE_CLASS["in_progress"],
            pilot_only=bool(run_info.get("pilot_only")),
            synthetic=bool(run_info.get("synthetic")),
            review_status="not_applicable",
            missing_data=[PLACEHOLDER_NOTE, f"attempt.json {why}"],
        )
        placeholder[PLACEHOLDER_MARK] = f"answers/{d.name}"
        adopted.append(placeholder)
    return adopted, foreign


HEALED_NOTE = "ledger entry restored from attempt.json after a failed ledger update"


def write_attempts(run_dir: Path, attempts: List[Dict[str, Any]]) -> None:
    """Write the ledger. A recovered row becomes an ordinary row: `ledger_recovered_from` is
    dropped (the ledger is now correct) and a missing_data note keeps the history."""
    rows: List[Dict[str, Any]] = []
    for a in attempts:
        for marker, note in (
            (RECOVERED_NOTE, HEALED_NOTE),
            (PLACEHOLDER_MARK, PLACEHOLDER_HEALED_NOTE),
        ):
            if marker in a:
                a = {k: v for k, v in a.items() if k != marker}
                notes = list(a.get("missing_data") or [])
                if note not in notes:
                    notes.append(note)
                a["missing_data"] = notes
        rows.append(a)
    write_text_atomic(
        run_dir / "attempts.jsonl",
        "".join(json.dumps(a, ensure_ascii=False, sort_keys=True) + "\n" for a in rows),
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
    # ---- Phase 1, before the request. Any failure here means nothing was sent: it ends as
    # RunRefused (exit 1). The reserved attempt directory is removed when it holds nothing but
    # dotfiles and its ledger row is gone; otherwise it stays, and its ID is never reissued.
    answer_dir: Optional[Path] = None
    attempt_id = ""
    ledger_written = False  # True once the in_progress row may be in attempts.jsonl
    try:
        with ledger_lock(run_dir):
            manifest, _, attempt_id, answer_dir = _reserve_attempt(
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
        rel = answer_dir.relative_to(run_dir).as_posix()
        _note_foreign_dirs(run_dir)
        query = f"{question['question']}\n\n{freeze['output_instruction']}"
        record = _initial_record(
            attempt_id,
            run_dir,
            question_id,
            question,
            freeze,
            ds,
            impl_sha,
            commit,
            dirty,
            mode,
            nocache,
            silence_timeout,
            deadline,
            query,
            rel,
            pilot_only,
            synthetic,
        )
        # Recorded before the request, so a crash leaves a visible in_progress attempt. Re-read
        # under the lock so a parallel process's record is never dropped.
        with ledger_lock(run_dir):
            rows = read_attempts(run_dir)
            ledger_written = True
            write_attempts(run_dir, [*rows, record])
    except BaseException as exc:
        row_gone = not ledger_written or _remove_ledger_row(run_dir, attempt_id)
        if answer_dir is not None and row_gone:
            _remove_if_empty(answer_dir)  # kept if its row could not be removed
        stray = ""
        if not row_gone:
            stray = (
                f"; the in_progress row for {attempt_id} could not be removed from "
                f"{run_dir / 'attempts.jsonl'}, but it was never sent. To clear it, delete that "
                f"line and the empty directory {answer_dir}; until then it counts as an "
                "attempt with no terminal record"
            )
        if not isinstance(exc, Exception):
            if stray:
                try:
                    sys.stderr.write(f"interrupted before the request{stray}.\n")
                except (OSError, ValueError):
                    pass
            raise
        if isinstance(exc, RunRefused):
            if stray:
                raise RunRefused(f"{exc}{stray}") from exc
            raise
        raise RunRefused(f"{type(exc).__name__}: {exc}; nothing was sent{stray}") from exc

    # ---- Phase 2 and 3: the request has been (or is being) sent. From here on, every failure
    # ends as the post-request outcome: the terminal record is kept in attempt.json, the exit is
    # 13, and the message says the request was sent. Never "refused", never a traceback.
    kwargs: Dict[str, Any] = {}
    if post_terminal_grace is not None:
        kwargs["post_terminal_grace"] = post_terminal_grace
    code: Optional[int] = None
    failure: Optional[BaseException] = None
    try:
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
    except BaseException as exc:  # recorded, then re-raised if it is not an Exception
        failure = exc
        _mark_unrecorded(record, exc, code)
    durable_error = _write_durable(answer_dir, record)
    ledger_error: Optional[BaseException] = None
    try:
        with ledger_lock(run_dir):
            current = read_attempts(run_dir)
            if not any(a.get("attempt_id") == attempt_id for a in current):
                current.append(record)
            write_attempts(
                run_dir, [record if a.get("attempt_id") == attempt_id else a for a in current]
            )
    except Exception as exc:
        ledger_error = exc
    if failure is not None or durable_error is not None or ledger_error is not None:
        _report_post_request_failure(
            attempt_id, record, code, run_dir, answer_dir, failure, durable_error, ledger_error
        )
        if failure is not None and not isinstance(failure, Exception):
            raise failure
        return EXIT_LEDGER_NOT_UPDATED
    try:
        if not quiet:
            stdout.write(
                f"attempt {attempt_id}: {record['terminal_status']} (exit {code}), "
                f"{record['source_count'] if record['source_count'] is not None else 'no'} "
                f"sources -> {run_dir / 'attempts.jsonl'}\n"
            )
            if record["terminal_status"] == "complete":
                stdout.write(f"next: cited-research-accept prepare-review --run {run_dir}\n")
            stdout.flush()
    except (OSError, ValueError):  # ValueError: write to a closed stream
        pass  # the outcome is already recorded; a closed stdout cannot change it
    assert code is not None
    return code


def _note_foreign_dirs(run_dir: Path) -> None:
    try:
        for f in foreign_answer_dirs(run_dir):
            sys.stderr.write(
                f"note: {run_dir / f['dir']} is not this run's attempt ({f['reason']}); "
                "it is not counted\n"
            )
    except (OSError, ValueError):
        pass


def _remove_ledger_row(run_dir: Path, attempt_id: str) -> bool:
    """Best-effort removal of a pre-request in_progress row when phase 1 failed after writing
    it: nothing was sent, so the row would only pollute denominators and block the question."""
    try:
        with ledger_lock(run_dir):
            rows = read_attempts(run_dir)
            if any(a.get("attempt_id") == attempt_id for a in rows):
                write_attempts(run_dir, [a for a in rows if a.get("attempt_id") != attempt_id])
    except Exception:
        return False
    return True


def is_effectively_empty(path: Path) -> bool:
    """True if the directory holds nothing but dotfiles (.DS_Store and the like), which the OS
    or a file browser may add to a reservation whose request never started. A dot-named
    directory or any other entry makes it non-empty; an unreadable directory is non-empty."""
    try:
        return all(e.name.startswith(".") and e.is_file() for e in path.iterdir())
    except OSError:
        return False


def _remove_if_empty(path: Path) -> None:
    """Remove a never-sent reservation. Only when it is effectively empty: its dotfiles are
    removed first, nothing else ever is; anything unexpected leaves the directory in place."""
    if not is_effectively_empty(path):
        return
    try:
        for e in path.iterdir():
            # Re-checked per entry: anything that appeared since the check above is left alone,
            # and rmdir then fails safely because the directory is not empty.
            if e.name.startswith(".") and e.is_file():
                e.unlink()
        path.rmdir()
    except OSError:
        pass


def _initial_record(
    attempt_id: str,
    run_dir: Path,
    question_id: str,
    question: Dict[str, Any],
    freeze: Dict[str, Any],
    ds: Dataset,
    impl_sha: str,
    commit: Optional[str],
    dirty: Optional[bool],
    mode: str,
    nocache: bool,
    silence_timeout: Optional[float],
    deadline: Optional[float],
    query: str,
    rel: str,
    pilot_only: bool,
    synthetic: bool,
) -> Dict[str, Any]:
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
    return record


def _mark_unrecorded(record: Dict[str, Any], exc: BaseException, code: Optional[int]) -> None:
    """The request went out but its outcome could not be read or recorded."""
    what = scrub_text(f"{type(exc).__name__}: {exc}")
    record.update(
        terminal_status="outcome_unrecorded",
        failure_class=FAILURE_CLASS["outcome_unrecorded"],
        exit_code=code,
        error=("the request was sent" if code is not None else "the request may have been sent")
        + f"; recording its outcome failed: {what}",
        provider_task_state="unknown",
        review_status="not_applicable",
        terminal_at_utc=utc_now_iso(),
        missing_data=[
            "request sent; outcome not recorded by the client (see error)",
            "usage unavailable",
        ],
    )


def _write_durable(answer_dir: Path, record: Dict[str, Any]) -> Optional[BaseException]:
    try:
        write_text_atomic(
            answer_dir / ATTEMPT_RECORD, json.dumps(record, indent=2, sort_keys=True) + "\n"
        )
    except Exception as exc:
        return exc
    return None


def _report_post_request_failure(
    attempt_id: str,
    record: Dict[str, Any],
    code: Optional[int],
    run_dir: Path,
    answer_dir: Path,
    failure: Optional[BaseException],
    durable_error: Optional[BaseException],
    ledger_error: Optional[BaseException],
) -> None:
    if failure is not None and code is None:
        # The runner raised instead of returning: it may have failed before opening the request.
        lines = [f"attempt {attempt_id}: the request may have been sent (the runner failed)."]
    else:
        lines = [f"attempt {attempt_id}: the request was sent."]
    if failure is None:
        lines.append(f"It finished with status {record['terminal_status']} (exit {code}).")
    else:
        lines.append(
            "Its outcome could not be recorded "
            f"({scrub_text(type(failure).__name__ + ': ' + str(failure))}); status "
            "outcome_unrecorded."
        )
    ledger = run_dir / "attempts.jsonl"
    durable = answer_dir / ATTEMPT_RECORD
    if durable_error is None and ledger_error is None:
        lines.append(f"The attempt record is in {durable} and {ledger} was updated.")
    elif durable_error is None and isinstance(ledger_error, LedgerCorrupt):
        lines.append(
            f"The attempt record is in {durable}. {ledger} is corrupt ({ledger_error}), so it "
            "was not updated, and summarize and run-one refuse until it is fixed. Fix that "
            "line; do not delete it: it may be the only record of another attempt. Then "
            f"summarize and the next run-one recover this attempt from {durable}."
        )
    elif durable_error is None:
        lines.append(
            f"The attempt record is in {durable}. {ledger} could not be updated "
            f"({ledger_error}); summarize and the next run-one read the attempt record and "
            "report it as recovered."
        )
    elif ledger_error is None:
        lines.append(
            f"{durable} could not be written ({durable_error}), but {ledger} was updated with "
            "this outcome."
        )
    else:
        lines.append(
            f"{durable} could not be written ({durable_error}) and {ledger} could not be updated "
            f"({ledger_error}). The ledger still shows this attempt as in_progress, and nothing "
            f"can recover its outcome automatically; the files in {answer_dir} and this message "
            "are what remains."
        )
    lines.append("Do not re-run the request to repair the records.")
    try:
        sys.stderr.write(" ".join(lines) + "\n")
    except (OSError, ValueError):  # ValueError: write to a closed stream
        pass


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
        runtime = {
            "python_version": platform.python_version(),
            "tabstack_version": tabstack.__version__,
        }
        differs = [
            f"{k} {manifest.get(k)} -> {v}" for k, v in runtime.items() if manifest.get(k) != v
        ]
        if differs:
            raise RunRefused(
                f"{run_dir} was started with a different runtime ({'; '.join(differs)}); every "
                "attempt in a run must use the same Python and tabstack SDK; start a new run "
                "directory"
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
