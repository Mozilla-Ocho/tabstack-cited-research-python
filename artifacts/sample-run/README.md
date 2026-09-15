# Sample run, 2026-09-15

One production `/research` call, `mode=fast`, `nocache=true`, made from the committed CLI at
commit `759f10a6f41e2a3ebeaaeab588daaa535ed56346`. Nothing here is edited except sanitization
described in the repository README.

| Fact | Value |
|---|---|
| Question | see `question.txt` |
| Command | see `command.txt` |
| Started (UTC) | 2026-09-15T19:00:04.556Z |
| Completed (UTC) | 2026-09-15T19:00:24.874Z |
| Duration | 20,299 ms (first event at 1,064 ms) |
| Terminal status | `complete`, exit 0 |
| Events, in order | start, planning:start, planning:end, iteration:start, searching:start, searching:end, iteration:end, writing:start, writing:end, complete (10 events, each once) |
| Pages analyzed | 7 |
| Cited pages | 7 |
| Report length | 1,649 characters, one paragraph, inline [n] citations |
| Python | 3.12.13, Darwin 25.6.0, arm64 |
| tabstack | 2.8.5 (locked in `uv.lock`) |
| Cost | no credit amount available. Tabstack internal telemetry records `action_count = 1` for this call (fast mode); the public rate is 250 credits per fast Research action, so 250 is the implied but unreceipted figure |

Things worth noticing in `sources.json`: every `claims` array is empty; three of the seven URLs
are the same Ollama documentation page in different forms; two sources are web-scraper tutorials
rather than web-search integrations. See `handoff/BUILD-FINDINGS.md`.
