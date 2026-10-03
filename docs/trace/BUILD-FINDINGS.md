# Build findings: trace a question from request to source-backed answer

Two live runs of one question. Nothing here measures typical latency, cost, accuracy, variance,
or reliability.

## Identity and status

- Repo https://github.com/Mozilla-Ocho/tabstack-cited-research-python, branch `trace-mode` off
  `main@6325338`, pushed; [PR #1](https://github.com/Mozilla-Ocho/tabstack-cited-research-python/pull/1) open against `main`, not merged. Implementation: `7746971` (`src/`,
  `tests/`, `pyproject.toml` and `uv.lock` are unchanged from there to the branch tip). Worked-
  example run at `7fe5f20` (its manifest says `82b9be9`, the same tree before a message rewrite);
  reproduction run at `98be5ca`. `tabstack==2.8.5` (lockfile unchanged), uv 0.11.28,
  macOS 25.5.0 arm64. Offline tests pass on Python 3.12.13 and 3.9.
- The original CLI is built here; no application layer exists (no `ApplicationResponse` anywhere), so the trace
  runs on the original CLI. The review sheet follows the article ("What makes a
  citation useful", supplied in chat 2026-09-29): its nine rubric fields in order, with the CLI
  filling `claim_id`, `answer_text`, `citation_ids`, `source_url`, and a person filling the rest.

## Design

Single request and event loop; SDK retries off (`max_retries=0`) and no application retries.
Exits: 2 streamed error, 3 HTTP, 4 transport, 6 closed before terminal, 7 silence timeout,
8 second terminal event, 9 transport failure mid-stream, 11 unexpected failure (9 and 11 added
after review of PR #1). Timeline: `seq`, local monotonic `elapsed_ms`, `timestamp_raw` and
`timestamp_type`, allowlisted counters, redacted messages; written atomically. Cited pages in
returned order with link checks, duplicate flags (scheme, slash, `.md` variants), stripped
credentials; `[]` becomes `review_needed_no_sources`. Rubric-shaped review sheet with `support`
always blank; `cited-research-review DIR` rebuilds it offline. Per-run diagram. The original path
used by the frozen evaluation harness is unchanged. 62 offline tests pass
(`docs/trace/TEST-OUTPUT.txt`), also from a fresh `uv sync --frozen` clone with no key.

## Observed: worked-example run, 2026-09-29 18:23:24Z

- `complete`, exit 0, 17,091 ms; first event 496 ms; the same 10-event sequence as the first sample run, each
  once; the stream closed by itself after `complete`. Gaps by arrival: planning ~1.1 s,
  searching ~5.3 s, writing ~10.2 s (client-side, not server stage timings). Events arrive in
  bursts that share a timestamp; order comes from `seq`.
- `timestamp`: a float in epoch ms on every event. 3 cited pages, `claims: []` on all (15 of 15
  pages across the three fast-mode runs so far). All pages share the same six `source_queries`.
  Positions 1 and 2 are the same doc with and without `.md`; the rebuilt sheet flags C04's
  `[1][2]` as one page, the same pattern found in the first sample run.
- 5 of 10 report sentences have no inline marker, including the endpoint, the API-key
  requirement, and `max_results` limits. Noticed during inspection (not a review): "MCP
  (Multi-Context Processor)", uncited.
- This run predates later changes: its log field is `timestamp` (now `timestamp_raw`), its
  manifest shows `sdk_max_retries: 2` (now 0), and its diagram carries the older join caveat.
  `review-sheet.csv` was regenerated offline in rubric form from the run's own `report.md` and
  `sources.json`. One request was sent; whether the SDK retried internally was not observable.
- First-pass claim review, sign-off pending (`artifacts/trace-run/REVIEW-NOTES.md`):
  13 rows, 5 scored `2`, 8 scored `1`, none `0` or `U`. Core facts were all on a returned page.
  The `1`s are qualifiers the report dropped or added: "snippet" becomes page content (C04),
  unstated MCP output format (C07), "Multi-Context Processor" (C06), "specific" models when the
  post says any cloud model (C08). The output half of the question is the weakly supported part.
- Cost not measured.

## Observed: fresh-clone reproduction, 2026-09-29 18:58:36Z

Clone, `uv sync --frozen`, 62 tests with no key, then the README command
(`artifacts/trace-repro/`). `complete`, exit 0, 12,163 ms, the same 10 events, 5 cited pages
(`claims: []`; `.md` duplicate flagged), 1 of 7 sentences uncited, `sdk_max_retries: 0`,
`timestamp_raw` float, `iteration:end` with `is_last: true` and `stop_reason: max_iterations`.
Different report and sources from the first run. Same machine, not a second engineer. Not
reviewed.

## Checks

Technical-review checklist results: `TECHNICAL-REVIEW.md` (mapping, docs, safe sharing,
diagrams; sign-off blank). The detect-secrets scan found only false positives, and the key is
absent from all history. Editorial items (CTA, the trust line versus the Privacy Notice, the
current-answers page): `EDITORIAL-NOTES.md`.

## Docs correspondence

Checked word for word against the raw HTML of the [guide](https://docs.tabstack.ai/guides/research)
(sha256 `da3615b8…`) and the [API reference](https://docs.tabstack.ai/api/resources/agent/methods/research)
(sha256 `8788f2b0…`), fetched 2026-09-29T18:57:44Z. Quotes are exact.

| Topic | Guide | API reference | Observed / SDK 2.8.5 |
|---|---|---|---|
| `timestamp` | "ISO-8601 string for when the event was emitted" | `timestamp : number` | float, epoch ms; SDK `float`. **Guide wrong** |
| `claims` | "the specific statements drawn from that page" | `claims : array of string` | `[]` on 3 of 3 pages (15 of 15 across fast runs) |
| Cited-page order | not stated | "ordered by first citation appearance" | same text in the SDK docstring; markers matched it |
| `reliability` | optional, "absent for this source" in the example | optional `"low"`, `"medium"` or `"high"` | absent in fast mode |
| Terminal event | "complete fires once, at the end" | no mention of `done` | no `done`; stream closed after `complete` |
| Total timeout | "There is no server-side timeout on the request as a whole" | not stated | SDK sets `httpx.Timeout(600)` client-side: a 600 s silence timeout between events (connect/read/write/pool), not a total cap |
| Client timeout | "Watch for stream silence instead" | not stated | `--silence-timeout` implements this |
| `iteration:end` | "Adds isLast and an optional stopReason" | `isLast : boolean`, `stopReason` | now allowlisted |
| `query` length | not stated | "Maximum 10,000 characters" | not validated client-side; over-limit behavior not tested |

## Not done

Technical-reviewer sign-off (`TECHNICAL-REVIEW.md`). A reproduction by a second engineer (this one
used the same machine). Coverage scoring (required elements were not frozen before the output was
read). Editorial decisions in `EDITORIAL-NOTES.md`. Merge of PR #1.
