"""Reviewer worksheet and per-run trace diagram.

Nothing here judges a citation. The worksheet lays out candidate claims next to the source each
inline marker points at; a person fills in the passage and the judgment.
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .models import CitedPage, load_cited_pages
from .sanitize import write_text_atomic

# The first nine columns are the Week 3 Understand rubric's per-claim record, in its order:
# "What makes a citation useful", section "A rubric you can reuse". The CLI fills the first four
# (claim_id, answer_text, citation_ids, source_url); a reviewer fills the other five.
RUBRIC_COLUMNS: Sequence[str] = (
    "claim_id",
    "answer_text",
    "citation_ids",
    "source_url",
    "passage",
    "source_date_or_version",
    "retrieved_at_utc",
    "support",
    "reason",
)
# Generated context for the reviewer. Not part of the rubric; drop them when reporting scores.
HELPER_COLUMNS: Sequence[str] = ("cited_page_ids", "auto_flags", "api_claims")
REVIEW_COLUMNS: Sequence[str] = (*RUBRIC_COLUMNS, *HELPER_COLUMNS)
# Allowed values for `support`, filled in by a person. Empty means not yet reviewed.
# 2 supports at the stated scope, 1 partial or needs a qualifier, 0 unsupported or contradicted,
# U couldn't inspect. U is counted on its own, never as 0.
SUPPORT_VALUES = ("2", "1", "0", "U")

MARKER_GROUP = re.compile(r"\[(\d+(?:\s*,\s*\d+)*)\]")
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=\S)")
SOURCES_HEADING = re.compile(r"^\W*(sources|references|citations)\W*$", re.IGNORECASE)
LIST_PREFIX = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def candidate_claims(report: str) -> List[str]:
    """Split the report into sentence-sized candidates. Headings and any trailing
    Sources/References block are skipped. This is a starting point for a reviewer, who merges,
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
    """One row per candidate claim (a report sentence). The reviewer splits, merges, or deletes
    rows until each is one material claim, then fills passage through reason."""
    by_position = {s.position: s for s in sources}
    rows: List[Dict[str, str]] = []
    for i, sentence in enumerate(candidate_claims(report), start=1):
        markers = markers_in(sentence)
        urls: List[str] = []
        ids: List[str] = []
        api_claims: List[str] = []
        flags: List[str] = []
        if not markers:
            flags.append("no inline citation")
        for n in markers:
            src = by_position.get(n)
            if src is None:
                flags.append(f"[{n}] has no cited page at position {n}")
                continue
            ids.append(src.id or f"(position {n}, no id)")
            api_claims.extend(src.claims)
            if not src.link_ok:
                flags.append(f"[{n}] not linked ({src.link_issue})")
            elif src.url and src.url not in urls:
                urls.append(src.url)
            if src.duplicate_of_position and src.duplicate_of_position in markers:
                flags.append(f"[{n}] likely same page as [{src.duplicate_of_position}]")
            elif src.duplicate_of_position:
                flags.append(f"[{n}] likely same page as cited page {src.duplicate_of_position}")
        row = dict.fromkeys(REVIEW_COLUMNS, "")
        row.update(
            claim_id=f"C{i:02d}",
            answer_text=sentence,
            citation_ids="".join(f"[{n}]" for n in markers),
            source_url=" ; ".join(urls),
            cited_page_ids=" ; ".join(ids),
            auto_flags=" ; ".join(flags),
            api_claims=" | ".join(api_claims),
        )
        rows.append(row)
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
            "Inline markers `[n]` in the report are joined to position `n` below. The SDK",
            "documents `cited_pages` as ordered by first citation appearance; the public guide",
            "does not say. The reviewer confirms the join.",
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


REVIEWER_COLUMNS: Sequence[str] = (
    "passage",
    "source_date_or_version",
    "retrieved_at_utc",
    "support",
    "reason",
)


def has_review_entries(sheet_csv: str) -> bool:
    return any(
        (row.get(c) or "").strip()
        for row in csv.DictReader(io.StringIO(sheet_csv))
        for c in REVIEWER_COLUMNS
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Rebuild review-sheet.csv from a run directory's report.md and sources.json. No network."""
    p = argparse.ArgumentParser(
        prog="cited-research-review",
        description="Regenerate review-sheet.csv for an existing trace run. Makes no API call.",
    )
    p.add_argument("run_dir", type=Path)
    p.add_argument(
        "--force", action="store_true", help="Overwrite a sheet that already has review entries."
    )
    args = p.parse_args(argv)
    report = (args.run_dir / "report.md").read_text(encoding="utf-8")
    pages = load_cited_pages(args.run_dir / "sources.json")
    out = args.run_dir / "review-sheet.csv"
    if out.exists() and not args.force and has_review_entries(out.read_text(encoding="utf-8")):
        sys.stderr.write(f"{out} already has review entries; not overwriting (use --force).\n")
        return 1
    write_text_atomic(out, review_sheet_csv(report, pages))
    print(f"review sheet -> {out} ({len(candidate_claims(report))} candidate claims, unreviewed)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
