# cited-research

One Tabstack `/research` call from Python: streamed progress on the terminal, a final Markdown
report, and a machine-readable list of the sources the report cites. Plus a pre-registered
evaluation harness for comparing that managed call against a search + fetch + model pipeline you
run yourself.

Built for people running their own model inside a product, assistant, agent, or internal
workflow that needs a current, cited answer from the public web.

## What you get

```text
artifacts/trace-run/
├── question.txt              the question asked (API key values scrubbed, like command.txt)
├── command.txt               the exact command that ran (no secrets; the CLI takes none)
├── events.sanitized.jsonl    lifecycle timeline: event, seq, elapsed_ms, allowlisted fields
├── report.md                 the final Markdown report
├── sources.json              cited pages in returned order, with link checks
├── review-sheet.csv          report sentences joined to cited pages; judgments left blank
├── trace-diagram.md          the timeline as a Mermaid sequence diagram, plus its limits
├── run-manifest.json         mode, timings, terminal status, review state, caveats, versions
└── stdout.txt / stderr.txt   what the terminal showed
```

`artifacts/sample-run/` is the first sample run (2026-09-15, schema 1, before the trace files existed).
`artifacts/trace-run/` is the trace run (2026-09-29, schema 2). See
[`docs/trace/BUILD-FINDINGS.md`](docs/trace/BUILD-FINDINGS.md).

## Prerequisites

- Python 3.9 or later (tested on 3.12.13)
- [uv](https://docs.astral.sh/uv/) (tested with 0.11.6)
- A Tabstack API key from <https://console.tabstack.ai>, exported as `TABSTACK_API_KEY`

The CLI reads the key from the environment only. There is no `--api-key` flag and the key is
never written to any output file.

## Run it

```bash
git clone https://github.com/Mozilla-Ocho/tabstack-cited-research-python.git && cd tabstack-cited-research-python
uv sync --frozen
export TABSTACK_API_KEY=...

uv run cited-research \
  --query "What are the current ways to add web search to an Ollama-based application, and what output does each approach return?" \
  --mode fast \
  --nocache \
  --silence-timeout 120 \
  --output artifacts/my-run
```

Flags: `--mode fast|balanced` (default `fast`; `balanced` needs a paid plan), `--nocache` to
bypass the content cache, `--fetch-timeout SECONDS`, `--silence-timeout SECONDS` (stop waiting
if no event arrives for that long; off by default), `--quiet`.

Terminal output from the committed trace run (2026-09-29):

```text
start  Starting research
planning:start  Planning research strategy
planning:end  Planning complete: 6 queries
iteration:start  iteration 1/1  Starting iteration 1 of 1
searching:start  iteration 1  Searching with 6 queries
searching:end  iteration 1  6 new urls  Found 6 URLs
iteration:end  iteration 1  Iteration 1 complete (fast mode)
writing:start  Writing report
writing:end  Report draft complete
complete  report -> artifacts/trace-run/report.md
sources (3) -> artifacts/trace-run/sources.json
  1. Web search  https://docs.ollama.com/capabilities/web-search
  2. Web search  https://docs.ollama.com/capabilities/web-search.md
  3. Subagents and web search in Claude Code  https://ollama.com/blog/web-search-subagents-claude-code
review sheet -> artifacts/trace-run/review-sheet.csv (unreviewed)
```

## How it works

```text
your app ──POST /research (SSE)──▶ Tabstack
   ▲                                  │ start, planning:*, iteration:*, searching:*, writing:*
   │                                  ▼
   └── report.md + sources.json ◀── complete { report, metadata.citedPages, ... }
```

`src/cited_research/tabstack_runner.py` opens the client as a context manager and reads the
stream on a worker thread, so the main thread can wait with a silence timeout. It prints one
line per progress event and records each event's arrival order and local elapsed time. After
`complete` it listens up to 2 s more for the stream to close; a second `complete` or `error` is
a protocol error. `error` raises and exits non-zero. There is no `done` event on `/research`;
`complete` (or `error`) is the last event, then the stream closes.

The timeline is a **request lifecycle**, not a source-by-source execution trace. A
`searching:start` event says the service reports it is searching; it does not say which URL was
fetched. Only `complete.metadata.cited_pages` identifies sources.

## Exit codes

| Code | Meaning | Where it is decided |
|---|---|---|
| 0 | `complete` received, files written | |
| 2 | Task-level failure: a streamed `error` event | inside the stream |
| 3 | HTTP rejection before the stream opened (401, 400, 429, ...) | SDK raises `APIStatusError` |
| 4 | Connection or transport failure before the stream opened | SDK raises `APIConnectionError` |
| 5 | `TABSTACK_API_KEY` not set; no request is made | CLI |
| 6 | Stream closed before `complete` or `error` | inside the stream |
| 7 | `--silence-timeout` elapsed with no event; the request may still run and bill | CLI |
| 8 | Protocol error: a second terminal event (`complete` or `error`) after `complete` | inside the stream |
| 9 | Connection dropped or timed out after the stream opened (`RemoteProtocolError`, `ReadTimeout`, ...); the request was accepted and may bill | `httpx` raises `TransportError` while reading |
| 10 | `complete` arrived without a report string; nothing is written as `report.md` | CLI |
| 11 | Any other unexpected failure; the files are still written | CLI |

Changed in schema 2: a stream that closes early exits 6 (it was 2). Exits 7 and 8 are new.
Exits 9, 10, and 11 were added after the trace run. Before them a mid-stream failure exited 1
with a traceback and wrote no `run-manifest.json`, and a `complete` without a report exited 8.
Exit 12 (`deadline_exceeded`, an overall client deadline) is used only by
`cited-research-accept run-one --deadline`; this CLI has no `--deadline` flag.
Every exit except 5 writes `run-manifest.json`, `events.sanitized.jsonl`, and
`trace-diagram.md`.
`run-manifest.json` records the same state as `terminal_status`: `complete`, `task_error`,
`http_error`, `transport_error`, `premature_close`, `silence_timeout`, `protocol_error`,
`stream_transport_error`, `malformed_complete`, or `unexpected_error`.

Observed on 2026-09-15 with a fake key: `request rejected (HTTP 401): Unauthorized - Invalid token`,
exit 3, 195 ms. The `error`-event path is covered by a synthetic fixture test only; we did not try
to make production fail.

## Timeouts, retries, cleanup

- The SDK sets `timeout = 600` on `/research`, which becomes `httpx.Timeout(600)`: connect,
  read, write, and pool are each 600 s. On a stream that is a 600 s silence timeout between
  events, not a cap on the whole request; a run that keeps sending events is never cut off by
  it. If it fires mid-stream the CLI exits 9. This CLI adds no total timeout either.
  `--silence-timeout` is optional and measures the gap between events (including the wait for
  the first one), not total duration.
- The SDK retries transport-level failures (connection errors, 408, 409, 429, 5xx) twice by
  default. The CLI turns that off (`Tabstack(max_retries=0)`): a request that failed at the
  connection level may already have been accepted, and re-sending it could re-run and re-bill the
  research. A 429 or 5xx therefore exits 3 at once, and a connection failure exits 4. There are
  no application-level retries either. The manifest records both (`sdk_max_retries: 0`,
  `application_retries: 0`). Changed in schema 2; earlier sample runs and the evaluation harness used
  the SDK default of 2.
- The docs guide says there is no server-side limit on total duration and recommends watching
  for stream silence. That is what `--silence-timeout` does.
- The client is a context manager; the HTTP response is closed when the loop exits, including on
  error.

## What is in `sources.json`, and what is not

Kept per cited page, in the order returned: `position` (1-based), `id`, `url`, `title`,
`claims`, `source_queries`, `relevance`, `reliability`. Added by this CLI: `link_ok` and
`link_issue` (only absolute http(s) URLs on public hosts are rendered as links),
`duplicate_of_position` (scheme, `www.`, and slash variants of an earlier URL; flagged, not
removed), and `malformed_fields`. Credentials inside a URL are replaced with `[REDACTED]`.
Dropped on purpose: `full_text`, `summary`, `depth`, `parent_url`, `url_source`. A `complete`
with no `cited_pages` gives `sources.json` = `[]` and `review_state = review_needed_no_sources`.

The event log keeps event names, arrival order (`seq`), local monotonic `elapsed_ms`, the raw
server timestamp as `timestamp_raw` with its observed type (`timestamp_type`; the guide says ISO
string, the API reference and SDK say number, and the wire sent a float in epoch milliseconds),
allowlisted counters, and status
messages with URLs, emails, and credential-shaped strings redacted. It never duplicates the
report or page text, and a denylist strips anything that looks like a key, header, cookie, stack
trace, or environment dump.

## Reviewing citations

`review-sheet.csv` follows the per-claim record in the article "What makes a
citation useful". It is written as UTF-8 with a byte-order mark (`utf-8-sig`) so Excel on
Windows decodes it. Its first nine columns are the rubric's, in order:

| Column | Filled by | Content |
|---|---|---|
| `claim_id` | CLI | `C01`, `C02`, ... one per report sentence |
| `answer_text` | CLI | the sentence, qualifiers included |
| `citation_ids` | CLI | its inline markers, e.g. `[1][2]`; empty if none |
| `source_url` | CLI, then reviewer | returned URLs for those markers; replace with the page you opened |
| `passage` | reviewer | short supporting or contradicting excerpt |
| `source_date_or_version` | reviewer | date or version, or `unknown` |
| `retrieved_at_utc` | reviewer | when you opened the page |
| `support` | reviewer | `2` supported at stated scope, `1` partial, `0` unsupported, `U` couldn't inspect |
| `reason` | reviewer | one sentence naming the gap |

Three helper columns follow and are not part of the rubric: `cited_page_ids`, `auto_flags`
(no inline citation, marker with no cited page, not linked, likely duplicate page), and
`api_claims`. Rows are candidates. Split, merge, or delete them until each row is one material
claim. The CLI never fills `support`.

`[n]` is joined to the cited page at position `n`. The SDK documents `cited_pages` as "ordered
by first citation appearance"; the public guide does not say, so the reviewer confirms it. The
API's own `claims` list is context, not verification; in fast mode it has been empty on every
page so far.

To rebuild the sheet for an existing run without another API call:

```bash
uv run cited-research-review artifacts/my-run
```

It refuses to overwrite a sheet that already has entries in any reviewer column, or whose
`claim_id`/`answer_text` rows no longer match what it would generate (rows split, merged, or
deleted); `--force` overrides that. Sheets saved with `;` or tab delimiters (EU-locale Excel)
are read correctly. The committed `artifacts/trace-run/review-sheet.csv` has a first-pass review,
described in `artifacts/trace-run/REVIEW-NOTES.md`.

## Evaluation harness

See [`evals/README.md`](evals/README.md) and [`evals/PROTOCOL.md`](evals/PROTOCOL.md). The
protocol was committed before any comparison output was inspected. Only the Q01 harness pilot has
run; `FULL_EVALUATION_RUN=false`.

## Acceptance evaluation kit

`cited-research-accept` and [`acceptance-evals/`](acceptance-evals/README.md) check whether one
current-answer workflow meets its own acceptance rules: a versioned 20-question set (five
categories of four), one frozen question per request, separate coverage and claim reviews, and a
summary that keeps failures, blank or `U` scores, and missing usage visible.

```bash
uv run cited-research-accept validate --dataset acceptance-evals/questions.candidate.jsonl
uv run cited-research-accept run-one \
  --dataset acceptance-evals/pilot/questions.pilot-q05.jsonl \
  --question Q05 \
  --run acceptance-evals/runs/20261006-pilot-q05 \
  --mode fast --nocache --silence-timeout 120 --deadline 300 --pilot
uv run cited-research-accept prepare-review --run acceptance-evals/runs/20261006-pilot-q05
uv run cited-research-accept summarize --run acceptance-evals/runs/20261006-pilot-q05
```

Only `run-one` calls the API, once per invocation, with SDK retries off. 19 of the 20 candidate
questions are still `pending`, so only the one-question pilot set is frozen and runnable. One
live pilot ran on 2026-10-06 (`pilot_only=true`); `acceptance-evals/examples/synthetic-run/` is a
synthetic offline-fixture run for the summary, not API output.

## What one run proves

That the documented path worked, once, in the recorded environment, on the recorded date. Not
typical latency, not cost, not accuracy, not reliability, not anything relative to another tool.
See `handoff/BUILD-FINDINGS.md` for what we actually observed, including the parts that did not
match the docs.

## Data flow

Your question goes to Tabstack, and Tabstack fetches public pages to answer it. The
[Tabstack Privacy Notice](https://tabstack.ai/legal/privacy) (last updated 2026-09-16) says
inputs are run through LLMs offered by third parties, and that history, including your content
and outputs, is stored for 90 days unless you delete it. Use public, non-sensitive questions with
this example. For anything private, get engineering and legal review before calling the hosted
API.

## License

MIT.
