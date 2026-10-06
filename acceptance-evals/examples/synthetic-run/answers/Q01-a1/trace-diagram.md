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
    T-->>C: 0 ms planning:end: Planning complete: 2 queries
    T-->>C: 0 ms writing:start: Writing report
    T-->>C: 0 ms complete: report 170 chars, 3 cited pages
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
| 1 | `z1` | [Zeta](https://zeta.example/one) | claims: [] |
| 2 | `a2` | [Alpha](https://alpha.example/two) | claims: [] |
| 3 | `m3` | [Mid](https://mid.example/three) | claims: [] |
