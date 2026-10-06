# Request lifecycle trace

Generated from `events.sanitized.jsonl`. Times are local monotonic milliseconds since
just before the request was sent. This is a **request lifecycle**, not a source-by-source
execution trace: progress events say what phase the service reports, not which page it
read. Only the `complete` event's `cited_pages` identifies sources.

```mermaid
sequenceDiagram
    participant C as cited-research CLI
    participant T as Tabstack /research
    participant V as Citation reviewer
    C->>T: one POST /research (SSE)
    T-->>C: 645 ms start: Starting research
    T-->>C: 645 ms planning:start: Planning research strategy
    T-->>C: 1978 ms planning:end: Planning complete: 5 queries
    T-->>C: 1978 ms iteration:start (iteration 1): Starting iteration 1 of 1
    T-->>C: 1978 ms searching:start (iteration 1): Searching with 5 queries
    T-->>C: 4230 ms searching:end (iteration 1): Found 6 URLs
    T-->>C: 4230 ms iteration:end (iteration 1): Iteration 1 complete (fast mode)
    T-->>C: 4230 ms writing:start: Writing report
    T-->>C: 8879 ms writing:end: Report draft complete
    T-->>C: 8879 ms complete: report 639 chars, 3 cited pages
    C->>V: report.md + sources.json + review-sheet.csv
    V-->>C: claim-to-source judgments (manual, not generated)
```

## What this trace shows and does not show

| Observed (from the stream) | Not observable here |
|---|---|
| Event names, arrival order, local elapsed time | Which URL was fetched when |
| Allowlisted counters (iteration, urls_new, ...) | Model prompts or internal reasoning |
| Terminal state and report length | Retrieval passages behind each sentence |
| Cited pages in returned order | Whether a cited page supports a sentence |

Terminal state: `complete`. Review state: `review_needed`.

## Cited pages (returned order)

Inline markers `[n]` in the report are joined to position `n` below. The SDK
documents `cited_pages` as ordered by first citation appearance; the public guide
does not say. The reviewer confirms the join.

| n | id | link | notes |
|---|---|---|---|
| 1 | `WMjKTb` | [Web search](https://docs.ollama.com/capabilities/web-search) | claims: [] |
| 2 | `Xv76iu` | [Web search](https://docs.ollama.com/capabilities/web-search.md) | likely same page as 1; claims: [] |
| 3 | `dFZ3-x` | [Testing Ollama Web Search 🔍 and a Thinking Model 💭](https://dev.to/aairom/testing-ollama-web-search-and-a-thinking-model-1dh7) | claims: [] |
