# Build findings

Observations only. No conclusions about typical behavior, cost, accuracy, or other tools.

## Identity

- Repository: https://github.com/Mozilla-Ocho/tabstack-cited-research-python (public), branch `main`.
- Implementation commit (used for the production run): `759f10a6f41e2a3ebeaaeab588daaa535ed56346`
- Protocol commit (evals/ frozen before the pilot): `116d90191c6d900a97779ac0fc07dc9ffcfc229a`
- License: MIT. Secret scan: the API key value and common token patterns are absent from the tree (grep-based; see TEST-OUTPUT for versions). No gitleaks binary on this machine.

## Environment

- macOS Darwin 25.6.0, arm64
- Python 3.12.13 via uv 0.11.6 (`.python-version` pins 3.12; `requires-python >= 3.9`, code is 3.9-compatible, not executed on 3.9)
- `tabstack==2.8.5` from PyPI, locked in `uv.lock`
- Install: `uv sync --frozen`. Run: see `artifacts/sample-run/command.txt`.

## Version discrepancy: docs 2.6.1 vs 2.8.5

PyPI's current release is 2.8.5 (checked 2026-09-15; the three most recent releases are 2.8.3, 2.8.4, 2.8.5). The public Python quickstart shows 2.6.1. `Mozilla-Ocho/tabstack-python` at `1ddba54` is 2.8.5. The research request parameters (`query`, `mode`, `nocache`, `fetch_timeout`) are identical in 2.6.1 and 2.8.5, so the mismatch did not change the code, only the pinned version.

## Production run (Build question)

Numbers in `artifacts/sample-run/run-manifest.json` and `artifacts/sample-run/README.md`. Summary: complete, exit 0, 20,299 ms, first event at 1,064 ms, 10 events each once in the documented order, 7 pages analyzed, 7 cited pages, report 1,649 chars.

## Failure paths

| Path | How produced | Result |
|---|---|---|
| Missing key | `TABSTACK_API_KEY` unset | exit 5, message on stderr, no request made |
| Invalid key | one request with a fake key | HTTP 401 `Unauthorized - Invalid token`, exit 3, 195 ms, manifest `terminal_status=http_error` |
| Streamed `error` event | synthetic fixture only | exit 2, message and activity printed, stack field dropped from the public log |
| Stream ends without `complete` | synthetic fixture only | exit 2 |

Files: `handoff/failure-evidence/`.

## SDK and docs behavior observed

1. **`claims` is empty on every cited page in fast mode.** Both the Build run (7 pages) and the Q01 pilot (5 pages) returned `"claims": []` for every source. The field is present, as typed, but carries nothing. The docs guide describes `claims` as "the specific statements drawn from that page." Whether balanced mode populates it was not tested (paid plan). Consequence for the Build article: the claim-to-source mapping cannot be shown from fast mode output; only report-level [n] citations can.
2. **Duplicate sources under URL variants.** Build run: `https://docs.ollama.com/capabilities/web-search`, the same with `http://`, and the same with `.md` appear as three cited pages. Pilot: `ollama.com/blog/web-search` and `ollama.com//blog/web-search` (double slash) both appear. Cited-page counts therefore overstate distinct sources.
3. **`relevance`, `reliability`, `summary` absent in fast mode**, as the SDK docstring says. `title` was present on all 12 cited pages across both runs.
4. **`timestamp` is epoch milliseconds (float in Python), not ISO-8601.** The docs guide shows an ISO string. Already filed from the Understand-lane review.
5. **No `done` event.** The stream closes after `complete`. Matches the brief.
6. **`metadata.metrics` is typed in the SDK but never on the wire.** The API strips it server-side.
7. **Report shape in fast mode:** a single paragraph, no headings, a Sources section appeared in one of the two pilot runs that asked for it (run 2) and not the other (run 1), so it is not dependable. Citations are inline [n] only; the reader must join them to `citedPages` by order.
8. **Source selection in fast mode admitted off-topic pages.** Two of seven Build-run sources are "build a web scraper with Ollama" tutorials (Medium, GitHub); the report then lists "custom web scrapers" as a way to add web search. That is a source-quality observation for the evaluation, not a bug.
9. **No usage or cost in the response, no usage API.** With Tessa's PostHog key, the internal `api_request` events for both calls were retrieved by `trace_id`: each records `action_count = 1`, `research_mode = fast`, `task_success = true`, server-side `duration_ms` 19,258 (Build run) and 14,278 (pilot). No credit amount is on the event and no credit-transaction event exists. At the public 250 credits per fast Research action, one action implies 250 credits per call; the article can say "one action" from telemetry but must label 250 as the implied rate, not a receipt. Note for the pricing page: it says Research runs "a variable number of actions per call"; both fast-mode calls here ran exactly one action despite 7 and 5 pages analyzed. Source is internal telemetry, so cite it as "Tabstack request telemetry" and not by tool name.

## Friction

- Biggest: no way to get per-call cost programmatically. Everything else was smooth: the typed
  event union made the runner short, and the fixture tests were straightforward because
  `construct_type` parses JSONL into the same models the stream yields.
- Minor: the SDK's Python types use snake_case (`cited_pages`, `total_pages_analyzed`) while the
  wire and docs use camelCase; fixtures must be camelCase to parse.

## Limitations

- One run per question. Nothing here is a benchmark.
- Not executed on Python 3.9 despite the declared floor.
- `terminal.png` is a rendering of the captured stdout, not a screenshot of a live terminal.
- No credit receipt. Action count from internal telemetry only.
