from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..sanitize import utc_now_iso

OUTPUT_INSTRUCTION = (
    "Answer the question directly. Support material factual claims with citations to public "
    "source URLs. Distinguish sourced facts from interpretation. State when evidence is "
    "incomplete or conflicting. End with a Sources section listing only sources used in the answer."
)


@dataclass
class Question:
    id: str
    category: str
    question: str
    required_elements: List[str]
    freshness_requirement: str
    notes: str = ""

    @staticmethod
    def load_all(path: Path) -> List[Question]:
        out: List[Question] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                out.append(Question(**json.loads(line)))
        return out


@dataclass
class UsageReceipt:
    source: str = "unavailable"
    search_cost: Optional[float] = None
    fetch_cost: Optional[float] = None
    model_cost: Optional[float] = None
    model_input_tokens: Optional[int] = None
    model_output_tokens: Optional[int] = None
    tabstack_credits: Optional[float] = None
    currency: str = "USD"
    notes: str = ""


@dataclass
class RunResult:
    protocol_version: str
    protocol_commit: Optional[str]
    question_id: str
    category: str
    system_id: str
    run_number: int
    pilot_only: bool
    system_config_hash: str
    started_at_utc: str
    completed_at_utc: Optional[str] = None
    duration_ms: Optional[int] = None
    first_event_ms: Optional[int] = None
    terminal_status: str = "unknown"
    retry_count: int = 0
    answer_path: Optional[str] = None
    sources_path: Optional[str] = None
    event_log_path: Optional[str] = None
    usage_receipt_path: Optional[str] = None
    usage: UsageReceipt = field(default_factory=UsageReceipt)
    blind_answer_id: str = field(default_factory=lambda: secrets.token_hex(4))
    failure_reason: Optional[str] = None
    notes: str = ""

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def config_hash(config: Dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def write_answer_bundle(
    run_dir: Path, answer_md: str, sources: List[Dict[str, Any]], result: RunResult
) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "answer.md").write_text(answer_md.rstrip() + "\n", encoding="utf-8")
    (run_dir / "sources.json").write_text(
        json.dumps(sources, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    result.answer_path = str(run_dir / "answer.md")
    result.sources_path = str(run_dir / "sources.json")
    (run_dir / "usage-receipt.json").write_text(
        json.dumps(asdict(result.usage), indent=2) + "\n", encoding="utf-8"
    )
    result.usage_receipt_path = str(run_dir / "usage-receipt.json")
    result.completed_at_utc = utc_now_iso()


def write_blind_copy(blind_dir: Path, result: RunResult) -> Path:
    """Answer text only, under a random ID. No system name, timing, cost, or event log."""
    blind_dir.mkdir(parents=True, exist_ok=True)
    assert result.answer_path is not None
    text = Path(result.answer_path).read_text(encoding="utf-8")
    text = text.replace("Tabstack", "[provider]") if "tabstack" in result.system_id else text
    out = blind_dir / f"{result.blind_answer_id}.md"
    out.write_text(text, encoding="utf-8")
    return out
