# Week 3 trace run, 2026-09-29

One live `fast`, `nocache` call through the trace path. The files here are what the CLI wrote,
plus `stdout.txt`, `stderr.txt`, and `exit-code.txt` captured from the shell.

| | |
|---|---|
| Commit | `82b9be98d4add677d1fe650b1a0701d4d198189a` |
| Started (UTC) | 2026-09-29T18:23:24.612Z |
| Terminal status | `complete`, exit 0 |
| Local duration | 17,091 ms (first event 496 ms) |
| Events | 10, each once |
| Cited pages | 3 (`claims: []` on all) |
| Review state | `review_needed`, no judgments recorded |

`report.md` and `sources.json` are unreviewed. Do not quote the report as correct. Read
`trace-diagram.md` for what the timeline can and cannot show, and
`../../handoff/week-3/BUILD-FINDINGS.md` for observations.

Written by commit `82b9be9`. Two things changed afterwards: the event-log field `timestamp` is
now `timestamp_raw`, and SDK retries are now off (this manifest shows `sdk_max_retries: 2`).
The files are left as the run produced them, except `review-sheet.csv`, which was rebuilt
offline in the rubric format with `uv run cited-research-review artifacts/week-3-trace`.
