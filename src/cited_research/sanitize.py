"""Allowlist sanitizer for research SSE events destined for public artifacts."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

# Scalar, non-sensitive metadata that may be kept per event. Anything not listed is dropped.
ALLOWED_DATA_KEYS = frozenset(
    {
        "message",
        "timestamp",
        "iteration",
        "max_iterations",
        "attempt",
        "urls_found",
        "urls_new",
        "complexity",
        "activity",
    }
)

# Never allowed regardless of nesting.
DENIED_KEY_PATTERN = re.compile(
    r"(api[_-]?key|authorization|cookie|secret|token|password|stack|full_?text|env|"
    r"headers?|reasoning|thinking|account|org(anization)?_?id|email)",
    re.IGNORECASE,
)

SECRET_ENV_VARS = ("TABSTACK_API_KEY", "SEARCH_API_KEY", "MODEL_API_KEY")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _scrub_secrets(text: str) -> str:
    for name in SECRET_ENV_VARS:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            text = text.replace(value, "[REDACTED]")
    return text


def sanitize_event(
    event_name: str, data: Any, received_at_utc: Optional[str] = None
) -> Dict[str, Any]:
    """Return an allowlisted, JSON-serialisable record for one event."""
    record: Dict[str, Any] = {
        "event": event_name,
        "received_at_utc": received_at_utc or utc_now_iso(),
    }
    raw: Dict[str, Any]
    if hasattr(data, "model_dump"):
        raw = data.model_dump()
    elif isinstance(data, dict):
        raw = data
    else:
        raw = {}

    for key, value in raw.items():
        if key not in ALLOWED_DATA_KEYS or DENIED_KEY_PATTERN.search(key):
            continue
        if isinstance(value, (str, int, float, bool)) or value is None:
            record[key] = _scrub_secrets(value) if isinstance(value, str) else value

    if event_name == "complete":
        metadata = raw.get("metadata") or {}
        record["pages_analyzed"] = metadata.get("total_pages_analyzed")
        cited = metadata.get("cited_pages")
        record["cited_page_count"] = len(cited) if cited else 0
        record["report_chars"] = len(raw.get("report") or "")
    if event_name == "error":
        err = raw.get("error") or {}
        record["error_name"] = err.get("name")
        record["error_message"] = _scrub_secrets(str(err.get("message", "")))
    return record


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def scrub_text(text: str) -> str:
    """Public wrapper used for stdout/stderr captures."""
    return _scrub_secrets(text)
