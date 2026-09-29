# cited-research

One Tabstack `/research` call from Python: streamed progress on the terminal, a final Markdown
report, and a machine-readable list of the sources the report cites. Plus a pre-registered
evaluation harness for comparing that managed call against a search + fetch + model pipeline you
run yourself.

Built for people running their own model inside a product, assistant, agent, or internal
workflow that needs a current, cited answer from the public web.

## What you get

```text
artifacts/week-3-trace/
├── question.txt              the exact question asked
├── command.txt               the exact command that ran (no secrets; the CLI takes none)
├── events.sanitized.jsonl    lifecycle timeline: event, seq, elapsed_ms, allowlisted fields
├── report.md                 the final Markdown report
├── sources.json              cited pages in returned order, with link checks
├── review-sheet.csv          report sentences joined to cited pages; judgments left blank
├── trace-diagram.md          the timeline as a Mermaid sequence diagram, plus its limits
├── run-manifest.json         mode, timings, terminal status, review state, caveats, versions
└── stdout.txt / stderr.txt   what the terminal showed
```

`artifacts/sample-run/` is the Week 1 run (2026-09-15, schema 1, before the trace files existed).
`artifacts/week-3-trace/` is the trace run (2026-09-29, schema 2). See
[`handoff/week-3/BUILD-FINDINGS.md`](handoff/week-3/BUILD-FINDINGS.md).

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
complete  report -> artifacts/week-3-trace/report.md
sources (3) -> artifacts/week-3-trace/sources.json
  1. Web search  https://docs.ollama.com/capabilities/web-search
  2. Web search  https://docs.ollama.com/capabilities/web-search.md
  3. Subagents and web search in Claude Code  https://ollama.com/blog/web-search-subagents-claude-code
review sheet -> artifacts/week-3-trace/review-sheet.csv (unreviewed)
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
| 4 | Connection or transport failure | SDK raises `APIConnectionError` |
| 5 | `TABSTACK_API_KEY` not set; no request is made | CLI |
| 6 | Stream closed before `complete` or `error` | inside the stream |
| 7 | `--silence-timeout` elapsed with no event; the request may still run and bill | CLI |
| 8 | Protocol error: a second terminal event after `complete`, or `complete` without a report | inside the stream |

Changed in schema 2: a stream that closes early exits 6 (it was 2). Exits 7 and 8 are new.
`run-manifest.json` records the same state as `terminal_status`.

Observed on 2026-09-15 with a fake key: `request rejected (HTTP 401): Unauthorized - Invalid token`,
exit 3, 195 ms. The `error`-event path is covered by a synthetic fixture test only; we did not try
to make production fail.

## Timeouts, retries, cleanup

- The SDK applies a 600 s per-request timeout to `/research` streams. This CLI adds no shorter
  total timeout, so a healthy long run is not killed early. `--silence-timeout` is optional and
  measures the gap between events (including the wait for the first one), not total duration.
- The SDK retries transport-level failures (408, 409, 429, 5xx) twice by default. This CLI adds
  **no** application-level retries; a retry could re-run and re-bill the research. The manifest
  records both (`sdk_max_retries`, `application_retries`).
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
server `timestamp` with its observed type (`timestamp_type`), allowlisted counters, and status
messages with URLs, emails, and credential-shaped strings redacted. It never duplicates the
report or page text, and a denylist strips anything that looks like a key, header, cookie, stack
trace, or environment dump.

## Reviewing citations

`review-sheet.csv` has one row per (report sentence, inline marker). Sentences without a marker
get a row with `notes = no inline citation`. `[n]` is joined to the cited page at position `n`;
the API does not state this join, so the reviewer checks it. `supporting_passage`, `judgment`
(`supported`, `partially_supported`, `not_supported`, `source_unavailable`), `reviewer`, and
`reviewed_at_utc` are always blank when generated. The columns are provisional until checked
against the Understand-lane rubric. The API's own `claims` list is shown for context and is
not verification; in fast mode it has been empty on every page so far.

## Evaluation harness

See [`evals/README.md`](evals/README.md) and [`evals/PROTOCOL.md`](evals/PROTOCOL.md). The
protocol was committed before any comparison output was inspected. Only the Q01 harness pilot has
run; `FULL_EVALUATION_RUN=false`.

## What one run proves

That the documented path worked, once, in the recorded environment, on the recorded date. Not
typical latency, not cost, not accuracy, not reliability, not anything relative to another tool.
See `handoff/BUILD-FINDINGS.md` for what we actually observed, including the parts that did not
match the docs.

## Data flow

The question and the retrieved page content are processed by Tabstack and, for research, by
third-party models under Tabstack's contracts. Read the
[Tabstack Privacy Notice](https://tabstack.ai/legal/privacy) before sending confidential input.

## License

MIT.
