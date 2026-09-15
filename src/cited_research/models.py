from __future__ import annotations

import hashlib
import json
import platform
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = 1


class ResearchTaskError(RuntimeError):
    """The research stream reported a task-level failure or ended without `complete`."""

    def __init__(
        self, message: str, activity: Optional[str] = None, iteration: Optional[float] = None
    ):
        super().__init__(message)
        self.activity = activity
        self.iteration = iteration


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
    completed_at_utc: Optional[str] = None
    duration_ms: Optional[int] = None
    first_event_ms: Optional[int] = None
    terminal_status: str = "unknown"
    event_counts: Dict[str, int] = field(default_factory=dict)
    event_sequence: List[str] = field(default_factory=list)
    pages_analyzed: Optional[float] = None
    cited_page_count: int = 0
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
