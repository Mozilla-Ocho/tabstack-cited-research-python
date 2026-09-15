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

## System A, search + fetch + model: NOT frozen

Adapter implemented (`src/cited_research/harness/baseline.py`): Serper or Brave search, httpx
fetch with stdlib text extraction, any OpenAI-compatible chat endpoint, planner/writer prompts in
code, budgets from `evals/baseline-config.json` (defaults per protocol §3). Blocked on:

1. Search provider and key (`SEARCH_API_KEY`).
2. Model decision: provider, base URL, exact model ID, and the one-line rationale for why it is the realistic deployment, not the weakest. Per the brief, this is asked, not chosen.
3. Cost source for the chosen providers (published rates on the run date).

Once decided: copy `baseline-config.example.json` to `baseline-config.json`, fill in, commit. That commit becomes the System A freeze.

## Q01 pilot status

| System | Status | Duration | Notes |
|---|---|---|---|
| tabstack_research_fast | complete | 14,634 ms (first event 376 ms) | 5 cited pages, all `claims` empty, 2 duplicate URL variants; answer has no Sources section despite the output instruction |
| search_fetch_model | not run | | blocked on items above |

Pilot batch: `evals/runs/20260915T192745-pilot/`. `pilot_only=true` on every row. Not to be included in any denominator.

## Blind packaging

Works for the managed run: `evals/runs/20260915T192745-pilot/blind/` contains one file named by a random 8-hex ID with the answer text only, provider name replaced. Timing, cost, event log, and system ID live only in `result.json`.

## Blockers before the full 12-question run

1. System A frozen and committed (above).
2. Cost capture plan for System B: read console balance before and after each paired block, or accept `unavailable` across the study and say so in the article.
3. A second evaluator named (the builder cannot be the sole scorer).
4. Decide runs per cell: 1 (exploratory) or 3.
5. Decide whether Q01 pilot outputs are discarded (default) before the full run.
