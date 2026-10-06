"""Summarize a run's attempts and review sheets. Offline; makes no API call.

Rules: blank and `U` never enter a numerator or an inspected denominator; a rate with nothing
inspected is "unavailable", never 0; missing usage is "unavailable", never 0; pilot and
synthetic attempts are reported in their own scopes and excluded from live scored totals; a
completed request is not an accepted answer. The summary sets no minimum score: acceptance is
the reviewer's recorded decision, honored only when the review is complete and no frozen
critical failure condition was triggered.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..sanitize import utc_now_iso, write_text_atomic
from .dataset import file_sha256, load_dataset
from .reviews import CF_NONE, DECISION_VALUES, LOCK_VALUES, SCORE_VALUES, read_review_sheet
from .run import read_attempts

SUMMARY_SCHEMA = "cited-research-accept/summary/v1"
SCOPES: Sequence[str] = ("live_scored", "pilot", "synthetic")


class SheetError(ValueError):
    """A review sheet holds a value outside the allowed set."""


def provenance(att: Dict[str, Any]) -> str:
    if att.get("synthetic"):
        return "synthetic"
    if att.get("pilot_only"):
        return "pilot"
    return "live_scored"


def _rate(numerator: int, denominator: int, empty: str = "nothing inspected") -> Dict[str, Any]:
    if denominator == 0:
        return {"status": "unavailable", "reason": empty}
    return {
        "status": "available",
        "numerator": numerator,
        "denominator": denominator,
        "value": round(numerator / denominator, 4),
    }


def _score_counts(values: Sequence[str]) -> Dict[str, int]:
    return {
        "2": sum(v == "2" for v in values),
        "1": sum(v == "1" for v in values),
        "0": sum(v == "0" for v in values),
        "U": sum(v == "U" for v in values),
        "blank": sum(v == "" for v in values),
    }


def _parse_cf(value: str, n_conditions: int) -> Optional[List[str]]:
    """'' -> None (unreviewed), 'U' -> None, 'none' -> [], 'CF1;CF2' -> ids. Raises on junk."""
    if value in ("", "U"):
        return None
    if value.lower() == CF_NONE:
        return []
    ids = [p.strip().upper() for p in value.replace(",", ";").split(";") if p.strip()]
    valid = {f"CF{i}" for i in range(1, n_conditions + 1)}
    bad = [i for i in ids if i not in valid]
    if bad:
        raise SheetError(f"unknown critical failure id(s) {', '.join(bad)}")
    return ids


def load_review_state(run_dir: Path) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Per-attempt review state from the sheets, plus a list of sheet errors."""
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    dataset_path = (run_dir / manifest["dataset_path"]).resolve()
    recorded = manifest["config"]["dataset_sha256"]
    if file_sha256(dataset_path) != recorded:
        raise SheetError(
            f"{dataset_path} sha256 does not match the dataset_sha256 recorded for this run "
            f"({recorded[:12]}...); the frozen set was edited after the run"
        )
    ds = load_dataset(dataset_path)
    questions = ds.by_id()
    attempts = {a["attempt_id"]: a for a in read_attempts(run_dir)}
    errors: List[str] = []
    sheets = {
        name: read_review_sheet(run_dir / "reviews" / f"{name}.csv")
        for name in ("coverage", "claims", "decisions", "usage")
    }
    keys = {
        "coverage": ("attempt_id", "element_id"),
        "claims": ("attempt_id", "claim_id"),
        "decisions": ("attempt_id",),
        "usage": ("attempt_id",),
    }
    for name, rows in sheets.items():
        seen: Dict[Tuple[str, ...], int] = {}
        for n, row in enumerate(rows, start=2):
            if row.get("attempt_id") not in attempts:
                errors.append(
                    f"reviews/{name}.csv row {n}: unknown attempt_id {row.get('attempt_id')!r}"
                )
            key = tuple(row.get(k, "") for k in keys[name])
            if key in seen:
                errors.append(
                    f"reviews/{name}.csv row {n}: duplicate row for {' '.join(key)} "
                    f"(first on row {seen[key]})"
                )
            else:
                seen[key] = n

    state: Dict[str, Dict[str, Any]] = {}
    for aid, att in attempts.items():
        q = questions.get(att["question_id"], {})
        entry: Dict[str, Any] = {"coverage": {}, "claims": [], "decision": None, "usage": None}
        if att["terminal_status"] == "complete":
            entry["coverage"] = {el["id"]: "" for el in q.get("required_elements", [])}
            entry["coverage_rows_missing"] = []
        state[aid] = entry

    for n, row in enumerate(sheets["coverage"], start=2):
        st = state.get(row.get("attempt_id", ""))
        if st is None or "coverage_rows_missing" not in st:
            continue
        score = row.get("score", "")
        score = "U" if score == "u" else score
        if score not in ("", *SCORE_VALUES):
            errors.append(f"reviews/coverage.csv row {n}: score {score!r} not in 2, 1, 0, U, blank")
            continue
        eid = row.get("element_id", "")
        if eid not in st["coverage"]:
            errors.append(f"reviews/coverage.csv row {n}: element {eid!r} not in the question")
            continue
        st["coverage"][eid] = score
    for aid, st in state.items():
        if "coverage_rows_missing" in st:
            present = {
                r.get("element_id") for r in sheets["coverage"] if r.get("attempt_id") == aid
            }
            st["coverage_rows_missing"] = [e for e in st["coverage"] if e not in present]

    for n, row in enumerate(sheets["claims"], start=2):
        st = state.get(row.get("attempt_id", ""))
        if st is None:
            continue
        support = row.get("support", "")
        support = "U" if support == "u" else support
        if support not in ("", *SCORE_VALUES):
            errors.append(
                f"reviews/claims.csv row {n}: support {support!r} not in 2, 1, 0, U, blank"
            )
            continue
        st["claims"].append(support)

    for n, row in enumerate(sheets["decisions"], start=2):
        aid = row.get("attempt_id", "")
        st = state.get(aid)
        if st is None:
            continue
        q = questions.get(attempts[aid]["question_id"], {})
        conditions = q.get("critical_failure_conditions", [])
        lock = row.get("claim_enumeration_locked", "").lower()
        decision = row.get("decision", "").lower()
        if lock not in ("", *LOCK_VALUES):
            errors.append(f"reviews/decisions.csv row {n}: claim_enumeration_locked {lock!r}")
            continue
        if decision not in ("", *DECISION_VALUES):
            errors.append(f"reviews/decisions.csv row {n}: decision {decision!r}")
            continue
        raw_cf = row.get("critical_failures_triggered", "")
        try:
            cf = _parse_cf(raw_cf, len(conditions))
        except SheetError as exc:
            errors.append(f"reviews/decisions.csv row {n}: {exc}")
            continue
        st["decision"] = {
            "critical_failures": cf,
            "critical_failures_raw": raw_cf,
            "conditions": conditions,
            "locked": lock == "yes",
            "decision": decision,
        }

    for n, row in enumerate(sheets["usage"], start=2):
        st = state.get(row.get("attempt_id", ""))
        if st is None:
            continue
        value = row.get("usage_value", "")
        if value == "":
            continue
        try:
            number = float(value)
        except ValueError:
            errors.append(f"reviews/usage.csv row {n}: usage_value {value!r} is not a number")
            continue
        if (
            not math.isfinite(number)
            or number < 0
            or not row.get("usage_unit")
            or not row.get("usage_receipt_ref")
        ):
            errors.append(
                f"reviews/usage.csv row {n}: usage needs a finite non-negative value, a unit, "
                "and a receipt reference"
            )
            continue
        st["usage"] = {
            "value": number,
            "unit": row["usage_unit"],
            "source": row.get("usage_source", ""),
            "receipt_ref": row["usage_receipt_ref"],
        }
    return state, errors


def _response_outcome(att: Dict[str, Any], st: Dict[str, Any]) -> Dict[str, Any]:
    cov = list(st["coverage"].values())
    claims = st["claims"]
    dec = st["decision"] or {}
    cf = dec.get("critical_failures")
    locked = bool(dec.get("locked"))
    open_items: List[str] = []
    if any(v == "" for v in cov):
        open_items.append("coverage has blank scores")
    if any(v == "U" for v in cov):
        open_items.append("coverage has U")
    if any(v == "" for v in claims):
        open_items.append("claims have blank support")
    if any(v == "U" for v in claims):
        open_items.append("claims have U")
    if not locked:
        open_items.append("claim enumeration not locked")
    if cf is None:
        open_items.append("critical failure check not recorded")
    fully = not open_items and bool(cov)
    decision = dec.get("decision", "")
    accepted = fully and decision == "accept" and cf == []
    conflict = None
    if decision == "accept" and not accepted:
        conflict = "accept recorded but " + (
            "a critical failure was triggered" if cf else "; ".join(open_items)
        )
    return {
        "fully_reviewed": fully,
        "accepted": accepted,
        "rejected": decision == "reject",
        "open_items": open_items,
        "decision_conflict": conflict,
        "critical_failures": cf or [],
    }


def summarize(run_dir: Path) -> Dict[str, Any]:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    attempts = read_attempts(run_dir)
    state, errors = load_review_state(run_dir)
    if errors:
        raise SheetError("\n".join(errors))
    by_status: Dict[str, int] = {}
    for a in attempts:
        by_status[a["terminal_status"]] = by_status.get(a["terminal_status"], 0) + 1
    attempted = sorted({a["question_id"] for a in attempts})
    out: Dict[str, Any] = {
        "schema": SUMMARY_SCHEMA,
        "run_id": manifest["run_id"],
        "generated_at_utc": utc_now_iso(),
        "dataset_version": manifest["config"]["dataset_version"],
        "dataset_sha256": manifest["config"]["dataset_sha256"],
        "implementation_commit": manifest.get("implementation_commit"),
        "provenance": {s: sum(provenance(a) == s for a in attempts) for s in SCOPES},
        "questions": {
            "intended": len(manifest["intended_question_ids"]),
            "attempted": len(attempted),
            "attempted_ids": attempted,
        },
        "attempts": {"total": len(attempts), "by_terminal_status": by_status},
        "scopes": {},
    }
    for scope in SCOPES:
        scoped = [a for a in attempts if provenance(a) == scope]
        if scoped or scope == "live_scored":
            out["scopes"][scope] = _scope_block(scoped, state)
    return out


def _scope_block(
    attempts: List[Dict[str, Any]], state: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    by_status: Dict[str, int] = {}
    for a in attempts:
        by_status[a["terminal_status"]] = by_status.get(a["terminal_status"], 0) + 1
    completed = [a for a in attempts if a["terminal_status"] == "complete"]
    outcomes = {a["attempt_id"]: _response_outcome(a, state[a["attempt_id"]]) for a in completed}
    cov_values = [v for a in completed for v in state[a["attempt_id"]]["coverage"].values()]
    claim_values = [v for a in completed for v in state[a["attempt_id"]]["claims"]]
    cov = _score_counts(cov_values)
    cl = _score_counts(claim_values)
    cov_inspected = cov["2"] + cov["1"] + cov["0"]
    cl_inspected = cl["2"] + cl["1"] + cl["0"]
    accepted = sum(o["accepted"] for o in outcomes.values())
    rejected = sum(o["rejected"] and not o["accepted"] for o in outcomes.values())

    critical: List[Dict[str, Any]] = []
    critical_unchecked: List[str] = []
    for a in completed:
        dec = state[a["attempt_id"]]["decision"] or {}
        cf = dec.get("critical_failures")
        if cf is None:
            critical_unchecked.append(a["attempt_id"])
            continue
        for cid in cf:
            critical.append(
                {
                    "question_id": a["question_id"],
                    "attempt_id": a["attempt_id"],
                    "id": cid,
                    "condition": dec["conditions"][int(cid[2:]) - 1],
                }
            )

    usage_known = [
        {"attempt_id": a["attempt_id"], **state[a["attempt_id"]]["usage"]}
        for a in attempts
        if state[a["attempt_id"]]["usage"] is not None
    ]
    usage_missing = [a["attempt_id"] for a in attempts if state[a["attempt_id"]]["usage"] is None]
    units = sorted({u["unit"] for u in usage_known})

    reasons: List[str] = []
    if not attempts:
        reasons.append("no attempts in this scope")
    if usage_missing:
        reasons.append(f"usage missing for {len(usage_missing)} of {len(attempts)} attempts")
    if len(units) > 1:
        reasons.append(f"usage recorded in mixed units ({', '.join(units)})")
    undecided = [aid for aid, o in outcomes.items() if not (o["accepted"] or (o["rejected"]))]
    if undecided:
        reasons.append(f"{len(undecided)} completed responses have no final accept/reject")
    not_full = [aid for aid, o in outcomes.items() if not o["fully_reviewed"]]
    if not_full:
        reasons.append(f"{len(not_full)} completed responses are not fully reviewed")
    if attempts and accepted == 0:
        reasons.append("no accepted answers")
    if reasons:
        cost: Dict[str, Any] = {"status": "unavailable", "reasons": reasons}
    else:
        total = sum(u["value"] for u in usage_known)
        cost = {
            "status": "available",
            "total_usage": total,
            "unit": units[0],
            "accepted": accepted,
            "value": round(total / accepted, 4),
            "covers_attempts": len(attempts),
        }

    return {
        "attempts": len(attempts),
        "by_terminal_status": by_status,
        "failure_classes": sorted(
            {a["failure_class"] for a in attempts if a["terminal_status"] != "complete"}
        ),
        "responses": {
            "completed": len(completed),
            "fully_reviewed": sum(o["fully_reviewed"] for o in outcomes.values()),
            "accepted": accepted,
            "rejected": rejected,
            "unreviewed_or_undecided": len(completed) - accepted - rejected,
            "decision_conflicts": {
                aid: o["decision_conflict"] for aid, o in outcomes.items() if o["decision_conflict"]
            },
            "open_review_items": {
                aid: o["open_items"] for aid, o in outcomes.items() if o["open_items"]
            },
        },
        "accepted_over_attempts": _rate(accepted, len(attempts), "no attempts"),
        "coverage": {
            "full": cov["2"],
            "partial": cov["1"],
            "missing": cov["0"],
            "U": cov["U"],
            "blank": cov["blank"],
            "inspected": cov_inspected,
            "full_rate": _rate(cov["2"], cov_inspected),
        },
        "claims": {
            "supported": cl["2"],
            "partial": cl["1"],
            "unsupported": cl["0"],
            "U": cl["U"],
            "blank": cl["blank"],
            "inspected": cl_inspected,
            "supported_rate": _rate(cl["2"], cl_inspected),
            "enumeration_locked": sum(
                bool((state[a["attempt_id"]]["decision"] or {}).get("locked")) for a in completed
            ),
            "enumeration_unlocked": sum(
                not (state[a["attempt_id"]]["decision"] or {}).get("locked") for a in completed
            ),
        },
        "critical_failures": critical,
        "critical_failure_check_missing": critical_unchecked,
        "timing": [
            {
                "attempt_id": a["attempt_id"],
                "terminal_status": a["terminal_status"],
                "first_event_ms": a.get("first_event_ms"),
                "terminal_elapsed_ms": a.get("terminal_elapsed_ms"),
            }
            for a in attempts
        ],
        "usage": {
            "known": usage_known,
            "missing": usage_missing,
            "missing_count": len(usage_missing),
        },
        "cost_per_accepted_answer": cost,
    }


def _ms(value: Any) -> str:
    return "not measured" if value is None else f"{value} ms"


def _rate_text(rate: Dict[str, Any]) -> str:
    if rate["status"] != "available":
        return f"unavailable ({rate['reason']})"
    return f"{rate['numerator']}/{rate['denominator']}"


def render_text(summary: Dict[str, Any]) -> str:
    p = summary["provenance"]
    lines = [
        f"run {summary['run_id']}  dataset {summary['dataset_version']} "
        f"(sha256 {summary['dataset_sha256'][:12]}...)",
        f"provenance: {p['live_scored']} live scored, {p['pilot']} pilot, "
        f"{p['synthetic']} synthetic attempts (pilot and synthetic are excluded from live "
        "scored totals)",
        f"questions: {summary['questions']['intended']} intended, "
        f"{summary['questions']['attempted']} attempted",
        "attempts: "
        + f"{summary['attempts']['total']} total; "
        + ", ".join(
            f"{k} {v}" for k, v in sorted(summary["attempts"]["by_terminal_status"].items())
        ),
    ]
    for scope, b in summary["scopes"].items():
        r, c, cl = b["responses"], b["coverage"], b["claims"]
        lines += [
            "",
            f"[{scope}] {b['attempts']} attempts"
            + (
                ": " + ", ".join(f"{k} {v}" for k, v in sorted(b["by_terminal_status"].items()))
                if b["attempts"]
                else ""
            ),
            f"  responses: {r['completed']} completed, {r['fully_reviewed']} fully reviewed, "
            f"{r['accepted']} accepted, {r['rejected']} rejected, "
            f"{r['unreviewed_or_undecided']} unreviewed or undecided",
            f"  accepted / attempts: {_rate_text(b['accepted_over_attempts'])}",
            f"  coverage elements: {c['full']} full, {c['partial']} partial, {c['missing']} "
            f"missing, {c['U']} U, {c['blank']} blank; full over inspected: "
            f"{_rate_text(c['full_rate'])}",
            f"  claims: {cl['supported']} supported, {cl['partial']} partial, "
            f"{cl['unsupported']} unsupported, {cl['U']} U, {cl['blank']} blank; supported over "
            f"inspected: {_rate_text(cl['supported_rate'])}; enumeration locked on "
            f"{cl['enumeration_locked']} of {r['completed']} responses",
        ]
        for cf in b["critical_failures"]:
            lines.append(
                f"  critical failure: {cf['question_id']} {cf['attempt_id']} {cf['id']}: "
                f"{cf['condition']}"
            )
        if b["critical_failure_check_missing"]:
            lines.append(
                "  critical failure check not recorded: "
                + ", ".join(b["critical_failure_check_missing"])
            )
        for aid, why in b["responses"]["decision_conflicts"].items():
            lines.append(f"  decision conflict: {aid}: {why}")
        for t in b["timing"]:
            lines.append(
                f"  timing {t['attempt_id']} {t['terminal_status']}: first event "
                f"{_ms(t['first_event_ms'])}, terminal {_ms(t['terminal_elapsed_ms'])}"
            )
        u = b["usage"]
        lines.append(f"  usage: {len(u['known'])} known, {u['missing_count']} missing")
        cost = b["cost_per_accepted_answer"]
        if cost["status"] == "available":
            lines.append(
                f"  cost per accepted answer: {cost['value']} {cost['unit']} "
                f"({cost['total_usage']} over {cost['accepted']} accepted, "
                f"{cost['covers_attempts']} attempts)"
            )
        else:
            lines.append(
                "  cost per accepted answer: unavailable (" + "; ".join(cost["reasons"]) + ")"
            )
    return "\n".join(lines) + "\n"


def write_summary(run_dir: Path, summary: Dict[str, Any]) -> Path:
    path = run_dir / "summary.json"
    write_text_atomic(path, json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return path
