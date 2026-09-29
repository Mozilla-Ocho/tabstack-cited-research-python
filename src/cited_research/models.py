from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .urls import check_public_url, strip_credentials, url_identity

SCHEMA_VERSION = 2


class ResearchTaskError(RuntimeError):
    """The research stream reported a task-level failure or ended without `complete`."""

    def __init__(
        self, message: str, activity: Optional[str] = None, iteration: Optional[float] = None
    ):
        super().__init__(message)
        self.activity = activity
        self.iteration = iteration


class PrematureCloseError(RuntimeError):
    """The stream ended before any terminal event (`complete` or `error`) arrived."""


class SilenceTimeoutError(RuntimeError):
    """No event arrived within the client-side silence window."""


class ProtocolError(RuntimeError):
    """The stream broke the documented contract, e.g. a second terminal event."""


def _str_list(value: Any) -> Optional[List[str]]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _opt_str(value: Any) -> Tuple[Optional[str], bool]:
    """Return (value, ok). Non-string, non-None values are dropped and reported."""
    if value is None or isinstance(value, str):
        return value, True
    return None, False


@dataclass
class Source:
    id: str
    url: str
    claims: List[str]
    source_queries: List[str]
    title: Optional[str] = None
    relevance: Optional[str] = None
    reliability: Optional[str] = None

    @classmethod
    def from_cited_page(cls, page: Any) -> Source:
        # Allowlist. full_text, summary, depth, parent_url and url_source are dropped
        # on purpose.
        return cls(
            id=page.id,
            url=page.url,
            claims=list(page.claims),
            source_queries=list(page.source_queries),
            title=getattr(page, "title", None),
            relevance=getattr(page, "relevance", None),
            reliability=getattr(page, "reliability", None),
        )


@dataclass
class CitedPage:
    """One cited page for the trace path: returned order, link check, malformed fields."""

    position: int
    id: Optional[str]
    url: Optional[str]
    claims: List[str]
    source_queries: List[str]
    title: Optional[str] = None
    relevance: Optional[str] = None
    reliability: Optional[str] = None
    link_ok: bool = False
    link_issue: Optional[str] = None
    duplicate_of_position: Optional[int] = None
    malformed_fields: List[str] = field(default_factory=list)

    @classmethod
    def from_cited_page(cls, page: Any, position: int) -> CitedPage:
        # Allowlist. full_text, summary, depth, parent_url and url_source are dropped
        # on purpose. Malformed fields are recorded, never guessed at.
        malformed: List[str] = []
        values: Dict[str, Any] = {}
        for name in ("id", "url", "title", "relevance", "reliability"):
            value, ok = _opt_str(getattr(page, name, None))
            if not ok:
                malformed.append(name)
            values[name] = value
        lists: Dict[str, List[str]] = {}
        for name in ("claims", "source_queries"):
            parsed = _str_list(getattr(page, name, None))
            if parsed is None:
                malformed.append(name)
                parsed = []
            lists[name] = parsed
        if not values["id"]:
            malformed.append("id")
        link_ok, link_issue = check_public_url(values["url"])
        if values["url"] is not None:
            values["url"] = strip_credentials(values["url"])
        return cls(
            position=position,
            claims=lists["claims"],
            source_queries=lists["source_queries"],
            link_ok=link_ok,
            link_issue=link_issue,
            malformed_fields=sorted(set(malformed)),
            **values,
        )


def build_cited_pages(cited_pages: Optional[Sequence[Any]]) -> List[CitedPage]:
    """Cited pages in returned order, 1-based, with likely duplicates flagged (not removed)."""
    sources = [CitedPage.from_cited_page(p, i) for i, p in enumerate(cited_pages or [], start=1)]
    flag_duplicates(sources)
    return sources


def flag_duplicates(pages: Sequence[CitedPage]) -> None:
    first_seen: Dict[str, int] = {}
    for page in pages:
        page.duplicate_of_position = None
        if not page.link_ok or page.url is None:
            continue
        key = url_identity(page.url)
        if key in first_seen:
            page.duplicate_of_position = first_seen[key]
        else:
            first_seen[key] = page.position


def load_cited_pages(path: Path) -> List[CitedPage]:
    """Read a trace-path sources.json back, re-running the link and duplicate checks."""
    pages: List[CitedPage] = []
    for record in json.loads(path.read_text(encoding="utf-8")):
        page = CitedPage(**record)
        page.link_ok, page.link_issue = check_public_url(page.url)
        pages.append(page)
    flag_duplicates(pages)
    return pages


@dataclass
class CreditEvidence:
    source: str = "unavailable"
    value: Optional[float] = None
    unit: str = "credits"
    notes: str = "Do not infer final call cost from the public per-action rate."


@dataclass
class RunManifest:
    query_sha256: str
    mode: str
    nocache: bool
    fetch_timeout_seconds: Optional[int]
    started_at_utc: str
    silence_timeout_seconds: Optional[float] = None
    completed_at_utc: Optional[str] = None
    duration_ms: Optional[int] = None
    first_event_ms: Optional[int] = None
    terminal_status: str = "unknown"
    event_counts: Dict[str, int] = field(default_factory=dict)
    event_sequence: List[str] = field(default_factory=list)
    pages_analyzed: Optional[float] = None
    cited_page_count: int = 0
    rejected_link_count: int = 0
    review_state: str = "not_applicable"
    stream_closed_after_terminal: Optional[bool] = None
    timestamp_types: List[str] = field(default_factory=list)
    caveats: List[str] = field(default_factory=list)
    python_version: str = field(default_factory=lambda: platform.python_version())
    os: str = field(default_factory=lambda: f"{platform.system()} {platform.release()}")
    architecture: str = field(default_factory=platform.machine)
    tabstack_version: str = ""
    sdk_max_retries: Optional[int] = None
    application_retries: int = 0
    repository_commit: Optional[str] = None
    credit_evidence: CreditEvidence = field(default_factory=CreditEvidence)
    schema_version: int = SCHEMA_VERSION
    error: Optional[str] = None

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def python_runtime() -> str:
    return sys.version.split()[0]
