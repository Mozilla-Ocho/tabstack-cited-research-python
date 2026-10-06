"""`cited-research-accept`: workflow acceptance kit.

    validate        check a question set offline (structure, uniqueness, counts, review state)
    freeze          hash and version a reviewed set so it can run live
    run-one         the only command that calls the API: one frozen question, one request
    prepare-review  write blank coverage, claim, decision, and usage sheets for a run
    summarize       count attempts and reviews; missing scores and usage stay unavailable

Only run-one makes a network request. It reads TABSTACK_API_KEY from the environment; no
command accepts a key as an argument.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Optional, Sequence

from .dataset import (
    DEFAULT_MAX_EVIDENCE_AGE_DAYS,
    TOTAL_QUESTIONS,
    category_counts,
    freeze_dataset,
    load_dataset,
    validate_dataset,
)
from .reviews import prepare_review
from .run import RunRefused, run_one
from .summary import SheetError, render_text, summarize, write_summary

EXIT_REFUSED = 1
EXIT_NO_KEY = 5


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cited-research-accept",
        description="Acceptance evaluation for one current-answer workflow. Only run-one "
        "calls the API.",
    )
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate", help="Validate a JSONL question set offline.")
    v.add_argument("--dataset", required=True, type=Path)
    v.add_argument(
        "--allow-subset",
        action="store_true",
        help=f"Skip the {TOTAL_QUESTIONS}-question, five-categories-of-four design check.",
    )
    v.add_argument(
        "--for-run",
        action="store_true",
        help="Also apply live-run rules: no pending rows, evidence not stale.",
    )
    v.add_argument(
        "--max-evidence-age-days", type=int, default=DEFAULT_MAX_EVIDENCE_AGE_DAYS, metavar="DAYS"
    )

    f = sub.add_parser("freeze", help="Validate with live-run rules, then hash and version.")
    f.add_argument("--dataset", required=True, type=Path)
    f.add_argument("--version", required=True, help="Dataset version label, e.g. pilot-q05-v1.")
    f.add_argument("--allow-subset", action="store_true")
    f.add_argument(
        "--max-evidence-age-days", type=int, default=DEFAULT_MAX_EVIDENCE_AGE_DAYS, metavar="DAYS"
    )

    r = sub.add_parser("run-one", help="Send one frozen question to Tabstack /research.")
    r.add_argument("--dataset", required=True, type=Path)
    r.add_argument("--question", required=True, help="Question ID, e.g. Q05.")
    r.add_argument("--run", required=True, type=Path, help="Run directory (created if absent).")
    r.add_argument("--mode", choices=("fast", "balanced"), default="fast")
    r.add_argument("--nocache", action="store_true")
    r.add_argument("--fetch-timeout", type=int, default=None, metavar="SECONDS")
    r.add_argument(
        "--silence-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="Stop waiting after this long without an event (exit 7).",
    )
    r.add_argument(
        "--deadline",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="Stop waiting after this long in total, even if events keep arriving (exit 12).",
    )
    r.add_argument("--pilot", action="store_true", help="Mark the run pilot_only=true.")
    r.add_argument(
        "--another-attempt",
        action="store_true",
        help="Record a new attempt for a question that already has one in this run.",
    )
    r.add_argument("--quiet", action="store_true")

    pr = sub.add_parser("prepare-review", help="Write or extend the blank review sheets.")
    pr.add_argument("--run", required=True, type=Path)
    pr.add_argument(
        "--force", action="store_true", help="Discard existing review rows and write blank sheets."
    )

    s = sub.add_parser("summarize", help="Summarize attempts and review sheets.")
    s.add_argument("--run", required=True, type=Path)
    s.add_argument("--json", action="store_true", help="Print summary.json instead of text.")
    return p


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except (RunRefused, FileNotFoundError, FileExistsError, ValueError) as exc:
        sys.stderr.write(f"refused: {exc}\n")
        return EXIT_REFUSED


def _validate(args: argparse.Namespace) -> int:
    ds = load_dataset(args.dataset)
    issues = validate_dataset(
        ds,
        allow_subset=args.allow_subset,
        for_run=args.for_run,
        max_evidence_age_days=args.max_evidence_age_days,
    )
    counts = category_counts(ds.records)
    pending = ds.pending_ids()
    print(
        f"{args.dataset}: {len(ds.records)} questions; "
        + ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    )
    print(f"evidence: {len(ds.records) - len(pending)} reviewed, {len(pending)} pending")
    print(f"sha256: {ds.sha256}")
    sys.stdout.flush()
    for issue in issues:
        print(f"  {issue}", file=sys.stderr)
    if issues:
        print(f"INVALID: {len(issues)} issue(s)", file=sys.stderr)
        return EXIT_REFUSED
    if pending and not args.for_run:
        print("valid structure; not runnable live until every row is reviewed and frozen")
    else:
        print("valid" + (" for a live run" if args.for_run else ""))
    return 0


def _freeze(args: argparse.Namespace) -> int:
    ds = load_dataset(args.dataset)
    issues = validate_dataset(
        ds,
        allow_subset=args.allow_subset,
        for_run=True,
        max_evidence_age_days=args.max_evidence_age_days,
    )
    if issues:
        for issue in issues:
            print(f"  {issue}", file=sys.stderr)
        print(f"not frozen: {len(issues)} issue(s)", file=sys.stderr)
        return EXIT_REFUSED
    out = freeze_dataset(ds, args.version, args.allow_subset, args.max_evidence_age_days)
    print(f"frozen {args.dataset} as {args.version} (sha256 {ds.sha256}) -> {out}")
    return 0


def _run_one(args: argparse.Namespace) -> int:
    if not os.environ.get("TABSTACK_API_KEY"):
        sys.stderr.write(
            "TABSTACK_API_KEY is not set. Export it in your shell; "
            "the CLI never accepts it as a flag.\n"
        )
        return EXIT_NO_KEY
    return run_one(
        dataset_path=args.dataset,
        question_id=args.question,
        run_dir=args.run,
        mode=args.mode,
        nocache=args.nocache,
        fetch_timeout=args.fetch_timeout,
        silence_timeout=args.silence_timeout,
        deadline=args.deadline,
        pilot_only=args.pilot,
        another_attempt=args.another_attempt,
        quiet=args.quiet,
    )


def _prepare_review(args: argparse.Namespace) -> int:
    result = prepare_review(args.run, force=args.force)
    for name, (kept, added) in result.items():
        note = "kept as-is" if kept else ""
        print(
            f"reviews/{name}: {added} blank row(s) added"
            + (f", {kept} existing row(s) {note}" if kept else "")
        )
    return 0


def _summarize(args: argparse.Namespace) -> int:
    try:
        summary = summarize(args.run)
    except SheetError as exc:
        sys.stderr.write(f"review sheets have invalid values:\n{exc}\n")
        return EXIT_REFUSED
    path = write_summary(args.run, summary)
    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        sys.stdout.write(render_text(summary))
        print(f"summary -> {path}")
    return 0


COMMANDS = {
    "validate": _validate,
    "freeze": _freeze,
    "run-one": _run_one,
    "prepare-review": _prepare_review,
    "summarize": _summarize,
}


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
