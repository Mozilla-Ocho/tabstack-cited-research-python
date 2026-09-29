"""Reviewer worksheet and per-run trace diagram.

Nothing here judges a citation. The worksheet lays out candidate claims next to the source each
inline marker points at; a person fills in the passage and the judgment.
"""

from __future__ import annotations

import csv
import io
import re
from typing import Any, Dict, List, Optional, Sequence

from .models import CitedPage

# Provisional until the Understand-lane rubric is checked against this header. Keep the order
# stable; downstream tooling and the article reference these names.
REVIEW_COLUMNS: Sequence[str] = (
    "row_id",
    "claim_id",
    "report_excerpt",
    "citation_marker",
    "source_position",
    "source_id",
    "source_url",
    "source_title",
    "link_ok",
    "api_claims_for_source",
    "supporting_passage",
    "judgment",
    "reviewer",
    "reviewed_at_utc",
    "notes",
)
# Allowed values for `judgment`, filled in by a person. Empty means not yet reviewed.
JUDGMENTS = ("supported", "partially_supported", "not_supported", "source_unavailable")

MARKER_GROUP = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=\S)")
SOURCES_HEADING = re.compile(r"^\W*(sources|references|citations)\W*$", re.IGNORECASE)
LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def candidate_claims(report: str) -> List[str]:
    """Split the report into sentence-sized candidates. Headings and any trailing
    CitedPages/References block are skipped. This is a starting point for a reviewer, who merges,
    splits, or deletes rows; it is not claim extraction."""
    out: List[str] = []
    for line in report.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if SOURCES_HEADING.match(stripped.lstrip("#").strip()):
            break
        if stripped.startswith("#"):
            continue
        stripped = LIST_PREFIX.sub("", stripped)
        out.extend(s.strip() for s in SENTENCE_BREAK.split(stripped) if s.strip())
    return out


def markers_in(text: str) -> List[int]:
    seen: List[int] = []
    for group in MARKER_GROUP.findall(text):
        for n in group.split(","):
            value = int(n.strip())
            if value not in seen:
                seen.append(value)
    return seen


def review_rows(report: str, sources: Sequence[CitedPage]) -> List[Dict[str, str]]:
    by_position = {s.position: s for s in sources}
    rows: List[Dict[str, str]] = []

    def add(claim_id: str, excerpt: str, marker: str, src: Optional[CitedPage], notes: str) -> None:
        row = dict.fromkeys(REVIEW_COLUMNS, "")
        row.update(
            row_id=str(len(rows) + 1),
            claim_id=claim_id,
            report_excerpt=excerpt,
            citation_marker=marker,
            notes=notes,
        )
        if src is not None:
            row.update(
                source_position=str(src.position),
                source_id=src.id or "",
                source_url=(src.url or "") if src.link_ok else "",
                source_title=src.title or "",
                link_ok="yes" if src.link_ok else f"no ({src.link_issue})",
                api_claims_for_source=" | ".join(src.claims),
            )
        rows.append(row)

    for i, sentence in enumerate(candidate_claims(report), start=1):
        claim_id = f"C{i:02d}"
        markers = markers_in(sentence)
        if not markers:
            add(claim_id, sentence, "", None, "no inline citation")
            continue
        for n in markers:
            src = by_position.get(n)
            note = "" if src else f"marker [{n}] has no cited page at position {n}"
            if src is not None and src.duplicate_of_position:
                note = f"likely same page as position {src.duplicate_of_position}"
            add(claim_id, sentence, f"[{n}]", src, note)
    return rows


def _csv_safe(value: str) -> str:
    # Report and page text come from the web; stop spreadsheets from evaluating it as a formula.
    return "'" + value if value[:1] in ("=", "+", "-", "@", "\t", "\r") else value


def review_sheet_csv(report: str, sources: Sequence[CitedPage]) -> str:
    buf = io.StringIO()
    writer: csv.DictWriter[str] = csv.DictWriter(
        buf, fieldnames=REVIEW_COLUMNS, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(
        {k: _csv_safe(v) for k, v in row.items()} for row in review_rows(report, sources)
    )
    return buf.getvalue()


def _mermaid_text(text: Any) -> str:
    # Mermaid message text: no semicolons, hashes, or angle brackets; keep it one short line.
    cleaned = re.sub(r"[;#<>{}]", " ", str(text))
    cleaned = " ".join(cleaned.split())
    return cleaned[:80]


def trace_diagram_md(
    events: Sequence[Dict[str, Any]],
    terminal_status: str,
    sources: Sequence[CitedPage],
    review_state: str,
) -> str:
    lines = [
        "# Request lifecycle trace",
        "",
        "Generated from `events.sanitized.jsonl`. Times are local monotonic milliseconds since",
        "just before the request was sent. This is a **request lifecycle**, not a source-by-source",
        "execution trace: progress events say what phase the service reports, not which page it",
        "read. Only the `complete` event's `cited_pages` identifies sources.",
        "",
        "```mermaid",
        "sequenceDiagram",
        "    participant C as cited-research CLI",
        "    participant T as Tabstack /research",
        "    participant V as Citation reviewer",
        "    C->>T: one POST /research (SSE)",
    ]
    for ev in events:
        label = f"{ev.get('elapsed_ms', '?')} ms  {ev['event']}"
        if ev.get("iteration") is not None:
            label += f" (iteration {int(ev['iteration'])})"
        if ev["event"] == "complete":
            label += f": report {ev.get('report_chars', 0)} chars, {len(sources)} cited pages"
        elif ev["event"] == "error":
            label += f": {ev.get('error_name') or 'error'}"
        elif ev.get("message"):
            label += f": {ev['message']}"
        lines.append(f"    T-->>C: {_mermaid_text(label)}")
    if terminal_status == "complete":
        lines.append("    C->>V: report.md + sources.json + review-sheet.csv")
        lines.append("    V-->>C: claim-to-source judgments (manual, not generated)")
    else:
        lines.append(f"    Note over C: terminal state {terminal_status}. No report")
    lines += ["```", ""]

    lines += [
        "## What this trace shows and does not show",
        "",
        "| Observed (from the stream) | Not observable here |",
        "|---|---|",
        "| Event names, arrival order, local elapsed time | Which URL was fetched when |",
        "| Allowlisted counters (iteration, urls_new, ...) | Model prompts or internal reasoning |",
        "| Terminal state and report length | Retrieval passages behind each sentence |",
        "| Cited pages in returned order | Whether a cited page supports a sentence |",
        "",
        f"Terminal state: `{terminal_status}`. Review state: `{review_state}`.",
        "",
    ]
    if terminal_status == "complete":
        lines += [
            "## Cited pages (returned order)",
            "",
            "Inline markers `[n]` in the report are joined to position `n` below. The API does not",
            "state this join; it is an assumption the reviewer checks.",
            "",
            "| n | id | link | notes |",
            "|---|---|---|---|",
        ]
        for s in sources:
            if s.link_ok and s.url:
                link = f"[{_md_cell(s.title or s.url)}]({s.url})"
            else:
                link = f"not linked ({s.link_issue})"
            notes = []
            if s.duplicate_of_position:
                notes.append(f"likely same page as {s.duplicate_of_position}")
            if s.malformed_fields:
                notes.append("malformed: " + ", ".join(s.malformed_fields))
            if not s.claims:
                notes.append("claims: []")
            ident = _md_cell(s.id or "")
            lines.append(f"| {s.position} | `{ident}` | {link} | {'; '.join(notes)} |")
        if not sources:
            lines.append("| - | - | no cited_pages returned | review needed |")
        lines.append("")
    return "\n".join(lines)


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").replace("[", "\\[").replace("]", "\\]")
