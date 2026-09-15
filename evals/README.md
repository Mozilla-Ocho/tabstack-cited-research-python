# Evaluation harness

Pre-registered comparison of two complete systems on the same frozen questions:

- **System A** `search_fetch_model`: search API + HTTP fetch + an OpenAI-compatible model, with
  fixed budgets. Adapter: `src/cited_research/harness/baseline.py`.
- **System B** `tabstack_research_fast`: one Tabstack `/research` call, `mode=fast`,
  `nocache=true`. Adapter: `src/cited_research/harness/managed.py`.

Read `PROTOCOL.md` first. It was committed before any comparison output was inspected. Scoring
rules live in `rubric.md`. Questions are frozen in `questions.jsonl`.

## Pilot (harness validation only)

```bash
cp evals/baseline-config.example.json evals/baseline-config.json   # fill in the model decision
export TABSTACK_API_KEY=... SEARCH_API_KEY=... MODEL_API_KEY=...
uv run cited-research-eval --question Q01 --system both
```

Every row is written with `pilot_only=true`. Outputs land in `evals/runs/<utc-stamp>-pilot/`:
per-system `answer.md`, `sources.json`, `events.sanitized.jsonl`, `result.json`,
`system-config.json`, a `blind/` directory of answers under random IDs, and `results.csv` in the
template's column order.

## Full run

Gated behind `--allow-full`. Do not pass it until the protocol commit SHA is recorded in
`handoff/PROVE-PREFLIGHT.md` and the baseline model decision is committed in
`baseline-config.json`. Changing any scoring rule after seeing outputs means protocol v2 and a
full rerun.

## What the harness does not do

It does not score. Scoring is human, blind, and recorded in `results.csv` per `rubric.md`. It
does not estimate Tabstack cost; the API returns no usage, so `tabstack_credits` stays empty
unless a console balance delta is recorded by hand.
