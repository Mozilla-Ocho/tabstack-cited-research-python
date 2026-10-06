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
    T-->>C: 0 ms searching:start (iteration 1): Searching
    Note over C: terminal state premature_close. No report
```

## What this trace shows and does not show

| Observed (from the stream) | Not observable here |
|---|---|
| Event names, arrival order, local elapsed time | Which URL was fetched when |
| Allowlisted counters (iteration, urls_new, ...) | Model prompts or internal reasoning |
| Terminal state and report length | Retrieval passages behind each sentence |
| Cited pages in returned order | Whether a cited page supports a sentence |

Terminal state: `premature_close`. Review state: `not_applicable`.
