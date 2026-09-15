"""System B adapter: one Tabstack /research call per question."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, Optional

import tabstack
from tabstack import Tabstack

from ..models import ResearchTaskError, RunManifest, sha256_text
from ..sanitize import utc_now_iso
from ..tabstack_runner import consume_stream, persist_complete
from .common import OUTPUT_INSTRUCTION, Question, RunResult, config_hash, write_answer_bundle

SYSTEM_ID = "tabstack_research_fast"


def system_config(
    mode: str = "fast", nocache: bool = True, fetch_timeout: Optional[int] = None
) -> Dict[str, Any]:
    return {
        "system_id": SYSTEM_ID,
        "provider": "Tabstack",
        "endpoint": "/research",
        "mode": mode,
        "nocache": nocache,
        "fetch_timeout": fetch_timeout,
        "application_retries": 0,
        "sdk": f"tabstack=={tabstack.__version__}",
        "sdk_transport_retries_default": 2,
        "output_instruction": OUTPUT_INSTRUCTION,
    }


def run(question: Question, run_dir: Path, result: RunResult, cfg: Dict[str, Any]) -> RunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    query = f"{question.question}\n\n{OUTPUT_INSTRUCTION}"
    manifest = RunManifest(
        query_sha256=sha256_text(query),
        mode=cfg["mode"],
        nocache=cfg["nocache"],
        fetch_timeout_seconds=cfg["fetch_timeout"],
        started_at_utc=utc_now_iso(),
        tabstack_version=tabstack.__version__,
    )
    started = perf_counter()
    try:
        with Tabstack() as client:
            kwargs: Dict[str, Any] = {
                "query": query,
                "mode": cfg["mode"],
                "nocache": cfg["nocache"],
            }
            if cfg["fetch_timeout"] is not None:
                kwargs["fetch_timeout"] = cfg["fetch_timeout"]
            stream = client.agent.research(**kwargs)
            final = consume_stream(stream, run_dir, manifest, started, lambda _: None, quiet=True)
            sources = persist_complete(final, run_dir, manifest)
        result.terminal_status = "complete"
        write_answer_bundle(run_dir, final.data.report, [asdict(s) for s in sources], result)
    except ResearchTaskError as exc:
        result.terminal_status = "task_failure"
        result.failure_reason = str(exc)
    except tabstack.APIStatusError as exc:
        result.terminal_status = "http_failure"
        result.failure_reason = f"HTTP {exc.status_code}: {exc.message}"
    except tabstack.APIConnectionError as exc:
        result.terminal_status = "transport_failure"
        result.failure_reason = str(exc)
    result.duration_ms = int((perf_counter() - started) * 1000)
    result.first_event_ms = manifest.first_event_ms
    result.completed_at_utc = result.completed_at_utc or utc_now_iso()
    result.event_log_path = str(run_dir / "events.sanitized.jsonl")
    result.usage.source = "unavailable"
    result.usage.notes = (
        "Tabstack exposes no per-call usage in the API response. Record console credit balance "
        "before/after the paired block, or mark unavailable."
    )
    manifest.write(run_dir / "run-manifest.json")
    return result


def hash_for(cfg: Dict[str, Any]) -> str:
    return config_hash(cfg)
