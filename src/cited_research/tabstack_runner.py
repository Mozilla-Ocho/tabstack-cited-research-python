"""Run one Tabstack /research call.

Persists report, sources, manifest, and a sanitized event log."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Iterable, List, Optional, TextIO

import tabstack
from tabstack import Tabstack

from .models import ResearchTaskError, RunManifest, Source, sha256_text
from .sanitize import append_jsonl, sanitize_event, utc_now_iso

PROGRESS_EVENTS = frozenset(
    {
        "start",
        "planning:start",
        "planning:end",
        "iteration:start",
        "iteration:end",
        "searching:start",
        "searching:end",
        "writing:start",
        "writing:end",
    }
)

Printer = Callable[[str], None]


def _git_commit() -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def _progress_line(event: Any) -> str:
    data = event.data
    parts: List[str] = [event.event]
    iteration = getattr(data, "iteration", None)
    max_iterations = getattr(data, "max_iterations", None)
    if iteration is not None:
        parts.append(
            f"iteration {int(iteration)}" + (f"/{int(max_iterations)}" if max_iterations else "")
        )
    urls_new = getattr(data, "urls_new", None)
    if urls_new is not None:
        parts.append(f"{int(urls_new)} new urls")
    message = getattr(data, "message", None)
    if message:
        parts.append(str(message))
    return "  ".join(parts)


def write_sources(path: Path, sources: Iterable[Source]) -> None:
    path.write_text(
        json.dumps([asdict(s) for s in sources], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def consume_stream(
    events: Iterable[Any],
    output_dir: Path,
    manifest: RunManifest,
    started: float,
    printer: Printer,
    quiet: bool = False,
) -> Any:
    """Iterate a research event stream. Returns the `complete` event or raises ResearchTaskError.

    Separated from the client call so fixture tests can drive it with synthetic events.
    """
    log_path = output_dir / "events.sanitized.jsonl"
    final_event: Any = None
    for event in events:
        now_ms = int((perf_counter() - started) * 1000)
        if manifest.first_event_ms is None:
            manifest.first_event_ms = now_ms
        name = event.event
        manifest.event_counts[name] = manifest.event_counts.get(name, 0) + 1
        manifest.event_sequence.append(name)
        append_jsonl(log_path, sanitize_event(name, event.data, utc_now_iso()))

        if name in PROGRESS_EVENTS and not quiet:
            printer(_progress_line(event))

        if name == "error":
            err = event.data.error
            raise ResearchTaskError(
                err.message,
                activity=getattr(event.data, "activity", None),
                iteration=getattr(event.data, "iteration", None),
            )
        if name == "complete":
            final_event = event
            break

    if final_event is None:
        raise ResearchTaskError("Research stream ended without a complete event")
    return final_event


def persist_complete(final_event: Any, output_dir: Path, manifest: RunManifest) -> List[Source]:
    data = final_event.data
    (output_dir / "report.md").write_text(data.report.rstrip() + "\n", encoding="utf-8")
    cited = data.metadata.cited_pages or []
    sources = [Source.from_cited_page(p) for p in cited]
    write_sources(output_dir / "sources.json", sources)
    manifest.pages_analyzed = data.metadata.total_pages_analyzed
    manifest.cited_page_count = len(sources)
    manifest.terminal_status = "complete"
    return sources


def run_research(
    query: str,
    mode: str,
    nocache: bool,
    fetch_timeout: Optional[int],
    output_dir: Path,
    quiet: bool = False,
    stdout: TextIO = sys.stdout,
    client_factory: Callable[[], Tabstack] = Tabstack,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "question.txt").write_text(query + "\n", encoding="utf-8")

    def printer(line: str) -> None:
        stdout.write(line + "\n")
        stdout.flush()

    manifest = RunManifest(
        query_sha256=sha256_text(query),
        mode=mode,
        nocache=nocache,
        fetch_timeout_seconds=fetch_timeout,
        started_at_utc=utc_now_iso(),
        tabstack_version=tabstack.__version__,
        repository_commit=_git_commit(),
    )
    manifest_path = output_dir / "run-manifest.json"
    started = perf_counter()

    try:
        with client_factory() as client:
            manifest.sdk_max_retries = client.max_retries
            kwargs: dict = {"query": query, "mode": mode, "nocache": nocache}
            if fetch_timeout is not None:
                kwargs["fetch_timeout"] = fetch_timeout
            stream = client.agent.research(**kwargs)
            final_event = consume_stream(stream, output_dir, manifest, started, printer, quiet)
            sources = persist_complete(final_event, output_dir, manifest)
    except ResearchTaskError as exc:
        manifest.terminal_status = "task_error"
        manifest.error = str(exc)
        _finish(manifest, manifest_path, started)
        where = f" during {exc.activity}" if exc.activity else ""
        sys.stderr.write(f"research failed{where}: {exc}\n")
        return 2
    except tabstack.APIStatusError as exc:
        manifest.terminal_status = "http_error"
        manifest.error = f"HTTP {exc.status_code}: {exc.message}"
        _finish(manifest, manifest_path, started)
        sys.stderr.write(f"request rejected (HTTP {exc.status_code}): {exc.message}\n")
        return 3
    except tabstack.APIConnectionError as exc:
        manifest.terminal_status = "transport_error"
        manifest.error = str(exc)
        _finish(manifest, manifest_path, started)
        sys.stderr.write(f"connection failed: {exc}\n")
        return 4

    _finish(manifest, manifest_path, started)
    if not quiet:
        printer(f"complete  report -> {output_dir / 'report.md'}")
        printer(f"sources ({len(sources)}) -> {output_dir / 'sources.json'}")
        for s in sources:
            printer(f"  - {s.title or '(untitled)'}  {s.url}")
    return 0


def _finish(manifest: RunManifest, path: Path, started: float) -> None:
    manifest.completed_at_utc = utc_now_iso()
    manifest.duration_ms = int((perf_counter() - started) * 1000)
    manifest.write(path)
