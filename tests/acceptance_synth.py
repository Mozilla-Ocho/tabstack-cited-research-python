"""Build a SYNTHETIC acceptance run from offline fixtures. No network, no API key.

Used by tests, and to regenerate the committed example:

    uv run python tests/acceptance_synth.py acceptance-evals/examples

Every attempt is marked synthetic=true; the questions are about a fictional "Example Widgets
API" on docs.example.com, and the reports come from tests/fixtures. Review values are filled in
by this script to exercise each score state; they are not a review of anything real.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import httpx
import tabstack
from conftest import FakeClient, load_events

from cited_research.accept.dataset import freeze_dataset, load_dataset
from cited_research.accept.reviews import SHEETS, prepare_review, read_review_sheet, sheet_csv
from cited_research.accept.run import run_one
from cited_research.review import write_review_sheet
from cited_research.sanitize import utc_now_iso

SYNTH_URL = "https://docs.example.com/widgets"


class ZeroRetryFake(FakeClient):
    """Matches the real default client: SDK retries off."""

    max_retries = 0


def synthetic_questions(retrieved_at: str) -> List[Dict[str, Any]]:
    def q(i: int, cat: str, question: str, cf: str) -> Dict[str, Any]:
        return {
            "id": f"Q{i:02d}",
            "category": cat,
            "question": question,
            "required_elements": [
                {"id": "E1", "criterion": "Name the documented field (synthetic)."},
                {"id": "E2", "criterion": "State the documented limit (synthetic)."},
            ],
            "reference_sources": [
                {
                    "url": f"{SYNTH_URL}/{i}",
                    "passage": "Synthetic passage for an offline fixture.",
                    "retrieved_at_utc": retrieved_at,
                    "date_or_version": "Synthetic fixture; no real source.",
                }
            ],
            "evidence_status": "reviewed",
            "critical_failure_conditions": [cf],
        }

    return [
        q(
            1,
            "exact_contract",
            "Which fields does the Example Widgets API list?",
            "Invents a field.",
        ),
        q(
            2,
            "limits_uncertainty",
            "What is the Example Widgets API rate limit?",
            "Invents a limit.",
        ),
        q(3, "synthesis", "How do Example Widgets list and get calls differ?", "Merges the calls."),
    ]


def write_synthetic_dataset(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "synthetic-dataset.jsonl"
    rows = synthetic_questions(utc_now_iso())
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    freeze_dataset(load_dataset(path), "synthetic-v1", allow_subset=True, max_evidence_age_days=30)
    return path


def _status_error(code: int) -> tabstack.APIStatusError:
    req = httpx.Request("POST", "https://api.tabstack.ai/v1/research")
    return tabstack.APIStatusError(
        "Unauthorized", response=httpx.Response(code, request=req), body=None
    )


def fixture_client(name: str) -> Callable[[], Any]:
    client = ZeroRetryFake(load_events(name))
    return lambda: client


def raising_client(exc: BaseException) -> Callable[[], Any]:
    def opener() -> Any:
        raise exc

    client = ZeroRetryFake(opener)
    return lambda: client


def attempt(
    dataset: Path,
    run: Path,
    qid: str,
    factory: Callable[[], Any],
    another: bool = False,
    synthetic: bool = True,
    pilot: bool = False,
    **kw: Any,
) -> int:
    return run_one(
        dataset,
        qid,
        run,
        another_attempt=another,
        quiet=True,
        stdout=io.StringIO(),
        client_factory=factory,
        synthetic=synthetic,
        pilot_only=pilot,
        post_terminal_grace=0.2,
        **kw,
    )


def fill(run: Path, sheet: str, match: Dict[str, str], values: Dict[str, str]) -> None:
    """Set reviewer cells on every row matching `match` (synthetic review only)."""
    path = run / "reviews" / sheet
    rows = read_review_sheet(path)
    for row in rows:
        if all(row.get(k) == v for k, v in match.items()):
            row.update(values)
    write_review_sheet(path, sheet_csv(SHEETS[sheet], rows))


def build_synthetic_run(directory: Path, run_name: str = "synthetic-run") -> Path:
    dataset = write_synthetic_dataset(directory)
    run = directory / run_name
    who = {"reviewer": "synthetic fixture", "reviewed_at_utc": "1970-01-01T00:00:00Z"}

    attempt(dataset, run, "Q01", fixture_client("complete-ordered-sources.jsonl"))
    attempt(dataset, run, "Q02", fixture_client("complete-no-cited-pages.jsonl"))
    attempt(dataset, run, "Q03", fixture_client("error-events.jsonl"))
    attempt(dataset, run, "Q03", raising_client(_status_error(401)), another=True)
    attempt(dataset, run, "Q01", fixture_client("complete-events.jsonl"), another=True)
    attempt(dataset, run, "Q02", fixture_client("truncated-events.jsonl"), another=True)
    prepare_review(run)

    # Q01-a1: fully reviewed and accepted.
    fill(run, "coverage.csv", {"attempt_id": "Q01-a1", "element_id": "E1"}, {"score": "2", **who})
    fill(run, "coverage.csv", {"attempt_id": "Q01-a1", "element_id": "E2"}, {"score": "1", **who})
    for cid, support in (("C01", "2"), ("C02", "2"), ("C03", "0"), ("C04", "1")):
        fill(run, "claims.csv", {"attempt_id": "Q01-a1", "claim_id": cid}, {"support": support})
    fill(
        run,
        "decisions.csv",
        {"attempt_id": "Q01-a1"},
        {
            "critical_failures_triggered": "none",
            "claim_enumeration_locked": "yes",
            "decision": "accept",
            **who,
        },
    )
    # Q02-a1: a critical failure; rejected.
    fill(run, "coverage.csv", {"attempt_id": "Q02-a1", "element_id": "E1"}, {"score": "2", **who})
    fill(run, "coverage.csv", {"attempt_id": "Q02-a1", "element_id": "E2"}, {"score": "0", **who})
    fill(run, "claims.csv", {"attempt_id": "Q02-a1"}, {"support": "0"})
    fill(
        run,
        "decisions.csv",
        {"attempt_id": "Q02-a1"},
        {
            "critical_failures_triggered": "CF1",
            "claim_enumeration_locked": "yes",
            "decision": "reject",
            **who,
        },
    )
    # Q01-a2: review left incomplete (one U, one blank, enumeration not locked).
    fill(run, "coverage.csv", {"attempt_id": "Q01-a2", "element_id": "E1"}, {"score": "U", **who})
    # Usage: known for two attempts, missing for the rest.
    fill(
        run,
        "usage.csv",
        {"attempt_id": "Q01-a1"},
        {"usage_value": "3", "usage_unit": "credits", "usage_receipt_ref": "synthetic-receipt-1"},
    )
    fill(
        run,
        "usage.csv",
        {"attempt_id": "Q03-a2"},
        {"usage_value": "0", "usage_unit": "credits", "usage_receipt_ref": "synthetic-receipt-2"},
    )
    return run


def main(argv: Optional[List[str]] = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    directory = Path(args[0])
    run = build_synthetic_run(directory)
    print(f"synthetic run -> {run}")
    return 0


def parallel_worker(dataset: str, run: str, question: str, count: int, start: Any) -> None:
    """Record `count` attempts for `question` in `run`; used by the two-process lock test.

    Each ledger read sleeps briefly before returning, widening the read-modify-write window so a
    missing lock loses records reliably instead of by chance."""
    import time

    import cited_research.accept.run as run_mod

    real_read = run_mod.read_attempts

    def slow_read(run_dir: Path) -> List[Dict[str, Any]]:
        rows = real_read(run_dir)
        time.sleep(0.05)
        return rows

    run_mod.read_attempts = slow_read
    start.wait(30)
    for _ in range(count):
        attempt(
            Path(dataset),
            Path(run),
            question,
            fixture_client("complete-events.jsonl"),
            another=True,
        )


if __name__ == "__main__":
    raise SystemExit(main())
