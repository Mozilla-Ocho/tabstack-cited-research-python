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
    T-->>C: 0 ms start: Starting research
    T-->>C: 0 ms planning:start: Planning
    T-->>C: 0 ms planning:end: Plan ready
    T-->>C: 0 ms iteration:start (iteration 1): Iteration 1
    T-->>C: 0 ms searching:start (iteration 1): Searching
    T-->>C: 0 ms searching:end (iteration 1): Found pages
    T-->>C: 0 ms iteration:end (iteration 1): Iteration done
    T-->>C: 0 ms writing:start: Writing
    T-->>C: 0 ms writing:end: Written
    T-->>C: 0 ms complete: report 33 chars, 1 cited pages
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
| 1 | `pg_1` | [Example doc](https://example.org/doc) |  |
