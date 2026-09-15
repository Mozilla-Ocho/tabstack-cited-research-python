"""`cited-research-eval`: run frozen questions through one or both systems.

Full evaluation is gated: without --allow-full only a single --question may run, and every
row is marked pilot_only=true.
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Sequence

from ..sanitize import utc_now_iso
from . import baseline, managed
from .common import Question, RunResult, write_blind_copy

CSV_COLUMNS = [
    "protocol_version", "protocol_commit", "question_id", "category", "system_id", "run_number",
    "pilot_only", "system_config_hash", "started_at_utc", "completed_at_utc", "duration_ms",
    "first_event_ms", "terminal_status", "retry_count", "answer_path", "sources_path",
    "event_log_path", "usage_receipt_path", "search_cost", "fetch_cost", "model_cost",
    "tabstack_credits", "tabstack_cost", "total_cost", "currency", "material_claims",
    "correct_claims", "partial_claims", "incorrect_claims", "unscorable_claims", "citation_pairs",
    "fully_supported_citation_pairs", "partly_supported_citation_pairs",
    "unsupported_citation_pairs", "citable_material_claims", "cited_material_claims",
    "citation_completeness_pct", "coverage_points_available", "coverage_points_earned",
    "source_quality_points", "source_quality_max", "freshness_points", "freshness_max",
    "uncertainty_score", "blind_answer_id", "evaluator_id", "scoring_status", "failure_reason",
    "notes",
]  # fmt: skip


def _git_head() -> Optional[str]:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception:
        return None


def _row(result: RunResult) -> dict:
    d = asdict(result)
    usage = d.pop("usage")
    row = {c: d.get(c, "") for c in CSV_COLUMNS}
    row["pilot_only"] = "true" if result.pilot_only else "false"
    row["tabstack_credits"] = "" if usage["tabstack_credits"] is None else usage["tabstack_credits"]
    row["model_cost"] = "" if usage["model_cost"] is None else usage["model_cost"]
    row["currency"] = usage["currency"]
    row["scoring_status"] = "unscored"
    return row


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cited-research-eval")
    p.add_argument("--questions", type=Path, default=Path("evals/questions.jsonl"))
    p.add_argument("--question", help="Question ID, e.g. Q01. Required unless --allow-full.")
    p.add_argument("--system", choices=("managed", "baseline", "both"), default="both")
    p.add_argument("--baseline-config", type=Path, default=Path("evals/baseline-config.json"))
    p.add_argument("--protocol-version", default="v1")
    p.add_argument("--out", type=Path, default=Path("evals/runs"))
    p.add_argument("--run-number", type=int, default=1)
    p.add_argument(
        "--allow-full",
        action="store_true",
        help="Run every frozen question. Requires the committed protocol.",
    )
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    questions = Question.load_all(args.questions)
    if args.question:
        questions = [q for q in questions if q.id == args.question]
        if not questions:
            sys.stderr.write(f"no question with id {args.question}\n")
            return 2
    elif not args.allow_full:
        sys.stderr.write(
            "refusing to run the full set without --allow-full; pass --question Q01 for the pilot\n"
        )
        return 2
    pilot_only = not args.allow_full
    protocol_commit = _git_head()
    stamp = utc_now_iso().replace(":", "").replace("-", "")[:15]
    batch_dir = args.out / f"{stamp}-{'pilot' if pilot_only else 'full'}"
    rows: List[dict] = []

    systems = []
    if args.system in ("managed", "both"):
        mcfg = managed.system_config()
        systems.append(("managed", mcfg, managed.hash_for(mcfg), managed.run))
    if args.system in ("baseline", "both"):
        if not args.baseline_config.exists():
            sys.stderr.write(f"baseline config not found: {args.baseline_config}\n")
            return 2
        bcfg = baseline.load_config(args.baseline_config)
        systems.append(("baseline", bcfg, baseline.hash_for(bcfg), baseline.run))

    for q in questions:
        for _name, cfg, chash, runner in systems:
            result = RunResult(
                protocol_version=args.protocol_version,
                protocol_commit=protocol_commit,
                question_id=q.id,
                category=q.category,
                system_id=cfg["system_id"],
                run_number=args.run_number,
                pilot_only=pilot_only,
                system_config_hash=chash,
                started_at_utc=utc_now_iso(),
            )
            run_dir = batch_dir / q.id / cfg["system_id"] / f"run-{args.run_number}"
            sys.stdout.write(f"{q.id} {cfg['system_id']} ... ")
            sys.stdout.flush()
            result = runner(q, run_dir, result, cfg)
            (run_dir / "result.json").parent.mkdir(parents=True, exist_ok=True)
            result.write(run_dir / "result.json")
            (run_dir / "system-config.json").write_text(
                json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if result.answer_path:
                write_blind_copy(batch_dir / "blind", result)
            sys.stdout.write(f"{result.terminal_status} {result.duration_ms} ms\n")
            rows.append(_row(result))

    batch_dir.mkdir(parents=True, exist_ok=True)
    with (batch_dir / "results.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    sys.stdout.write(
        f"results -> {batch_dir / 'results.csv'}  (pilot_only={str(pilot_only).lower()})\n"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
