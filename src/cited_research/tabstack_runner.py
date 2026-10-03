"""Run one Tabstack /research call.

Persists report, cited pages, a sanitized lifecycle trace, a reviewer worksheet, a trace diagram,
and a manifest."""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Callable, Dict, Iterable, List, Optional, TextIO, Tuple

import httpx
import tabstack
from tabstack import Tabstack

from .models import (
    CitedPage,
    MalformedCompleteError,
    PrematureCloseError,
    ProtocolError,
    ResearchTaskError,
    RunManifest,
    SilenceTimeoutError,
    Source,
    build_cited_pages,
    sha256_text,
)
from .review import review_sheet_csv, trace_diagram_md
from .sanitize import (
    TERMINAL_EVENTS,
    append_jsonl,
    redact_message,
    safe_event_name,
    sanitize_event,
    scrub_text,
    utc_now_iso,
    write_jsonl_atomic,
    write_text_atomic,
)

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

# How long to keep listening after `complete` for the stream to close (or misbehave).
POST_TERMINAL_GRACE_SECONDS = 2.0

EXIT_CODES = {
    "complete": 0,
    "task_error": 2,
    "http_error": 3,
    "transport_error": 4,
    "premature_close": 6,
    "silence_timeout": 7,
    "protocol_error": 8,
    "stream_transport_error": 9,
    "malformed_complete": 10,
    "unexpected_error": 11,
}


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
        parts.append(redact_message(str(message)))
    return "  ".join(parts)


def write_sources(path: Path, sources: Iterable[Source]) -> None:
    path.write_text(
        json.dumps([asdict(s) for s in sources], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# consume_stream / persist_complete / write_sources are the original sample-run path. The
# evaluation harness (System B, frozen for the evaluation protocol) still imports them; do not
# change their behavior.
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


class EventPump:
    """Reads the stream on a worker thread so the caller can wait with a timeout.

    The request itself is opened on the worker too, so a silent server is detected whether it
    stalls before the first event or between events. Items are (kind, payload, elapsed_ms) with
    kind one of "event", "end", "exc"; elapsed_ms is taken when the item arrives.
    """

    def __init__(self, open_stream: Callable[[], Iterable[Any]], started: float):
        self._open = open_stream
        self._started = started
        self._queue: queue.Queue[Tuple[str, Any, int]] = queue.Queue()
        self._stream: Any = None
        self._thread = threading.Thread(target=self._run, name="research-stream", daemon=True)

    def _ms(self) -> int:
        return int((perf_counter() - self._started) * 1000)

    def _run(self) -> None:
        try:
            self._stream = self._open()
            for event in self._stream:
                self._queue.put(("event", event, self._ms()))
            self._queue.put(("end", None, self._ms()))
        except BaseException as exc:  # handed to the caller, never swallowed
            self._queue.put(("exc", exc, self._ms()))

    def start(self) -> EventPump:
        self._thread.start()
        return self

    def get(self, timeout: Optional[float]) -> Tuple[str, Any, int]:
        """Raises queue.Empty when nothing arrives within `timeout` seconds."""
        return self._queue.get(timeout=timeout)

    def close(self) -> None:
        close = getattr(self._stream, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass


def consume_trace(
    pump: EventPump,
    records: List[Dict[str, Any]],
    manifest: RunManifest,
    printer: Printer,
    quiet: bool = False,
    silence_timeout: Optional[float] = None,
    post_terminal_grace: float = POST_TERMINAL_GRACE_SECONDS,
) -> Any:
    """Drive one research stream to a terminal state. Returns the `complete` event.

    Raises ResearchTaskError (streamed `error`), PrematureCloseError (stream ended first),
    SilenceTimeoutError (no event within `silence_timeout`), ProtocolError (a second terminal
    event), or whatever the SDK raised opening the request. Sanitized records are appended to
    `records` as they arrive so every exit path can still write the timeline.
    """
    final_event: Any = None
    while True:
        timeout = post_terminal_grace if final_event is not None else silence_timeout
        try:
            kind, payload, elapsed_ms = pump.get(timeout)
        except queue.Empty:
            if final_event is not None:
                manifest.stream_closed_after_terminal = False
                manifest.caveats.append(
                    f"stream still open {post_terminal_grace:g}s after complete; closed locally"
                )
                return final_event
            raise SilenceTimeoutError(
                f"no event for {silence_timeout:g}s (client-side silence timeout)"
            ) from None

        if kind == "end":
            if final_event is not None:
                manifest.stream_closed_after_terminal = True
                return final_event
            raise PrematureCloseError("Research stream ended without a complete or error event")
        if kind == "exc":
            if final_event is not None:
                manifest.caveats.append(
                    f"transport error after complete ignored: {type(payload).__name__}"
                )
                return final_event
            raise payload

        event = payload
        name = safe_event_name(getattr(event, "event", None))
        if manifest.first_event_ms is None:
            manifest.first_event_ms = elapsed_ms
        manifest.event_counts[name] = manifest.event_counts.get(name, 0) + 1
        manifest.event_sequence.append(name)
        record = sanitize_event(
            name,
            getattr(event, "data", None),
            utc_now_iso(),
            len(records) + 1,
            elapsed_ms,
            timestamp_key="timestamp_raw",
        )
        records.append(record)
        ts_type = record.get("timestamp_type")
        if ts_type and ts_type not in manifest.timestamp_types:
            manifest.timestamp_types.append(ts_type)

        if final_event is not None:
            if name in TERMINAL_EVENTS:
                raise ProtocolError(f"second terminal event '{name}' after complete")
            manifest.caveats.append(f"event '{name}' arrived after complete")
            continue

        if name in PROGRESS_EVENTS and not quiet:
            printer(_progress_line(event))

        if name == "error":
            data = getattr(event, "data", None)
            raise ResearchTaskError(
                record.get("error_message") or "research task failed",
                activity=getattr(data, "activity", None),
                iteration=getattr(data, "iteration", None),
            )
        if name == "complete":
            final_event = event


def persist_trace(
    final_event: Any, output_dir: Path, manifest: RunManifest
) -> Tuple[str, List[CitedPage]]:
    data = final_event.data
    report = getattr(data, "report", None)
    if not isinstance(report, str):
        raise MalformedCompleteError("complete event has no report string")
    metadata = getattr(data, "metadata", None)
    cited = getattr(metadata, "cited_pages", None)
    if cited is not None and not isinstance(cited, list):
        manifest.caveats.append("metadata.cited_pages was not a list; treated as []")
        cited = None
    pages = build_cited_pages(cited)
    write_text_atomic(output_dir / "report.md", report.rstrip() + "\n")
    write_text_atomic(
        output_dir / "sources.json",
        json.dumps([asdict(p) for p in pages], indent=2, ensure_ascii=False) + "\n",
    )
    total = getattr(metadata, "total_pages_analyzed", None)
    manifest.pages_analyzed = total if isinstance(total, (int, float)) else None
    manifest.cited_page_count = len(pages)
    manifest.rejected_link_count = sum(1 for p in pages if not p.link_ok)
    manifest.review_state = "review_needed" if pages else "review_needed_no_sources"
    if not pages:
        manifest.caveats.append("complete carried no cited_pages; sources=[]")
    if any(p.malformed_fields for p in pages):
        manifest.caveats.append("one or more cited pages had malformed fields; see sources.json")
    if pages and all(not p.claims for p in pages):
        manifest.caveats.append("every cited page has claims=[]")
    manifest.terminal_status = "complete"
    return report, pages


def no_retry_client() -> Tabstack:
    """SDK client with transport retries off.

    The SDK's default (2) re-sends the POST on connection errors, 408, 409, 429 and 5xx. A request
    that failed at the connection level may already have been accepted and billed, so the trace
    path fails fast and lets the caller decide.
    """
    return Tabstack(max_retries=0)


STANDING_CAVEATS = (
    "Timeline is a request lifecycle, not a source-level execution trace.",
    "complete is evidence the task terminated, not that citations are correct.",
    "API `claims` are machine-generated and do not verify the page.",
    "Inline [n] markers are joined to cited_pages by position (SDK: ordered by first citation).",
)


def run_research(
    query: str,
    mode: str,
    nocache: bool,
    fetch_timeout: Optional[int],
    output_dir: Path,
    quiet: bool = False,
    stdout: TextIO = sys.stdout,
    client_factory: Callable[[], Tabstack] = no_retry_client,
    silence_timeout: Optional[float] = None,
    command: Optional[str] = None,
    post_terminal_grace: float = POST_TERMINAL_GRACE_SECONDS,
) -> int:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "question.txt").write_text(query + "\n", encoding="utf-8")
    if command is not None:
        (output_dir / "command.txt").write_text(scrub_text(command) + "\n", encoding="utf-8")

    def printer(line: str) -> None:
        stdout.write(line + "\n")
        stdout.flush()

    manifest = RunManifest(
        query_sha256=sha256_text(query),
        mode=mode,
        nocache=nocache,
        fetch_timeout_seconds=fetch_timeout,
        silence_timeout_seconds=silence_timeout,
        started_at_utc=utc_now_iso(),
        tabstack_version=tabstack.__version__,
        repository_commit=_git_commit(),
    )
    manifest.caveats.extend(STANDING_CAVEATS)
    records: List[Dict[str, Any]] = []
    pages: List[CitedPage] = []
    report = ""
    status = "complete"
    message = ""

    kwargs: dict = {"query": query, "mode": mode, "nocache": nocache}
    if fetch_timeout is not None:
        kwargs["fetch_timeout"] = fetch_timeout

    started = perf_counter()
    try:
        with client_factory() as client:
            manifest.sdk_max_retries = client.max_retries
            pump = EventPump(lambda: client.agent.research(**kwargs), started).start()
            try:
                final_event = consume_trace(
                    pump, records, manifest, printer, quiet, silence_timeout, post_terminal_grace
                )
            finally:
                pump.close()
            report, pages = persist_trace(final_event, output_dir, manifest)
    except ResearchTaskError as exc:
        status = "task_error"
        where = f" during {redact_message(str(exc.activity))}" if exc.activity else ""
        message = f"research failed{where}: {redact_message(str(exc))}"
    except PrematureCloseError as exc:
        status = "premature_close"
        message = f"stream closed early: {exc}"
    except SilenceTimeoutError as exc:
        status = "silence_timeout"
        message = f"gave up waiting: {exc}. The request may still be running and billed."
    except MalformedCompleteError as exc:
        status = "malformed_complete"
        message = f"unusable complete event: {exc}"
    except ProtocolError as exc:
        status = "protocol_error"
        message = f"unexpected stream behavior: {exc}"
    except tabstack.APIStatusError as exc:
        status = "http_error"
        message = f"request rejected (HTTP {exc.status_code}): {redact_message(exc.message)}"
    except tabstack.APIConnectionError as exc:
        status = "transport_error"
        message = f"connection failed: {redact_message(str(exc))}"
    except httpx.TransportError as exc:
        # Raised while iterating the stream (RemoteProtocolError, ReadTimeout, ...): the SDK
        # wraps errors opening the request, not errors reading it. The request was accepted.
        status = "stream_transport_error"
        message = (
            f"connection failed mid-stream: {type(exc).__name__}: {redact_message(str(exc))}. "
            "The request was accepted and may have been billed."
        )
    except Exception as exc:
        status = "unexpected_error"
        message = f"unexpected failure: {type(exc).__name__}: {redact_message(str(exc))}"

    if status != "complete":
        manifest.terminal_status = status
        manifest.error = scrub_text(message)
        manifest.review_state = "not_applicable"
    write_jsonl_atomic(output_dir / "events.sanitized.jsonl", records)
    if status == "complete":
        write_text_atomic(output_dir / "review-sheet.csv", review_sheet_csv(report, pages))
    write_text_atomic(
        output_dir / "trace-diagram.md",
        trace_diagram_md(records, manifest.terminal_status, pages, manifest.review_state),
    )
    _finish(manifest, output_dir / "run-manifest.json", started)

    if status != "complete":
        sys.stderr.write(scrub_text(message) + "\n")
        return EXIT_CODES[status]
    if not quiet:
        printer(f"complete  report -> {output_dir / 'report.md'}")
        printer(f"sources ({len(pages)}) -> {output_dir / 'sources.json'}")
        for p in pages:
            shown = p.url if p.link_ok else f"[not linked: {p.link_issue}]"
            printer(f"  {p.position}. {p.title or '(untitled)'}  {shown}")
        printer(f"review sheet -> {output_dir / 'review-sheet.csv'} (unreviewed)")
    return 0


def _finish(manifest: RunManifest, path: Path, started: float) -> None:
    manifest.completed_at_utc = utc_now_iso()
    manifest.duration_ms = int((perf_counter() - started) * 1000)
    manifest.write(path)
