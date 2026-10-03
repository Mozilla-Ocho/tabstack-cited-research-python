# Technical review checklist

The spec asks a technical reviewer to check the report/source mapping, docs correspondence, safe
sharing, and the article diagram. Each check below has been run and its result recorded. The
sign-off at the bottom is still blank.

## 1. Report and source mapping

| Check | Result |
|---|---|
| `[n]` joins to `cited_pages[n-1]` | Consistent with the API reference and SDK ("ordered by first citation appearance"). In both live runs, markers first appear in ascending order. |
| Every marker resolves to a cited page | Yes in both runs. There are no out-of-range markers. |
| Claim-level review of the worked example | `artifacts/trace-run/review-sheet.csv`: 13 rows, 5 scored `2`, 8 scored `1`. Passages are verbatim and checked by script. Method and page hashes are in `artifacts/trace-run/REVIEW-NOTES.md`. |
| Duplicate sources flagged | The `.md` variant is flagged in both runs: C04 in the first, C06 in the reproduction. |
| Reproduction run reviewed | No. It is intentionally left as generated. |

## 2. Docs correspondence

Checked word for word against the raw guide and API reference HTML (2026-09-29T18:57:44Z). The
table is in `BUILD-FINDINGS.md`. The one hard conflict: the guide's ISO-8601 `timestamp` versus
the reference, the SDK, and the wire, which all say number. `claims` is described as populated
but was empty on every fast-mode page. An earlier summarized reading got two rows wrong (`done`
event, cited-page order); both were corrected from the raw text.

## 3. Safe sharing

| Check | Result |
|---|---|
| API key value | Absent from the working tree and from every commit on every branch (`git log --all -p`). |
| detect-secrets (Yelp) over all tracked and new files | 18 hits, all false positives: query SHA-256s, git and config hashes, and deliberately fake keys and a `user:pw` URL in test fixtures. |
| Headers, cookies, bearer tokens, stack traces in trace artifacts | None. |
| Local paths, usernames, emails in trace artifacts | None. |
| URLs rendered as links | All public http(s); `rejected_link_count` is 0 in both runs. |
| Third-party text | Only short passages in the review sheet. Page text is not stored (hashes only). |
| Data-flow statement | The README now matches the Privacy Notice of 2026-09-16: third-party models, 90-day history. |
| Question | Public and non-sensitive. The manifest stores only its hash; `question.txt` holds the text. |

## 4. Diagrams

Both `trace-diagram.md` files render with `@mermaid-js/mermaid-cli` as sequence diagrams, with
every event label present. Each diagram marks the request lifecycle as observed and states that
it does not show source-level work. The spec's contract diagram (User, CLI, Tabstack, Reviewer)
matches the generated one, with the reviewer step drawn as manual.

## Remaining limits

- One question, two runs. Nothing here measures citation accuracy, latency, or variance.
- Coverage (required elements) is not scored. The elements were not frozen before the output was
  read.
- Python 3.12 and 3.9 were tested on macOS arm64 only.

## Sign-off

Reviewed by: ______________________  Date: ____________

Changes requested / notes:
