"""System A adapter: pinned search + fetch + model pipeline operated by the application.

Every provider and budget is read from a committed config file so the run is reproducible.
Search: Serper or Brave. Fetch: httpx + stdlib HTML-to-text. Model: any OpenAI-compatible chat
endpoint (OpenAI, Ollama's /v1, vLLM, etc.), selected by base URL and exact model ID.
"""

from __future__ import annotations

import json
import os
import re
from html.parser import HTMLParser
from pathlib import Path
from time import perf_counter
from typing import Any, Dict, List, Optional

import httpx

from ..sanitize import append_jsonl, utc_now_iso
from .common import OUTPUT_INSTRUCTION, Question, RunResult, config_hash, write_answer_bundle

SYSTEM_ID = "search_fetch_model"

DEFAULT_LIMITS: Dict[str, Any] = {
    "max_search_iterations": 3,
    "max_queries_per_iteration": 3,
    "max_results_per_query": 5,
    "max_pages_fetched_total": 12,
    "max_chars_per_page": 12000,
    "max_total_source_chars": 60000,
    "total_wall_clock_timeout_seconds": 300,
    "application_retries": 0,
}

PLANNER_PROMPT = (
    "You plan web research. Given a question and what has been gathered so far, return a JSON "
    'object {"queries": [...], "done": bool}. Return at most {max_queries} new search queries '
    "that would close the remaining gaps. Set done=true when the gathered evidence is enough to "
    "answer every part of the question, or when further search is unlikely to help."
)

WRITER_PROMPT = (
    "You write answers from the provided sources only. Cite with bracketed numbers like [1] that "
    "map to the numbered sources. Do not invent sources or facts that are not in the provided text."
)


class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__()
        self._skip = 0
        self.parts: List[str] = []

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in self.SKIP:
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip and data.strip():
            self.parts.append(data.strip())


def html_to_text(html: str) -> str:
    p = _TextExtractor()
    p.feed(html)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(p.parts))


def load_config(path: Path) -> Dict[str, Any]:
    cfg = json.loads(path.read_text(encoding="utf-8"))
    cfg.setdefault("limits", {})
    for k, v in DEFAULT_LIMITS.items():
        cfg["limits"].setdefault(k, v)
    cfg["system_id"] = SYSTEM_ID
    cfg["output_instruction"] = OUTPUT_INSTRUCTION
    return cfg


class Baseline:
    def __init__(self, cfg: Dict[str, Any], log_path: Path):
        self.cfg = cfg
        self.limits = cfg["limits"]
        self.log_path = log_path
        self.search_key = os.environ.get("SEARCH_API_KEY", "")
        self.model_key = os.environ.get("MODEL_API_KEY", "")
        if not self.search_key or not self.model_key:
            raise RuntimeError("SEARCH_API_KEY and MODEL_API_KEY must be set for the baseline")
        self.http = httpx.Client(timeout=httpx.Timeout(30.0), follow_redirects=True)
        self.usage_tokens = {"input": 0, "output": 0}
        self.search_calls = 0
        self.fetched: Dict[str, str] = {}
        self.fetch_failures: List[Dict[str, str]] = []

    def log(self, event: str, **fields: Any) -> None:
        append_jsonl(self.log_path, {"event": event, "received_at_utc": utc_now_iso(), **fields})

    # --- search -------------------------------------------------------------------------
    def search(self, query: str) -> List[Dict[str, str]]:
        n = self.limits["max_results_per_query"]
        provider = self.cfg["search"]["provider"]
        self.search_calls += 1
        if provider == "serper":
            r = self.http.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": self.search_key},
                json={"q": query, "num": n},
            )
            r.raise_for_status()
            items = r.json().get("organic", [])[:n]
            out = [
                {"title": i.get("title", ""), "url": i["link"], "snippet": i.get("snippet", "")}
                for i in items
            ]
        elif provider == "brave":
            r = self.http.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": self.search_key, "Accept": "application/json"},
                params={"q": query, "count": n},
            )
            r.raise_for_status()
            items = r.json().get("web", {}).get("results", [])[:n]
            out = [
                {"title": i.get("title", ""), "url": i["url"], "snippet": i.get("description", "")}
                for i in items
            ]
        else:
            raise ValueError(f"unknown search provider {provider}")
        self.log("search", query=query, results=len(out))
        return out

    # --- fetch --------------------------------------------------------------------------
    def fetch(self, url: str) -> Optional[str]:
        if url in self.fetched:
            return self.fetched[url]
        if len(self.fetched) >= self.limits["max_pages_fetched_total"]:
            return None
        try:
            r = self.http.get(url, headers={"User-Agent": self.cfg["fetch"]["user_agent"]})
            r.raise_for_status()
            text = html_to_text(r.text)[: self.limits["max_chars_per_page"]]
        except Exception as exc:  # recorded, not hidden: a failed fetch is a result
            self.fetch_failures.append({"url": url, "error": type(exc).__name__})
            self.log("fetch_failed", url=url, error=type(exc).__name__)
            return None
        self.fetched[url] = text
        self.log("fetch", url=url, chars=len(text))
        return text

    # --- model --------------------------------------------------------------------------
    def chat(self, system: str, user: str, json_mode: bool = False) -> str:
        body: Dict[str, Any] = {
            "model": self.cfg["model"]["id"],
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self.cfg["model"].get("temperature", 0),
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = self.http.post(
            self.cfg["model"]["base_url"].rstrip("/") + "/chat/completions",
            headers={"Authorization": f"Bearer {self.model_key}"},
            json=body,
            timeout=httpx.Timeout(120.0),
        )
        r.raise_for_status()
        data = r.json()
        usage = data.get("usage") or {}
        self.usage_tokens["input"] += int(usage.get("prompt_tokens", 0))
        self.usage_tokens["output"] += int(usage.get("completion_tokens", 0))
        self.log(
            "model_call",
            input_tokens=usage.get("prompt_tokens"),
            output_tokens=usage.get("completion_tokens"),
        )
        return data["choices"][0]["message"]["content"]

    # --- loop ---------------------------------------------------------------------------
    def answer(self, question: str, deadline: float) -> Dict[str, Any]:
        sources: List[Dict[str, Any]] = []
        seen_urls: set = set()
        gathered_chars = 0
        queries: List[str] = [question]
        for iteration in range(1, self.limits["max_search_iterations"] + 1):
            if perf_counter() > deadline:
                self.log("timeout", iteration=iteration)
                break
            self.log("iteration_start", iteration=iteration, queries=len(queries))
            for q in queries[: self.limits["max_queries_per_iteration"]]:
                for hit in self.search(q):
                    if hit["url"] in seen_urls:
                        continue
                    seen_urls.add(hit["url"])
                    if gathered_chars >= self.limits["max_total_source_chars"]:
                        break
                    text = self.fetch(hit["url"])
                    if text is None:
                        continue
                    budget = self.limits["max_total_source_chars"] - gathered_chars
                    text = text[:budget]
                    gathered_chars += len(text)
                    sources.append(
                        {
                            "id": len(sources) + 1,
                            "url": hit["url"],
                            "title": hit["title"],
                            "query": q,
                            "text": text,
                        }
                    )
            summary = (
                "\n".join(f"[{s['id']}] {s['title']} {s['url']}" for s in sources)
                or "(nothing yet)"
            )
            plan = self.chat(
                PLANNER_PROMPT.replace(
                    "{max_queries}", str(self.limits["max_queries_per_iteration"])
                ),
                f"Question: {question}\n\nGathered so far:\n{summary}",
                json_mode=True,
            )
            try:
                parsed = json.loads(plan)
            except json.JSONDecodeError:
                parsed = {"queries": [], "done": True}
            self.log(
                "plan",
                iteration=iteration,
                done=bool(parsed.get("done")),
                new_queries=len(parsed.get("queries", [])),
            )
            if parsed.get("done") or not parsed.get("queries"):
                break
            queries = [str(q) for q in parsed["queries"]]

        numbered = "\n\n".join(
            f"[{s['id']}] {s['title']}\nURL: {s['url']}\n{s['text']}" for s in sources
        )
        answer = self.chat(
            WRITER_PROMPT,
            f"Question: {question}\n\n{OUTPUT_INSTRUCTION}\n\nSources:\n{numbered}",
        )
        public_sources = [{k: s[k] for k in ("id", "url", "title", "query")} for s in sources]
        return {"answer": answer, "sources": public_sources}


def run(question: Question, run_dir: Path, result: RunResult, cfg: Dict[str, Any]) -> RunResult:
    run_dir.mkdir(parents=True, exist_ok=True)
    started = perf_counter()
    deadline = started + cfg["limits"]["total_wall_clock_timeout_seconds"]
    try:
        system = Baseline(cfg, run_dir / "events.sanitized.jsonl")
        out = system.answer(question.question, deadline)
        result.terminal_status = "complete" if perf_counter() <= deadline else "timeout"
        result.usage.source = "provider response usage fields (model tokens); search call count"
        result.usage.model_input_tokens = system.usage_tokens["input"]
        result.usage.model_output_tokens = system.usage_tokens["output"]
        result.usage.notes = (
            f"search_calls={system.search_calls}; pages_fetched={len(system.fetched)}; "
            f"fetch_failures={len(system.fetch_failures)}. Convert tokens to cost with the "
            "provider's published rate on the run date; record that rate in PROVE-PREFLIGHT."
        )
        write_answer_bundle(run_dir, out["answer"], out["sources"], result)
    except httpx.HTTPStatusError as exc:
        result.terminal_status = "http_failure"
        result.failure_reason = f"HTTP {exc.response.status_code} from {exc.request.url.host}"
    except Exception as exc:  # a provider error is a result, not an exclusion
        result.terminal_status = "task_failure"
        result.failure_reason = f"{type(exc).__name__}: {exc}"
    result.duration_ms = int((perf_counter() - started) * 1000)
    result.completed_at_utc = result.completed_at_utc or utc_now_iso()
    result.event_log_path = str(run_dir / "events.sanitized.jsonl")
    return result


def hash_for(cfg: Dict[str, Any]) -> str:
    public = {k: v for k, v in cfg.items() if k != "output_instruction"}
    return config_hash(public)
