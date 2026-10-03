# Fresh-clone reproduction run, 2026-09-29

A second live run, made from a fresh clone of `trace-mode` at `98be5ca`. The same machine was
used, but the run followed the README from scratch: `git clone`, `uv sync --frozen`, the offline
tests with no API key (62 passed at the time), then the documented command. This is evidence that the
documented path reproduces. It is **not** the article's worked example, and it has **not** been
reviewed; the review sheet is as generated.

| | |
|---|---|
| Commit | `98be5caf000686ce0c9c7c418ffa941ba44d59e8` |
| Started (UTC) | 2026-09-29T18:58:36Z |
| Terminal status | `complete`, exit 0 |
| Local duration | 12,163 ms (first event 412 ms) |
| Events | the same 10 as the first run, each once |
| Cited pages | 5 (`claims: []` on all); positions 1 and 4 are the same docs page (`.md` variant) |
| Report | 1,522 chars, 7 candidate claims, 1 without an inline citation |
| SDK retries | 0 |

Unlike the first run, this one was written by the current code. The timeline has
`timestamp_raw`, and `iteration:end` carries `is_last: true` and `stop_reason: max_iterations`.

The same question returned a different report and source set from the first run, 35 minutes
earlier. One pair of runs says nothing about how much answers vary.
