# Handoff: workflow acceptance kit (Week 4 Build)

Evidence for the article "Build a 20-question evaluation set for a live-web feature"
(placeholders BUILD-01 to BUILD-05). Every output below is copied from a real run on the
recorded date. Nothing here measures typical latency, cost, accuracy, or reliability.

## 1. Repository, branch, commits

- Repository: https://github.com/Mozilla-Ocho/tabstack-cited-research-python (public)
- Branch: `feat/acceptance-evals`, created from `origin/main` at
  `ede0ac24c6d84df97570512e2da70b4ae0fa3517`. Not merged; no PR opened.
- Implementation commit used for the live pilot: `452aa44c63d110e85e0c71f3118b8a81ca80a293`
  (`implementation_dirty: false` in the pilot manifest). Two later commits change tests only.
- Changed files: `FILES.txt` in this folder (`git diff --name-status` against `origin/main`).

## 2. Install and commands, as tested

```bash
git clone https://github.com/Mozilla-Ocho/tabstack-cited-research-python.git
cd tabstack-cited-research-python
git checkout feat/acceptance-evals
uv sync --frozen
export TABSTACK_API_KEY=...

uv run cited-research-accept validate --dataset acceptance-evals/questions.candidate.jsonl
uv run cited-research-accept validate --dataset acceptance-evals/pilot/questions.pilot-q05.jsonl --allow-subset --for-run
uv run cited-research-accept freeze --dataset acceptance-evals/pilot/questions.pilot-q05.jsonl --version pilot-q05-v1 --allow-subset
uv run cited-research-accept run-one \
  --dataset acceptance-evals/pilot/questions.pilot-q05.jsonl \
  --question Q05 \
  --run acceptance-evals/runs/20261006-pilot-q05 \
  --mode fast \
  --nocache \
  --silence-timeout 120 \
  --deadline 300 \
  --pilot
uv run cited-research-accept prepare-review --run acceptance-evals/runs/20261006-pilot-q05
uv run cited-research-accept summarize --run acceptance-evals/runs/20261006-pilot-q05
```

The freeze record is committed, so a fresh clone skips `freeze` (it refuses to replace an
existing freeze record). A fresh-clone run of the same `run-one` command into the committed run
directory is refused because Q05 already has an attempt there; use a new `--run` directory.

## 3. Versions and lockfile

macOS Darwin 25.6.0 arm64; uv 0.11.6; Python 3.12.13 (pilot and receipt); `tabstack==2.8.5`
from `uv.lock`, which is unchanged from `origin/main`. Offline tests also pass on Python 3.9.6.

## 4. Offline test receipt and fixtures

`TEST-OUTPUT.txt`: ruff check, ruff format --check, pyright (standard), pytest: 158 passed
(65 in `tests/test_accept.py`, 93 pre-existing). Python 3.9.6: 158 passed.

Fixtures: `tests/fixtures/*.jsonl` replayed through the SDK's own `ResearchEvent` model
(`complete-events`, `complete-ordered-sources`, `complete-no-cited-pages`,
`complete-malformed-sources` (duplicate and unsafe URLs), `error-events`, `truncated-events`,
`duplicate-complete`), plus raised `APIStatusError` 401/429/500, `APIConnectionError`,
`httpx.RemoteProtocolError` mid-stream, a stalled stream (silence timeout), a stream that keeps
sending events (overall deadline), and a `KeyboardInterrupt` inside the request.
`tests/acceptance_synth.py` builds the synthetic run used for the summary demo.

## 5. Live pilot

- Run: `acceptance-evals/runs/20261006-pilot-q05/`, one `POST /research`, `mode: fast`,
  `nocache: true`, `sdk_max_retries: 0`, `application_retries: 0`, `pilot_only: true`.
- Dispatched 2026-10-06T17:49:45.752Z, terminal 2026-10-06T17:49:54.673Z; first event 645 ms,
  terminal 8,882 ms (client-observed, one run). Exit 0, `complete`, 3 cited pages.
- Terminal output: `pilot-stdout.txt` (stderr empty, exit code 0).
- Report `answers/Q05-a1/report.md`; ordered sources `answers/Q05-a1/sources.json`; ledger
  `attempts.jsonl`; timeline `answers/Q05-a1/events.sanitized.jsonl`; reviews in `reviews/`.
- Sources in returned order: 1 `https://docs.ollama.com/capabilities/web-search`,
  2 `https://docs.ollama.com/capabilities/web-search.md` (flagged `duplicate_of_position: 1`),
  3 `https://dev.to/aairom/testing-ollama-web-search-and-a-thinking-model-1dh7`. All three had
  `claims: []`.

## 6. Review (reviewer: Claude (AI agent), pending human confirmation)

Reference passage, re-fetched before the run with one curl request
(`../evidence/Q05-ollama-web-search.txt`, retrieved 2026-10-06T17:37:54Z): "content (string):
relevant content snippet from the web page". It matches the article's frozen passage word for
word.

Coverage row (as written in `reviews/coverage.csv`):

```csv
20261006-pilot-q05,Q05-a1,Q05,E2,"Describe content as a relevant snippet, not the full page.","the `content` field within each web search result contains a **relevant content snippet** from the web page, not the full page content [1][2][3]",2,Matches the frozen passage 'content (string): relevant content snippet from the web page' and answers the full-page half of the question.,"Claude (AI agent), pending human confirmation",2026-10-06T17:50:51Z
```

Claim row (as written in `reviews/claims.csv`):

```csv
20261006-pilot-q05,Q05-a1,Q05,C04a,"For retrieving the **full content** of a specific web page, Ollama offers a separate `web_fetch` API",[1][2],yes,https://docs.ollama.com/capabilities/web-search,Web fetch API: Fetches a single web page by URL and returns its content.,Documentation as retrieved; no version or date stated in this section.,2026-10-06T17:37:54Z,1,"web_fetch is documented as a separate API on the same page, but the docs say 'main content', not 'full content'; the answer upgrades the qualifier.","Claude (AI agent), pending human confirmation",2026-10-06T17:50:51Z,[2] likely same page as [1]
```

Result: coverage E1, E2, E3 all `2`. Claims (enumeration locked; the report's fourth sentence
was split into C04a and C04b): C01, C02, C03, C04b `2`; C04a `1`. CF1 ("States that web_search
returns full page content.") not triggered. `decision` left blank: no release criteria beyond
CF1 are frozen for this pilot, so accept/reject is for the human release owner. The dev.to page
([3], cited on C03 only) was not opened: the approved boundary allowed one docs fetch. Source [2]
is the `.md` variant of [1], so C01 to C04 rest on one official page, not two.

## 7. Summary output

- Live pilot: `summary-live-pilot.txt` (pilot scope; live scored totals are empty by design).
- Offline fixture: `summary-offline-synthetic.txt`, run
  `acceptance-evals/examples/synthetic-run/` (6 synthetic attempts: 3 complete, 1 stream error,
  1 HTTP 401, 1 early close; review values set by the fixture script).

## 8. Usage

Unavailable. The `/research` stream has no per-request credit or billing field in SDK 2.8.5:
`complete.metadata.metrics` carries `tokens` (input/output per model ID), fetch, search and
iteration counts, and phase timings, and the sanitizer does not keep `metrics`. No console or
billing record was checked for this attempt. `reviews/usage.csv` is blank for Q05-a1; the
summary reports "usage: 0 known, 1 missing" and cost per accepted answer unavailable.

## 9. Failure behavior and failed-command output

`failure-evidence/`: `validate --for-run` on the candidate set (exit 1, 19 pending rows),
`freeze` on the candidate set (exit 1, nothing written), `run-one` with the key unset (exit 5,
no directory created), `run-one` re-run on the pilot directory (exit 1, refused before any
request; run with a dummy key and `TABSTACK_BASE_URL=http://127.0.0.1:9` so no request could
reach the API), `prepare-review` re-run (exit 0, every existing row kept). Every terminal state
in the table in `../README.md` is exercised offline through `run-one` except
`malformed_complete` and `unexpected_error`, which are covered at the runner level in
`tests/test_trace.py`. None was produced live.

## Not tested

- No live failure: HTTP rejection, streamed `error`, transport loss, early close, duplicate
  terminal event, silence timeout, and the overall deadline are fixture-only.
- `--mode balanced`, `--fetch-timeout`, and `--another-attempt` against the live API.
- Any batch beyond one question. The other 19 candidate questions are `pending`; their reference
  URLs were not fetched and their criteria have not been checked against a source.
- A second engineer or machine reproducing the pilot; Linux and Windows.
- Cost per accepted answer with real usage (only synthetic usage values reach the "available"
  branch).
- Spreadsheet round trips of the acceptance sheets (semicolon or BOM CSVs are read through the
  trace path's `read_sheet`, tested there, not with these sheets).
- Whether the service cancels a task after a client timeout.

## Article vs implementation

- Commands: the article placeholders should use `cited-research-accept validate | freeze |
  run-one | prepare-review | summarize`, with `--run DIR` (not the brief's `web-eval ...
  --output`). `freeze` is an extra step the brief describes ("Hash and version") but does not
  name.
- Output tree: the implementation adds `reviews/decisions.csv` (critical failures, claim
  enumeration lock, accept/reject) and `reviews/usage.csv`, and each `answers/<attempt-id>/`
  also holds `events.sanitized.jsonl`, `run-manifest.json`, `question.txt`, `trace-diagram.md`,
  and the traced runner's own `review-sheet.csv` (not read by the summary). The dataset gets a
  sibling `<file>.freeze.json`.
- The article's Q05 JSON shows `"retrieved_at_utc": "2026-10-03T20:44:32.190Z"`; the committed
  record now says `2026-10-06T17:37:54Z`. The passage and other fields are unchanged.
- Live runs refuse evidence older than 30 days by default (`max_evidence_age_days` in the
  freeze record). The article asks for a refresh but names no window.
- The brief's "category count other than four" is implemented as five categories of four.
