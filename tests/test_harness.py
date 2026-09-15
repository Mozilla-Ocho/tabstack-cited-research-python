from __future__ import annotations

import csv
from pathlib import Path

import pytest

from cited_research.harness.cli import CSV_COLUMNS, main
from cited_research.harness.common import Question

ROOT = Path(__file__).resolve().parents[1]


def test_csv_columns_match_frozen_template() -> None:
    with (ROOT / "evals" / "results-template.csv").open(encoding="utf-8") as fh:
        header = next(csv.reader(fh))
    assert header == CSV_COLUMNS


def test_questions_file_is_frozen_and_well_formed() -> None:
    qs = Question.load_all(ROOT / "evals" / "questions.jsonl")
    assert [q.id for q in qs] == [f"Q{i:02d}" for i in range(1, 13)]
    cats = [q.category for q in qs]
    assert cats.count("simple_source_discovery") == 3
    assert cats.count("multi_source_synthesis") == 5
    assert cats.count("freshness_sensitive") == 2
    assert cats.count("conflict_or_incomplete") == 2
    assert all(2 <= len(q.required_elements) <= 5 for q in qs)


def test_full_run_refused_without_flag(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--questions", str(ROOT / "evals" / "questions.jsonl")])
    assert code == 2
    assert "--allow-full" in capsys.readouterr().err


def test_unknown_question_id_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(["--questions", str(ROOT / "evals" / "questions.jsonl"), "--question", "Q99"])
    assert code == 2
