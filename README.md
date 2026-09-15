# cited-research

One Tabstack `/research` call from Python: streamed progress on the terminal, a final Markdown
report, and a machine-readable list of the sources the report cites. Plus a pre-registered
evaluation harness for comparing that managed call against a search + fetch + model pipeline you
run yourself.

Built for people running their own model inside a product, assistant, agent, or internal
workflow that needs a current, cited answer from the public web.

## What you get

```text
artifacts/sample-run/
├── question.txt              the exact question asked
├── command.txt               the exact command that ran
├── events.sanitized.jsonl    every SSE event name, with allowlisted metadata only
├── report.md                 the final Markdown report
├── sources.json              cited pages: id, url, title, claims, source_queries
├── run-manifest.json         mode, cache setting, timings, event counts, versions, commit
├── stdout.txt / stderr.txt   what the terminal showed
└── terminal.png              the same, rendered
```

## Prerequisites

- Python 3.9 or later (tested on 3.12.13)
- [uv](https://docs.astral.sh/uv/) (tested with 0.11.6)
- A Tabstack API key from <https://console.tabstack.ai>, exported as `TABSTACK_API_KEY`

The CLI reads the key from the environment only. There is no `--api-key` flag and the key is
never written to any output file.

## Run it

```bash
git clone <this repo> && cd tabstack-cited-research-python
uv sync --frozen
export TABSTACK_API_KEY=...

uv run cited-research \
  --query "What are the current ways to add web search to an Ollama-based application, and what output does each approach return?" \
  --mode fast \
  --nocache \
  --output artifacts/my-run
```

Flags: `--mode fast|balanced` (default `fast`; `balanced` needs a paid plan), `--nocache` to
bypass the content cache, `--fetch-timeout SECONDS`, `--quiet`.

Expected terminal output (from the committed sample run, 2026-09-15):

```text
start  Starting research
planning:start  Planning research strategy
planning:end  Planning complete: 6 queries
iteration:start  iteration 1/1  Starting iteration 1 of 1
searching:start  iteration 1  Searching with 6 queries
searching:end  iteration 1  8 new urls  Found 8 URLs
iteration:end  iteration 1  Iteration 1 complete (fast mode)
writing:start  Writing report
writing:end  Report draft complete
complete  report -> artifacts/sample-run/report.md
sources (7) -> artifacts/sample-run/sources.json
  - Web search  https://docs.ollama.com/capabilities/web-search.md
  ...
```

## How it works

```text
your app ──POST /research (SSE)──▶ Tabstack
   ▲                                  │ start, planning:*, iteration:*, searching:*, writing:*
   │                                  ▼
   └── report.md + sources.json ◀── complete { report, metadata.citedPages, ... }
```

`src/cited_research/tabstack_runner.py` opens the client as a context manager, iterates the
stream, prints one line per progress event, and stops at `complete`. `error` raises and exits
non-zero. A stream that closes without `complete` also exits non-zero. There is no `done` event
on `/research`; `complete` (or `error`) is the last thing you receive.

## Exit codes

| Code | Meaning | Where it is decided |
|---|---|---|
| 0 | `complete` received, files written | |
| 2 | Task-level failure: an `error` event, or the stream ended without `complete` | inside the stream |
| 3 | HTTP rejection before the stream opened (401, 400, 429, ...) | SDK raises `APIStatusError` |
| 4 | Connection or transport failure | SDK raises `APIConnectionError` |
| 5 | `TABSTACK_API_KEY` not set; no request is made | CLI |

Observed on 2026-09-15 with a fake key: `request rejected (HTTP 401): Unauthorized - Invalid token`,
exit 3, 195 ms. The `error`-event path is covered by a synthetic fixture test only; we did not try
to make production fail.

## Timeouts, retries, cleanup

- The SDK applies a 600 s per-request timeout to `/research` streams. This CLI adds no shorter
  total timeout, so a healthy long run is not killed early.
- The SDK retries transport-level failures (408, 409, 429, 5xx) twice by default. This CLI adds
  **no** application-level retries; a retry could re-run and re-bill the research. The manifest
  records both (`sdk_max_retries`, `application_retries`).
- The client is a context manager; the HTTP response is closed when the loop exits, including on
  error.

## What is in `sources.json`, and what is not

Kept per cited page: `id`, `url`, `title`, `claims`, `source_queries`, `relevance`,
`reliability`. Dropped on purpose: `full_text`, `summary`, `depth`, `parent_url`, `url_source`.
The event log keeps event names, timestamps, iteration counters, and short status messages only.
It never duplicates the report or page text, and a denylist strips anything that looks like a
key, header, cookie, stack trace, or environment dump.

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
