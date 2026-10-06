"""Load, validate, hash, and freeze an acceptance question set (JSON Lines).

`acceptance-evals/dataset-schema.json` defines the structure of one record. This module checks
that structure by hand (no jsonschema dependency; a test keeps the two in step) and adds what
JSON Schema cannot express: unique IDs, the 20-question / five-category design, review state,
evidence freshness before a live run, and network safety of reference URLs.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ..sanitize import utc_now_iso, write_text_atomic
from ..urls import check_public_url

CATEGORIES: Sequence[str] = (
    "source_discovery",
    "exact_contract",
    "synthesis",
    "freshness_scope",
    "limits_uncertainty",
)
QUESTIONS_PER_CATEGORY = 4
TOTAL_QUESTIONS = len(CATEGORIES) * QUESTIONS_PER_CATEGORY  # 20
EVIDENCE_STATUSES: Sequence[str] = ("pending", "reviewed")

RECORD_KEYS = frozenset(
    {
        "id",
        "category",
        "question",
        "required_elements",
        "reference_sources",
        "evidence_status",
        "critical_failure_conditions",
    }
)
ELEMENT_KEYS = frozenset({"id", "criterion"})
SOURCE_KEYS = frozenset({"url", "passage", "retrieved_at_utc", "date_or_version"})
QUESTION_ID = re.compile(r"Q[0-9]{2}")
ELEMENT_ID = re.compile(r"E[1-5]")
MIN_ELEMENTS, MAX_ELEMENTS = 2, 5
MIN_QUESTION_CHARS, MIN_CRITERION_CHARS, MIN_CONDITION_CHARS = 15, 10, 10
MIN_REVIEWED_PASSAGE_CHARS, MIN_REVIEWED_SCOPE_CHARS = 5, 5
# RFC 3339 date-time, UTC only: the field is named *_utc.
UTC_DATETIME = re.compile(
    r"(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(?:Z|\+00:00)"
)
DEFAULT_MAX_EVIDENCE_AGE_DAYS = 30
FREEZE_SCHEMA = "cited-research-accept/freeze/v1"
# The instruction sent after the frozen question. It names no expected fact, element, or
# failure condition; those stay in reviewer-side files.
DEFAULT_OUTPUT_INSTRUCTION = (
    "Answer from current public documentation and cite the sources you rely on. "
    "If the sources do not establish part of the answer, say which part."
)


@dataclass
class Issue:
    where: str
    message: str

    def __str__(self) -> str:
        return f"{self.where}: {self.message}"


@dataclass
class Dataset:
    path: Path
    records: List[Dict[str, Any]]
    lines: List[int]
    sha256: str
    issues: List[Issue] = field(default_factory=list)

    def by_id(self) -> Dict[str, Dict[str, Any]]:
        return {r["id"]: r for r in self.records if isinstance(r.get("id"), str)}

    def pending_ids(self) -> List[str]:
        return [str(r.get("id")) for r in self.records if r.get("evidence_status") != "reviewed"]


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_utc(value: Any) -> Optional[datetime]:
    """Parse an RFC 3339 UTC timestamp, or return None if it is not one."""
    if not isinstance(value, str) or not UTC_DATETIME.fullmatch(value):
        return None
    try:
        return datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def load_dataset(path: Path) -> Dataset:
    """Parse JSON Lines. Malformed lines become issues; nothing is guessed."""
    raw = path.read_bytes()
    records: List[Dict[str, Any]] = []
    lines: List[int] = []
    issues: List[Issue] = []
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return Dataset(path, [], [], hashlib.sha256(raw).hexdigest(), [Issue("file", str(exc))])
    for n, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as exc:
            issues.append(Issue(f"line {n}", f"malformed JSON: {exc.msg} (column {exc.colno})"))
            continue
        if not isinstance(value, dict):
            issues.append(Issue(f"line {n}", "a record must be a JSON object"))
            continue
        records.append(value)
        lines.append(n)
    if not records and not issues:
        issues.append(Issue("file", "no question records"))
    return Dataset(path, records, lines, hashlib.sha256(raw).hexdigest(), issues)


def _text(value: Any, minimum: int) -> bool:
    return isinstance(value, str) and len(value.strip()) >= minimum


def check_reference_url(url: Any) -> Optional[str]:
    """None if the URL is a public https URL; otherwise the reason it is refused."""
    if not isinstance(url, str):
        return "url must be a string"
    ok, issue = check_public_url(url)
    if not ok:
        return f"unsafe or unusable url ({issue})"
    if not url.startswith("https://"):
        return "url must use https"
    return None


def validate_record(rec: Dict[str, Any], where: str) -> List[Issue]:
    issues: List[Issue] = []

    def bad(message: str) -> None:
        issues.append(Issue(where, message))

    for key in sorted(RECORD_KEYS - set(rec)):
        bad(f"missing property '{key}'")
    for key in sorted(set(rec) - RECORD_KEYS):
        bad(f"unknown property '{key}'")

    qid = rec.get("id")
    if "id" in rec and not (isinstance(qid, str) and QUESTION_ID.fullmatch(qid)):
        bad("id must match Q00 to Q99")
    if "category" in rec and rec.get("category") not in CATEGORIES:
        bad(f"unexpected category {rec.get('category')!r}")
    if "question" in rec and not _text(rec.get("question"), MIN_QUESTION_CHARS):
        bad(f"question is blank or shorter than {MIN_QUESTION_CHARS} characters")

    elements = rec.get("required_elements")
    if "required_elements" in rec:
        if not isinstance(elements, list):
            bad("required_elements must be a list")
        else:
            if not MIN_ELEMENTS <= len(elements) <= MAX_ELEMENTS:
                bad(f"{len(elements)} required elements; need {MIN_ELEMENTS} to {MAX_ELEMENTS}")
            seen: List[str] = []
            for i, el in enumerate(elements, start=1):
                if not isinstance(el, dict):
                    bad(f"required_elements[{i}] must be an object")
                    continue
                for key in sorted(ELEMENT_KEYS - set(el)):
                    bad(f"required_elements[{i}] missing '{key}'")
                for key in sorted(set(el) - ELEMENT_KEYS):
                    bad(f"required_elements[{i}] unknown property '{key}'")
                eid = el.get("id")
                if not (isinstance(eid, str) and ELEMENT_ID.fullmatch(eid)):
                    bad(f"required_elements[{i}] id must be E1 to E5")
                elif eid in seen:
                    bad(f"duplicate element id {eid}")
                else:
                    seen.append(eid)
                if "criterion" in el and not _text(el.get("criterion"), MIN_CRITERION_CHARS):
                    bad(f"required_elements[{i}] criterion is blank or too short")

    status = rec.get("evidence_status")
    if "evidence_status" in rec and status not in EVIDENCE_STATUSES:
        bad(f"evidence_status must be one of {', '.join(EVIDENCE_STATUSES)}")

    sources = rec.get("reference_sources")
    if "reference_sources" in rec:
        if not isinstance(sources, list) or not sources:
            bad("reference_sources must be a non-empty list")
        else:
            for i, src in enumerate(sources, start=1):
                label = f"reference_sources[{i}]"
                if not isinstance(src, dict):
                    bad(f"{label} must be an object")
                    continue
                for key in sorted(SOURCE_KEYS - set(src)):
                    bad(f"{label} missing '{key}'")
                for key in sorted(set(src) - SOURCE_KEYS):
                    bad(f"{label} unknown property '{key}'")
                reason = check_reference_url(src.get("url"))
                if reason:
                    bad(f"{label} {reason}")
                for key in ("passage", "date_or_version"):
                    if key in src and not (src[key] is None or isinstance(src[key], str)):
                        bad(f"{label} {key} must be a string or null")
                retrieved = src.get("retrieved_at_utc")
                if retrieved is not None and parse_utc(retrieved) is None:
                    bad(f"{label} retrieved_at_utc {retrieved!r} is not a valid UTC date-time")
                if status == "reviewed":
                    if not _text(src.get("passage"), MIN_REVIEWED_PASSAGE_CHARS):
                        bad(f"{label} reviewed evidence needs the inspected passage")
                    if retrieved is None:
                        bad(f"{label} reviewed evidence needs retrieved_at_utc")
                    if not _text(src.get("date_or_version"), MIN_REVIEWED_SCOPE_CHARS):
                        bad(
                            f"{label} reviewed evidence needs date_or_version (explicit scope, "
                            "or say that the source states none)"
                        )

    conditions = rec.get("critical_failure_conditions")
    if "critical_failure_conditions" in rec:
        if not isinstance(conditions, list) or not conditions:
            bad("critical_failure_conditions must be a non-empty list")
        elif not all(_text(c, MIN_CONDITION_CHARS) for c in conditions):
            bad("critical_failure_conditions has a blank or too-short condition")
    return issues


def validate_dataset(
    ds: Dataset,
    allow_subset: bool = False,
    for_run: bool = False,
    now: Optional[datetime] = None,
    max_evidence_age_days: Optional[int] = DEFAULT_MAX_EVIDENCE_AGE_DAYS,
) -> List[Issue]:
    """All issues for the dataset. Empty means valid under the requested rules.

    `allow_subset` drops the 20-question / five-categories-of-four design check (for a frozen
    pilot subset). `for_run` adds the rules for a live run: no pending rows, and reviewed
    evidence retrieved no more than `max_evidence_age_days` ago and not in the future.
    """
    issues = list(ds.issues)
    ids: Dict[str, int] = {}
    for rec, line in zip(ds.records, ds.lines):
        qid = rec.get("id")
        where = f"line {line}" + (f" ({qid})" if isinstance(qid, str) else "")
        issues.extend(validate_record(rec, where))
        if isinstance(qid, str):
            if qid in ids:
                issues.append(
                    Issue(where, f"duplicate question id {qid} (first on line {ids[qid]})")
                )
            else:
                ids[qid] = line

    if not allow_subset:
        if len(ds.records) != TOTAL_QUESTIONS:
            issues.append(
                Issue(
                    "dataset", f"{len(ds.records)} questions; this design needs {TOTAL_QUESTIONS}"
                )
            )
        counts = category_counts(ds.records)
        for cat in CATEGORIES:
            if counts.get(cat, 0) != QUESTIONS_PER_CATEGORY:
                issues.append(
                    Issue(
                        "dataset",
                        f"category {cat} has {counts.get(cat, 0)} questions; "
                        f"this design needs {QUESTIONS_PER_CATEGORY}",
                    )
                )

    if for_run:
        now = now or datetime.now(timezone.utc)
        for rec, line in zip(ds.records, ds.lines):
            where = f"line {line} ({rec.get('id')})"
            if rec.get("evidence_status") != "reviewed":
                issues.append(Issue(where, "evidence_status is pending; not allowed in a live run"))
                continue
            for i, src in enumerate(rec.get("reference_sources") or [], start=1):
                when = parse_utc(src.get("retrieved_at_utc")) if isinstance(src, dict) else None
                if when is None:
                    continue
                if when > now + timedelta(minutes=5):
                    issues.append(Issue(where, f"reference_sources[{i}] retrieved in the future"))
                elif max_evidence_age_days is not None and now - when > timedelta(
                    days=max_evidence_age_days
                ):
                    issues.append(
                        Issue(
                            where,
                            f"reference_sources[{i}] evidence is older than "
                            f"{max_evidence_age_days} days; refresh it and re-freeze",
                        )
                    )
    return issues


def category_counts(records: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for rec in records:
        cat = rec.get("category")
        if isinstance(cat, str):
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def freeze_path_for(dataset_path: Path) -> Path:
    return dataset_path.with_name(dataset_path.name + ".freeze.json")


def freeze_dataset(
    ds: Dataset,
    version: str,
    allow_subset: bool,
    max_evidence_age_days: int,
    output_instruction: str = DEFAULT_OUTPUT_INSTRUCTION,
    out: Optional[Path] = None,
) -> Path:
    """Write the freeze record. Refuses to replace an existing one: a changed set is a new
    version in a new file."""
    out = out or freeze_path_for(ds.path)
    if out.exists():
        raise FileExistsError(f"{out} already exists; version a new dataset file instead")
    record = {
        "schema": FREEZE_SCHEMA,
        "dataset_file": ds.path.name,
        "dataset_version": version,
        "dataset_sha256": ds.sha256,
        "frozen_at_utc": utc_now_iso(),
        "design": "subset" if allow_subset else f"{TOTAL_QUESTIONS}-question",
        "question_ids": [r["id"] for r in ds.records],
        "category_counts": category_counts(ds.records),
        "max_evidence_age_days": max_evidence_age_days,
        "output_instruction": output_instruction,
    }
    write_text_atomic(out, json.dumps(record, indent=2, sort_keys=True) + "\n")
    return out


def load_freeze(dataset_path: Path, freeze: Optional[Path] = None) -> Dict[str, Any]:
    path = freeze or freeze_path_for(dataset_path)
    if not path.exists():
        raise FileNotFoundError(
            f"{path} not found: freeze the dataset first (cited-research-accept freeze)"
        )
    record = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(record, dict) or record.get("schema") != FREEZE_SCHEMA:
        raise ValueError(f"{path} is not a {FREEZE_SCHEMA} record")
    return record
