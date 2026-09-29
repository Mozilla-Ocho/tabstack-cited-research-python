# Week 3 Build findings: trace a question from request to source-backed answer

What we saw is kept separate from what we designed. One live run. Nothing here measures typical
latency, cost, accuracy, or reliability.

## Identity and prior status

- Repository: https://github.com/Mozilla-Ocho/tabstack-cited-research-python, branch
  `week-3-trace` (local; not pushed at time of writing), branched from `main` at `6325338`.
- Implementation commit used for the live run: `82b9be98d4add677d1fe650b1a0701d4d198189a`
  (recorded in `artifacts/week-3-trace/run-manifest.json`). A later commit on the branch changes
  only file modes for atomic writes, plus docs and artifacts.
- Week 1: built and verified in this repo (implementation `759f10a`, sample run 2026-09-15).
  The Week 1 handoff files (`week-1-build-prove/*.md`) were not on this machine; this repo and its
  `handoff/BUILD-FINDINGS.md` were the source of truth.
- Week 2: **not built.** No `ApplicationResponse` or `research` route exists anywhere under the
  Tabstack workspace. This trace runs on the Week 1 CLI, not a Week 2 application layer.
- Week 3 Understand rubric (`01-UNDERSTAND-what-makes-a-citation-useful-draft.md`): not on this
  machine. The review-sheet columns are **provisional**.

## Environment (live run)

- 2026-09-29, started 18:23:24.612Z UTC, macOS Darwin 25.5.0 arm64
- Python 3.12.13 via uv 0.11.28, `tabstack==2.8.5` from `uv.lock` (unchanged from Week 1)
- Command: `artifacts/week-3-trace/command.txt`. Key from `TABSTACK_API_KEY` in the environment.

## Design (what was built)

| Spec item | Where |
|---|---|
| Single request, single event loop, no application retry | `tabstack_runner.run_research`, `EventPump` |
| Timeline: event, `seq`, local monotonic `elapsed_ms`, allowlisted fields, redacted message | `sanitize.sanitize_event` |
| Raw server timestamp and its observed type | `timestamp`, `timestamp_type`; manifest `timestamp_types` |
| Atomic JSONL and artifact writes on every exit path | `sanitize.write_jsonl_atomic`, `write_text_atomic` |
| Separate exit states: task error 2, HTTP 3, transport 4, premature close 6, silence 7, protocol 8 | `EXIT_CODES` |
| Duplicate terminal event rejected | `consume_trace` raises `ProtocolError` |
| Cited pages in returned order, public-link check, duplicates flagged, credentials stripped | `models.build_cited_pages`, `urls.py` |
| `sources=[]` and `review_needed_no_sources` when citations are absent | `persist_trace` |
| Review worksheet with blank judgments | `review.review_sheet_csv` |
| Per-run annotated diagram | `review.trace_diagram_md` |

Kept unchanged for the frozen Prove harness (System B): `consume_stream`, `persist_complete`,
`Source`, `append_jsonl`. The harness's event logs now also get `known_event` and
`timestamp_type`, and messages are redacted; the request it sends is unchanged.

Offline tests: 57 passed (`handoff/week-3/TEST-OUTPUT.txt`), covering complete with and without
citations, streamed error, premature close, silence before and between events, stream left open
after complete, duplicate terminal events, malformed cited pages, sensitive-message redaction,
URL validation, source ordering, identical server timestamps, HTTP and transport opening
failures, and a scan for secret-shaped strings in all artifacts. The same 57 passed from a fresh
clone with `uv sync --frozen` and no API key.

## Observed (one live run)

- `complete`, exit 0, 17,091 ms local. First event at 496 ms. 10 events, each once, in the same
  order as the Week 1 run: `start, planning:start, planning:end, iteration:start,
  searching:start, searching:end, iteration:end, writing:start, writing:end, complete`.
- The stream closed by itself after `complete` (`stream_closed_after_terminal: true`).
- Where the time went, by local arrival: planning about 1.1 s, searching about 5.3 s, writing
  about 10.2 s. These are gaps between events as the client saw them, not server stage timings.
- Several events arrive together (same `elapsed_ms`, same server `timestamp`): `start` and
  `planning:start`; `planning:end`, `iteration:start` and `searching:start`; `searching:end`,
  `iteration:end` and `writing:start`. Order comes from `seq`, not from timestamps.
- `timestamp` is a float (epoch milliseconds) on every event. This matches the SDK type
  (`timestamp: float`) and Week 1 finding #4, not the ISO string in the docs guide.
- `urls_found = urls_new = 6`, `pages_analyzed = 3`, 3 cited pages. Report 1,621 characters,
  one paragraph, inline `[n]` markers only, no Sources section.
- `claims` was `[]` on all 3 cited pages. That makes 15 of 15 cited pages across three fast-mode
  runs. The API's `claims` field cannot supply the claim-to-source mapping in fast mode.
- Positions 1 and 2 are `docs.ollama.com/capabilities/web-search` and the same path with `.md`.
  `duplicate_of_position` does not flag this, by design (a `.md` path can be a different
  resource); a reviewer should treat them as probably the same page.
- Every cited page had the same six `source_queries`, so that field does not tell you which
  query surfaced which page.
- Review sheet: 10 candidate sentences, 5 with no inline marker (C01, C02, C03, C06, C08). The
  uncited ones include specific factual statements (the endpoint URL, the API-key requirement,
  `max_results` default 5 and cap 10, the MCP server filename and clients, the named cloud
  models).
- Noticed while inspecting the artifact, **not** a formal review: the report expands MCP as
  "Multi-Context Processor". Model Context Protocol is the usual expansion, and that sentence has
  no citation. This is the kind of thing the worksheet exists to catch.
- Secrets scan: the API key value is absent from the artifacts, source, tests, and handoff. There
  are no bearer, authorization, cookie, `sk_` key, or traceback patterns in
  `artifacts/week-3-trace/`. This is a grep-based scan; there is no gitleaks binary on this
  machine.
- Cost: not measured. No usage in the response, and telemetry was not queried for this run.

## Not done / blockers

- Review sheet judgments: **not filled.** Every judgment is blank. Needs a technical reviewer.
- Rubric: column names are not reconciled with the Understand draft.
- Second-engineer reproduction of the live sample: not done; no second live call was authorized.
- Tested on Python 3.12 only; the declared floor is 3.9.
- Content and security review of `report.md` and `sources.json` before publication: pending.
