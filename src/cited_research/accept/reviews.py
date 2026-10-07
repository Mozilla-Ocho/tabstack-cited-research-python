"""Blank review sheets for a run, and the rules for reading them back.

Four sheets, all UTF-8 with a BOM like the trace review sheet:

- reviews/coverage.csv   one row per required element per completed attempt
- reviews/claims.csv     one row per candidate claim per completed attempt (reviewer splits)
- reviews/decisions.csv  one row per completed attempt: critical failures, claim-enumeration
                         lock, accept/reject
- reviews/usage.csv      one row per attempt, every terminal state: verified usage or blank

Scores: 2 full/supported, 1 partial, 0 missing/unsupported, U unresolved. Blank is unreviewed.
Re-running never touches rows that already exist; it only appends rows for new attempts.
"""

from __future__ import annotations

import csv
import io
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..models import load_cited_pages
from ..review import _csv_safe, read_sheet, review_rows, write_review_sheet
from ..sanitize import write_text_atomic
from .dataset import file_sha256, load_dataset
from .run import read_attempts

SCORE_VALUES = ("2", "1", "0", "U")
CF_NONE = "none"
LOCK_VALUES = ("yes", "no")
DECISION_VALUES = ("accept", "reject")

COVERAGE_COLUMNS: Sequence[str] = (
    "run_id",
    "attempt_id",
    "question_id",
    "element_id",
    "criterion",
    "answer_excerpt",
    "score",
    "reason",
    "reviewer",
    "reviewed_at_utc",
)
CLAIM_COLUMNS: Sequence[str] = (
    "run_id",
    "attempt_id",
    "question_id",
    "claim_id",
    "answer_text",
    "citation_ids",
    "citation_present",
    "source_url",
    "passage",
    "source_date_or_version",
    "retrieved_at_utc",
    "support",
    "reason",
    "reviewer",
    "reviewed_at_utc",
    "auto_flags",
)
DECISION_COLUMNS: Sequence[str] = (
    "run_id",
    "attempt_id",
    "question_id",
    "critical_failure_conditions",
    "critical_failures_triggered",
    "claim_enumeration_locked",
    "decision",
    "reviewer",
    "reviewed_at_utc",
    "notes",
)
USAGE_COLUMNS: Sequence[str] = (
    "run_id",
    "attempt_id",
    "question_id",
    "terminal_status",
    "usage_value",
    "usage_unit",
    "usage_source",
    "usage_receipt_ref",
    "notes",
)
SHEETS: Dict[str, Sequence[str]] = {
    "coverage.csv": COVERAGE_COLUMNS,
    "claims.csv": CLAIM_COLUMNS,
    "decisions.csv": DECISION_COLUMNS,
    "usage.csv": USAGE_COLUMNS,
}


def sheet_csv(columns: Sequence[str], rows: Sequence[Dict[str, str]]) -> str:
    buf = io.StringIO()
    writer: csv.DictWriter[str] = csv.DictWriter(buf, fieldnames=list(columns), lineterminator="\n")
    writer.writeheader()
    writer.writerows({k: _csv_safe(str(row.get(k, ""))) for k in columns} for row in rows)
    return buf.getvalue()


def read_review_sheet(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    return [
        {k: (v or "").strip() for k, v in row.items() if k is not None}
        for row in read_sheet(path.read_text(encoding="utf-8-sig"))
    ]


def _dataset_for(run_dir: Path, manifest: Dict[str, Any], dataset: Optional[Path]) -> Path:
    if dataset is not None:
        return dataset
    recorded = manifest.get("dataset_path")
    if recorded:
        return (run_dir / recorded).resolve()
    raise FileNotFoundError("pass --dataset: the run manifest does not record its dataset path")


def generated_rows(
    run_dir: Path, dataset_path: Path
) -> Tuple[Dict[str, List[Dict[str, str]]], str]:
    """Blank rows for every attempt, per sheet. Returns (rows by sheet, dataset sha256)."""
    ds = load_dataset(dataset_path)
    questions = ds.by_id()
    out: Dict[str, List[Dict[str, str]]] = {name: [] for name in SHEETS}
    for att in read_attempts(run_dir):
        base = {
            "run_id": att["run_id"],
            "attempt_id": att["attempt_id"],
            "question_id": att["question_id"],
        }
        out["usage.csv"].append({**base, "terminal_status": att["terminal_status"]})
        if att["terminal_status"] != "complete":
            continue
        q = questions.get(att["question_id"])
        if q is None:
            raise ValueError(
                f"attempt {att['attempt_id']} is for {att['question_id']}, which is not in "
                f"{dataset_path}"
            )
        for el in q["required_elements"]:
            out["coverage.csv"].append(
                {**base, "element_id": el["id"], "criterion": el["criterion"]}
            )
        answer = run_dir / att["answer_dir"]
        try:
            report = (answer / "report.md").read_text(encoding="utf-8")
            pages = load_cited_pages(answer / "sources.json")
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"attempt {att['attempt_id']} is complete but its report or sources cannot be "
                f"read ({exc}); restore them before preparing reviews"
            ) from exc
        for row in review_rows(report, pages):
            out["claims.csv"].append(
                {
                    **base,
                    "claim_id": row["claim_id"],
                    "answer_text": row["answer_text"],
                    "citation_ids": row["citation_ids"],
                    "citation_present": "yes" if row["citation_ids"] else "no",
                    "source_url": row["source_url"],
                    "auto_flags": row["auto_flags"],
                }
            )
        conditions = " | ".join(
            f"CF{i}: {c}" for i, c in enumerate(q["critical_failure_conditions"], start=1)
        )
        out["decisions.csv"].append({**base, "critical_failure_conditions": conditions})
    return out, ds.sha256


def prepare_review(
    run_dir: Path, dataset: Optional[Path] = None, force: bool = False
) -> Dict[str, Tuple[int, int]]:
    """Write or extend the four sheets. Returns {sheet: (rows kept, rows added)}.

    Existing rows are kept verbatim, whatever the reviewer did to them (filled, split, merged,
    deleted). Rows are added only for attempts the sheet does not mention yet. `force` discards
    every existing row and writes blank sheets.
    """
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset_path = _dataset_for(run_dir, manifest, dataset)
    if file_sha256(dataset_path) != manifest["config"]["dataset_sha256"]:
        raise ValueError(
            f"{dataset_path} does not match the dataset hash recorded for this run; "
            "reviews must use the frozen set the run used"
        )
    rows, _ = generated_rows(run_dir, dataset_path)
    result: Dict[str, Tuple[int, int]] = {}
    for name, columns in SHEETS.items():
        path = run_dir / "reviews" / name
        if force or not path.exists():
            write_review_sheet(path, sheet_csv(columns, rows[name]))
            result[name] = (0, len(rows[name]))
            continue
        existing = read_review_sheet(path)
        seen = {r.get("attempt_id") for r in existing}
        added = [r for r in rows[name] if r["attempt_id"] not in seen]
        if added:
            _append_rows(path, columns, added)
        result[name] = (len(existing), len(added))
    return result


def _append_rows(path: Path, columns: Sequence[str], added: List[Dict[str, str]]) -> None:
    """Append rows to an existing sheet without re-serialising any existing byte: the existing
    header order, extra columns, delimiter, BOM, and line endings are kept."""
    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    header_line = text.split("\n", 1)[0].rstrip("\r")
    try:
        dialect: Any = csv.Sniffer().sniff(header_line, delimiters=",;\t")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    header = next(csv.reader([header_line], delimiter=delimiter))
    missing = [c for c in columns if c not in header]
    if missing:
        raise ValueError(
            f"{path} has no column(s) {', '.join(missing)}; cannot add rows without losing "
            "data (use --force to regenerate)"
        )
    newline = "\r\n" if "\r\n" in text else "\n"
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=delimiter, lineterminator=newline)
    for row in added:
        writer.writerow([_csv_safe(str(row.get(c, ""))) for c in header])
    if text and not text.endswith("\n"):
        text += newline
    write_text_atomic(path, ("\ufeff" if bom else "") + text + buf.getvalue())


# The seven release gates and their evidence, from the Prove article "The production-readiness
# checklist for current-answer features" (release-record table). Status is pass, fail, or
# unknown; an unknown gate stays open.
RELEASE_COLUMNS: Sequence[str] = (
    "gate",
    "evidence_to_attach",
    "decision_to_record",
    "status",
    "owner",
    "evidence_ref",
    "decided_at_utc",
    "notes",
)
RELEASE_STATUS_VALUES = ("pass", "fail", "unknown")
RELEASE_GATES: Sequence[Tuple[str, str, str]] = (
    (
        "Scope and acceptance",
        "Supported workflow and critical failure rules",
        "What the feature handles or refuses",
    ),
    (
        "Answer quality",
        "Coverage and claim-support reviews",
        "Which answers the application accepts",
    ),
    (
        "Time budget",
        "Client timings and deadline tests",
        "Whether the interaction meets its wait policy",
    ),
    (
        "Failure handling",
        "Attempt log, failure fixtures, and fallback tests",
        "How the application contains failures",
    ),
    (
        "Cost",
        "Matched usage records and budget controls",
        "Whether the workload fits the budget",
    ),
    (
        "Data and security",
        "Processing map and reviewed controls",
        "Whether the architecture meets requirements",
    ),
    (
        "Ownership and rollback",
        "Runbook and disable-path test",
        "Who acts when behavior changes",
    ),
)
