# Prove preflight

**FULL_EVALUATION_RUN=false**

## Frozen

- Protocol: v1, `evals/PROTOCOL.md`, commit `116d90191c6d900a97779ac0fc07dc9ffcfc229a` (committed before the pilot ran; the pilot's `results.csv` records that SHA in `protocol_commit`).
- Questions: `evals/questions.jsonl`, 12 questions, composition verified by test (3/5/2/2).
- Rubric: `evals/rubric.md`. Results template: `evals/results-template.csv`; harness CSV columns are tested to match it exactly.

## System B, managed Research: frozen

```yaml
provider: Tabstack
endpoint: /research
mode: fast
nocache: true
fetch_timeout: default
application_retries: 0
sdk: tabstack==2.8.5 (Python 3.12.13)
sdk_transport_retries_default: 2
config_hash: c7a6f61cc520eb46
```

## System A, search + fetch + model: frozen at commit `22c1613a8e2546acb429c818d61a5327706004ed`

```yaml
search: Brave Web Search API (GET /res/v1/web/search), key in SEARCH_API_KEY
fetch: httpx 0.28.1, follow_redirects, 30 s timeout, stdlib html.parser text extraction
model: OpenAI gpt-5.5-2026-04-23 via https://api.openai.com/v1, temperature omitted (model rejects non-default)
rationale: current flagship at a dated snapshot; not mini/nano (would weaken the baseline), not pro (not a realistic per-query deployment)
prompts: PLANNER_PROMPT and WRITER_PROMPT in src/cited_research/harness/baseline.py
limits: protocol section 3 defaults (3 iterations x 3 queries x 5 results, 12 pages, 12k chars/page, 60k total, 300 s, 0 retries)
config: evals/baseline-config.json, config_hash recorded per run in system-config.json
cost sources: Brave plan price per query on run date; OpenAI published gpt-5.5 rate x usage tokens
```

## Q01 pilot status

Two pilot batches. Batch 1 (`evals/runs/20260915T192745-pilot/`, run 1) ran the managed system only, before System A was frozen. Batch 2 (`evals/runs/20260915T203139-pilot/`, run 2) ran both systems back to back.

| System | Batch | Status | Duration | Observations |
|---|---|---|---|---|
| tabstack_research_fast | 1 | complete | 14,634 ms (first event 376 ms) | 5 cited pages, all `claims` empty, 2 URL variants of one page, no Sources section |
| tabstack_research_fast | 2 | complete | 16,666 ms (first event 720 ms) | 3 cited pages (2 variants of the same docs page + the blog post), 1,577 chars, no Sources section |
| search_fetch_model | 2 | complete | 69,434 ms | 7 Brave searches, 9 pages fetched, 0 fetch failures, 16,494 input + 5,712 output tokens; 3,879 chars with a Direct answer, numbered citations, and a Sources section |

`pilot_only=true` on every row. Pilot outputs are for harness validation and are excluded from any denominator. No quality scoring was done on them; the answer-quality differences above are size and structure only.

## Blind packaging

Works for both systems: `evals/runs/20260915T192745-pilot/blind/` contains one file per system named by a random 8-hex ID with the answer text only, provider name replaced. Note: the baseline answer's structure (Direct answer heading, Sources section) and the managed answer's single-paragraph shape are visible in the blind text, so the evaluator can likely tell them apart. Record this as a blinding limitation in the article. Timing, cost, event log, and system ID live only in `result.json`.

## Blockers before the full 12-question run

1. ~~System A frozen and committed~~ done, `22c1613`.
2. Cost capture plan for System B: internal `api_request` telemetry gives `action_count` per `trace_id` (all three fast-mode calls so far: 1). Decide whether action_count x published rate is acceptable as the cost line (labelled as such) or whether a console balance delta per paired block is required.
3. A second evaluator named (the builder cannot be the sole scorer).
4. Decide runs per cell: 1 (exploratory) or 3.
5. Decide whether Q01 pilot outputs are discarded (default) before the full run.
