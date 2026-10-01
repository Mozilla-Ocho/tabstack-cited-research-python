# Trace run, 2026-09-29

One live `fast`, `nocache` call through the trace path. The files here are what the CLI wrote,
plus `stdout.txt`, `stderr.txt`, and `exit-code.txt` captured from the shell.

| | |
|---|---|
| Commit | `7fe5f20ce49ba26dd63f2795bef5a0733267f644` (the manifest records `82b9be9`; see below) |
| Started (UTC) | 2026-09-29T18:23:24.612Z |
| Terminal status | `complete`, exit 0 |
| Local duration | 17,091 ms (first event 496 ms) |
| Events | 10, each once |
| Cited pages | 3 (`claims: []` on all) |
| Review state | first-pass review in `review-sheet.csv`; see `REVIEW-NOTES.md`. Technical-reviewer sign-off pending |

`report.md` has had a first-pass claim review (5 supported, 8 partial of 13 rows) but no
technical-reviewer sign-off. Do not quote the report as correct. Read
`trace-diagram.md` for what the timeline can and cannot show, and
`../../docs/trace/BUILD-FINDINGS.md` for observations.

Written by commit `82b9be9`. That commit's message was later reworded, which gave it the new
hash `7fe5f20`. The code is identical (same tree, `e68ccc7`). Two things changed afterwards: the event-log field `timestamp` is
now `timestamp_raw`, and SDK retries are now off (this manifest shows `sdk_max_retries: 2`).
The files are left as the run produced them, except `review-sheet.csv`, which was rebuilt
offline in the rubric format with `uv run cited-research-review artifacts/trace-run`.
