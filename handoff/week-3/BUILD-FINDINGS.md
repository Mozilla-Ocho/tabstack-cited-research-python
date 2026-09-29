# Week 3 Build findings: trace a question from request to source-backed answer

One live run. Nothing here measures typical latency, cost, accuracy, or reliability.

## Identity and status

- Repo https://github.com/Mozilla-Ocho/tabstack-cited-research-python, branch `week-3-trace` off
  `main@6325338`, **not pushed**. Live run at `82b9be9`. `tabstack==2.8.5` (lockfile unchanged),
  Python 3.12.13, uv 0.11.28, macOS 25.5.0 arm64.
- Week 1: built here. Week 2: **not built** (no `ApplicationResponse` anywhere), so the trace
  runs on the Week 1 CLI. The review sheet follows the Week 3 Understand draft ("What makes a
  citation useful", supplied in chat 2026-09-29): its nine rubric fields in order, with the CLI
  filling `claim_id`, `answer_text`, `citation_ids`, `source_url`, and a person filling the rest.

## Design

Single request and event loop; SDK retries off (`max_retries=0`) and no application retries.
Exits: 2 streamed error, 3 HTTP, 4 transport, 6 closed before terminal, 7 silence timeout,
8 second terminal event. Timeline: `seq`, local monotonic `elapsed_ms`, `timestamp_raw` and
`timestamp_type`, allowlisted counters, redacted messages; written atomically. Cited pages in
returned order with link checks, duplicate flags (scheme, slash, `.md` variants), stripped
credentials; `[]` becomes `review_needed_no_sources`. Rubric-shaped review sheet with `support`
always blank; `cited-research-review DIR` rebuilds it offline. Per-run diagram. The Week 1 path
used by the frozen Prove harness is unchanged. 62 offline tests pass
(`handoff/week-3/TEST-OUTPUT.txt`), also from a fresh `uv sync --frozen` clone with no key.

## Observed: live run, 2026-09-29 18:23:24Z

- `complete`, exit 0, 17,091 ms; first event 496 ms; the Week 1 sequence of 10 events, each
  once; the stream closed by itself after `complete`. Gaps by arrival: planning ~1.1 s,
  searching ~5.3 s, writing ~10.2 s (client-side, not server stage timings). Events arrive in
  bursts that share a timestamp; order comes from `seq`.
- `timestamp`: a float in epoch ms on every event. 3 cited pages, `claims: []` on all (15 of 15
  pages across the three fast-mode runs so far). All pages share the same six `source_queries`.
  Positions 1 and 2 are the same doc with and without `.md`; the rebuilt sheet flags C04's
  `[1][2]` as one page, the same pattern the Understand post found in the Week 1 run.
- 5 of 10 report sentences have no inline marker, including the endpoint, the API-key
  requirement, and `max_results` limits. Noticed during inspection (not a review): "MCP
  (Multi-Context Processor)", uncited.
- This run predates later changes: its log field is `timestamp` (now `timestamp_raw`), its
  manifest shows `sdk_max_retries: 2` (now 0), and its diagram carries the older join caveat.
  `review-sheet.csv` was regenerated offline in rubric form from the run's own `report.md` and
  `sources.json`. One request was sent; whether the SDK retried internally was not observable.
- First-pass claim review (Claude, not the technical reviewer; `artifacts/week-3-trace/REVIEW-NOTES.md`):
  13 rows, 5 scored `2`, 8 scored `1`, none `0` or `U`. Core facts were all on a returned page.
  The `1`s are qualifiers the report dropped or added: "snippet" becomes page content (C04),
  unstated MCP output format (C07), "Multi-Context Processor" (C06), "specific" models when the
  post says any cloud model (C08). The output half of the question is the weakly supported part.
- Grep-based secrets scan clean (no gitleaks). Cost not measured.

## Docs correspondence (guide and API reference, read 2026-09-29)

| Topic | Guide | API reference | Observed / SDK |
|---|---|---|---|
| `timestamp` | ISO-8601 string | number (ms) | float ms; SDK `float`. **Guide wrong** |
| `claims` | "specific statements drawn from that page" | strings extracted from page | `[]` in fast mode, 3 of 3 |
| `reliability` | string | low/medium/high | absent in fast mode |
| Event names | lifecycle plus balanced-only list | 22 names plus `error` | matches the SDK union; fast sent 10 |
| `done` event | none | none | none |
| Total timeout | none server-side; watch silence | not stated | SDK sets 600 s client-side |
| `iteration:end` | `isLast`, `stopReason` | in SDK types | now allowlisted |
| `citedPages` order | not stated | not stated | SDK: "ordered by first citation appearance" |

The pages were read through a fetch-and-summarize tool; recheck the wording against the live
pages before quoting.

## Not done

Human technical-reviewer check of the first-pass scores and sign-off; coverage (required elements over required elements, per
the rubric) is not generated; second-engineer live reproduction; Python 3.9; content and
security review before pushing (`report.md` and `sources.json` are committed on the branch, and
pushing to the public repo would publish them).
