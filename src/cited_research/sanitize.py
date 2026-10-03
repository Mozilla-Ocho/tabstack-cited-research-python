"""Allowlist sanitizer for research SSE events destined for public artifacts."""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

# Event names in the pinned SDK's typed event union (tabstack 2.8.5). Anything else is logged
# by name only and flagged `known_event: false`.
KNOWN_EVENTS = frozenset(
    {
        "start",
        "planning:start",
        "planning:end",
        "iteration:start",
        "iteration:end",
        "prefetching:start",
        "prefetching:end",
        "searching:start",
        "searching:end",
        "analyzing:start",
        "analyzing:end",
        "following:start",
        "following:end",
        "evaluating:start",
        "evaluating:end",
        "judging:start",
        "judging:end",
        "outlining:start",
        "outlining:end",
        "writing:start",
        "writing:end",
        "complete",
        "error",
    }
)
TERMINAL_EVENTS = frozenset({"complete", "error"})

# Scalar, non-sensitive metadata that may be kept per event. Anything not listed is dropped.
# `timestamp` is handled separately: kept raw, with its observed Python type alongside.
# The docs disagree on its type (guide: ISO string; API reference and SDK: number), so it is
# recorded as received and never reformatted.
ALLOWED_DATA_KEYS = frozenset(
    {
        "message",
        "iteration",
        "max_iterations",
        "attempt",
        "max_attempts",
        "pages_analyzed",
        "url_count",
        "urls_found",
        "urls_new",
        "complexity",
        "activity",
        "is_last",
        "stop_reason",
    }
)

# Never allowed regardless of nesting.
DENIED_KEY_PATTERN = re.compile(
    r"(api[_-]?key|authorization|cookie|secret|token|password|stack|full_?text|env|"
    r"headers?|reasoning|thinking|account|org(anization)?_?id|email)",
    re.IGNORECASE,
)

SECRET_ENV_VARS = ("TABSTACK_API_KEY", "SEARCH_API_KEY", "MODEL_API_KEY")

EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z_:-]{0,39}$")
URL_PATTERN = re.compile(r"\b(?:https?|ftp|file)://\S+", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
BEARER_PATTERN = re.compile(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", re.IGNORECASE)
KEYLIKE_PATTERN = re.compile(r"\b(sk|pk|tsk|key)[_-][A-Za-z0-9_-]{12,}")
MAX_MESSAGE_CHARS = 240


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scrub_secrets(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    return text


def redact_message(text: str) -> str:
    """Status messages are shown, but never carry URLs, emails, credentials, or long payloads.

    URLs are removed so a progress message cannot be read as proof that a page was fetched.
    """
    text = _scrub_secrets(text)
    text = BEARER_PATTERN.sub("[REDACTED]", text)
    text = KEYLIKE_PATTERN.sub("[REDACTED]", text)
    text = URL_PATTERN.sub("[url]", text)
    text = EMAIL_PATTERN.sub("[email]", text)
    text = " ".join(text.split())
    if len(text) > MAX_MESSAGE_CHARS:
        text = text[: MAX_MESSAGE_CHARS - 1] + "…"
    return text


def safe_event_name(name: Any) -> str:
    if isinstance(name, str) and EVENT_NAME_PATTERN.match(name):
        return name
    return "unrecognized"


def sanitize_event(
    event_name: str,
    data: Any,
    received_at_utc: Optional[str] = None,
    seq: Optional[int] = None,
    elapsed_ms: Optional[int] = None,
    timestamp_key: str = "timestamp",
) -> Dict[str, Any]:
    """Return an allowlisted, JSON-serialisable record for one event.

    `seq` is the 1-based arrival order and `elapsed_ms` is measured on the local monotonic clock
    from just before the request was sent. Both are local observations, not server data.
    The trace path passes `timestamp_key="timestamp_raw"`; the sample-run and harness logs keep
    `timestamp`.
    """
    name = safe_event_name(event_name)
    record: Dict[str, Any] = {
        "event": name,
        "known_event": name in KNOWN_EVENTS,
        "received_at_utc": received_at_utc or utc_now_iso(),
    }
    if seq is not None:
        record["seq"] = seq
    if elapsed_ms is not None:
        record["elapsed_ms"] = elapsed_ms

    raw: Dict[str, Any]
    if hasattr(data, "model_dump"):
        raw = data.model_dump(warnings=False)
    elif isinstance(data, dict):
        raw = data
    else:
        raw = {}

    if "timestamp" in raw:
        ts = raw["timestamp"]
        record["timestamp_type"] = type(ts).__name__
        if isinstance(ts, (int, float, str)) and not isinstance(ts, bool):
            record[timestamp_key] = redact_message(ts) if isinstance(ts, str) else ts

    for key, value in raw.items():
        if key not in ALLOWED_DATA_KEYS or DENIED_KEY_PATTERN.search(key):
            continue
        if isinstance(value, str):
            record[key] = redact_message(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            record[key] = value

    if name == "complete":
        metadata = raw.get("metadata") or {}
        if not isinstance(metadata, dict):
            metadata = {}
        pages = metadata.get("total_pages_analyzed")
        record["pages_analyzed"] = pages if isinstance(pages, (int, float)) else None
        cited = metadata.get("cited_pages")
        record["cited_page_count"] = len(cited) if isinstance(cited, list) else 0
        report = raw.get("report")
        record["report_chars"] = len(report) if isinstance(report, str) else 0
    if name == "error":
        err = raw.get("error") or {}
        if not isinstance(err, dict):
            err = {}
        err_name = err.get("name")
        record["error_name"] = redact_message(err_name) if isinstance(err_name, str) else None
        record["error_message"] = redact_message(str(err.get("message", "")))
    return record


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _umask_mode() -> int:
    # mkstemp creates 0600 files; give atomic writes the same mode a plain open() would.
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


def write_jsonl_atomic(path: Path, records: Iterable[Dict[str, Any]]) -> None:
    """Write the whole log at once via a temp file and rename, so readers never see a partial."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            for record in records:
                fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        os.chmod(tmp, _umask_mode())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        os.chmod(tmp, _umask_mode())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def scrub_text(text: str) -> str:
    """Public wrapper used for stdout/stderr captures."""
    return _scrub_secrets(text)
