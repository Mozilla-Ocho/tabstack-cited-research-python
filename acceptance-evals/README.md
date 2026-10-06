# Workflow acceptance kit

Define a small, versioned question set for one current-answer workflow, run one question
through Tabstack `/research`, review the answer's coverage and its claims separately, and
summarize without turning missing scores or missing usage into zero.

This is separate from [`../evals/`](../evals/), the comparative protocol and its historical
pilot, which is unchanged. Nothing here is a benchmark or a provider comparison.

## Commands

```bash
uv sync --frozen
export TABSTACK_API_KEY=...   # read from the environment only; there is no key flag

# 1. Validate offline. The candidate set is valid structure but not runnable: 19 rows pending.
uv run cited-research-accept validate --dataset acceptance-evals/questions.candidate.jsonl

# 2. The committed pilot set is already frozen (pilot-q05-v1); freeze refuses to replace a
#    freeze record. To run it yourself, copy it to a new file and freeze that copy.
cp acceptance-evals/pilot/questions.pilot-q05.jsonl acceptance-evals/pilot/questions.my-pilot.jsonl
uv run cited-research-accept freeze \
  --dataset acceptance-evals/pilot/questions.my-pilot.jsonl \
  --version my-pilot-v1 \
  --allow-subset

# 3. The only command that calls the API: one frozen question, one request, no retries.
#    Use a new run directory; the committed pilot run already holds attempt Q05-a1.
uv run cited-research-accept run-one \
  --dataset acceptance-evals/pilot/questions.my-pilot.jsonl \
  --question Q05 \
  --run acceptance-evals/runs/my-pilot-q05 \
  --mode fast \
  --nocache \
  --silence-timeout 120 \
  --deadline 300 \
  --pilot

# 4. Blank review sheets (re-running appends rows for new attempts and leaves existing rows
#    byte for byte; --force discards them). Shown on the committed, already-reviewed pilot run;
#    use your own --run directory after step 3.
uv run cited-research-accept prepare-review --run acceptance-evals/runs/20261006-pilot-q05

# 5. Summarize (offline). Writes summary.json; --json prints it.
uv run cited-research-accept summarize --run acceptance-evals/runs/20261006-pilot-q05
```

`run-one` refuses, before any request, when the dataset has no freeze record, its sha256 no
longer matches the freeze, any row is `pending`, evidence is older than the freeze's
`max_evidence_age_days` (default 30), the run directory was started with a different
configuration or a different implementation, or the question already has an attempt in the
run. The implementation is identified by `implementation_sha256`, a hash of the content of the
package files, `pyproject.toml`, and `uv.lock` as they are on disk, so a commit that touches only
docs or run artifacts does not change it, while any uncommitted code edit does. Each attempt
also records the HEAD commit and a dirty flag for provenance (null if git is unavailable). Parallel
`run-one` processes on one run directory take turns on an OS file lock (`.attempts.lock`) for
every ledger update, so no attempt record is lost; on Windows a held lock is waited on for at
most 60 s. `--silence-timeout` and `--deadline` must be finite numbers of seconds greater than 0;
`--fetch-timeout` must be a whole number of seconds greater than 0. `--another-attempt` records
a new, separate attempt (`Q05-a2`); nothing is ever reused or overwritten.

### Evidence expiry

The Q05 reference passage was retrieved 2026-10-06T17:37:54Z, and freeze records allow evidence
up to 30 days old. After 2026-11-05T17:37:54Z, `run-one` on the committed pilot set, or on a copy
of it, refuses with "evidence is older than 30 days", and `freeze` refuses the copy too. To run
it after that date: re-open the reference page, re-review the passage (update `passage`,
`retrieved_at_utc`, and `date_or_version` if anything changed), save the result as a new dataset
file with a new version label, and freeze that file. Do not edit a frozen file in place: its
sha256 no longer matches its freeze record, and `run-one`, `prepare-review`, and `summarize`
all refuse it.

## Files

| Path | What it is |
|---|---|
| `dataset-schema.json` | The question-record contract (JSON Schema 2020-12), as supplied |
| `questions.candidate.jsonl` | 20 public-documentation questions, five categories of four. Only Q05 is `reviewed`; the other 19 are `pending` and their reference URLs have not been fetched |
| `evidence/Q05-ollama-web-search.txt` | The Q05 passage as re-fetched 2026-10-06T17:37:54Z, with response metadata |
| `pilot/questions.pilot-q05.jsonl` | Q05 alone, byte-identical to its candidate line |
| `pilot/questions.pilot-q05.jsonl.freeze.json` | Version `pilot-q05-v1`, sha256, output instruction |
| `*-template.csv` | Coverage, claim, attempt-ledger, and release-review (seven gates) headers |
| `runs/20261006-pilot-q05/` | The one live pilot (`pilot_only=true`), reviewed |
| `examples/synthetic-run/` | **Synthetic** offline-fixture run for the summary demo; not API output |
| `handoff/` | Build evidence: commands, test receipt, pilot terminal output, failure paths |

## Run directory

```text
runs/<run-id>/
  manifest.json                    dataset version + sha256, config, commit, versions, provenance
  attempts.jsonl                   one record per attempt, every terminal state
  answers/<attempt-id>/report.md
  answers/<attempt-id>/sources.json            cited pages in returned order
  answers/<attempt-id>/events.sanitized.jsonl  request timeline
  answers/<attempt-id>/run-manifest.json       per-request timing and status
  answers/<attempt-id>/question.txt            exactly what was sent
  answers/<attempt-id>/attempt.json            the attempt's terminal record (new runs)
  answers/<attempt-id>/trace-diagram.md, review-sheet.csv   from the traced runner
  reviews/coverage.csv             one row per required element
  reviews/claims.csv               one row per material claim (split rows as needed)
  reviews/decisions.csv            critical failures, claim-enumeration lock, accept/reject
  reviews/usage.csv                verified usage per attempt, or blank
  summary.json
```

Acceptance reviews go in `reviews/`. The per-attempt `review-sheet.csv` is the traced runner's
own sheet and is not read by `summarize`.

The provider receives the frozen question plus the freeze record's neutral output instruction,
nothing else. Required elements, reference passages, and critical failure conditions stay in
reviewer-side files.

## Attempt ledger

Each `attempts.jsonl` record carries the dataset version and hash, `implementation_sha256`, the
HEAD commit and whether `src/`, `pyproject.toml`, or `uv.lock` differed from it, Python and SDK versions, mode, retry policy
(`sdk_max_retries: 0`, `application_retries: 0`), the question and instruction, UTC dispatch and
terminal times, client-observed first-event and terminal elapsed time, terminal status and
failure class, ordered source URLs, artifact paths, `usage_status`, `pilot_only`, `synthetic`,
review status, and missing-data notes. An attempt is written as `in_progress` before the request
and updated after, so a crash leaves a visible record. The terminal record is also written to
`answers/<attempt-id>/attempt.json` before the ledger update; if that update fails (the ledger
lock cannot be taken), `run-one` exits 13 and says so, and `summarize` reports the attempt as
recovered from `attempt.json` rather than leaving it `in_progress`. The next `run-one` writes it
back into the ledger, after which it is an ordinary row (with a `missing_data` note) and no
longer reported as recovered. Do not re-run the request to repair the ledger.

The committed pilot's `implementation_dirty: false` was recorded by a check later found to
always report clean (it ran git from inside the package directory); see
`handoff/HANDOFF.md`. Runs recorded after that fix report it correctly.

| terminal_status | failure_class | exit |
|---|---|---|
| `complete` | `completed` | 0 |
| `task_error` | `stream_error_event` | 2 |
| `http_error` | `http_rejection` | 3 |
| `transport_error` | `transport_error_before_stream` | 4 |
| `premature_close` | `missing_terminal_event` | 6 |
| `silence_timeout` | `client_timeout_silence` | 7 |
| `protocol_error` | `duplicate_terminal_event` | 8 |
| `stream_transport_error` | `transport_error_mid_stream` | 9 |
| `malformed_complete` | `malformed_complete` | 10 |
| `unexpected_error` | `unexpected_error` | 11 |
| `deadline_exceeded` | `client_timeout_deadline` | 12 |
| `outcome_unrecorded` | `post_request_recording_failed` | 13 |

`run-one` also exits 5 when `TABSTACK_API_KEY` is unset and 1 when it refuses; every exit-1
refusal happens before the request, and its message says nothing was sent. Once the request
has been handed to the runner, no failure is reported as a refusal: if the outcome or the
ledger cannot be recorded (lock error, unreadable ledger, missing per-request manifest, a
runner exception, an interrupt), `run-one` keeps what it can in
`answers/<attempt-id>/attempt.json`, exits 13 (an interrupt is recorded, then re-raised), and
says the request was sent, or may have been sent if the runner failed before returning.
`outcome_unrecorded` marks an attempt whose result the client could not read. A run directory
also refuses new attempts when the Python or tabstack SDK version differs from the one that
started it. A client timeout
or mid-stream transport failure sets `client_stopped_waiting: true` and
`provider_task_state: "unknown"`: the client stopped waiting, which does not establish that the
service cancelled the task.

## Score semantics

Coverage `score` and claim `support`: `2` full/supported at the stated scope, `1` partial or
missing qualifier, `0` missing/unsupported or contradicted after inspection, `U` unresolved.
Blank means unreviewed. Neither blank nor `U` enters a numerator or an inspected denominator;
both are counted and shown. `citation_present` is generated from the inline markers and is kept
separate from `support`.

`decisions.csv` per completed attempt: `critical_failures_triggered` (`none`, or `CF1;CF2`, or
blank), `claim_enumeration_locked` (`yes` once the reviewer has split the answer into every
material claim), and `decision` (`accept`, `reject`, or blank). A response is fully reviewed only
when every element and claim has `0`, `1`, or `2`, enumeration is locked, and the critical
failure check is recorded. `summarize` counts an answer as accepted only when it is fully
reviewed, `decision` is `accept`, and no critical failure was triggered; it sets no minimum
score of its own. An `accept` that breaks those rules is reported as a decision conflict.

`usage.csv`: a value needs a unit and a receipt reference. Blank is unavailable, never zero. Cost
per accepted answer is reported only when every attempt in the scope has usage in one unit, every
completed response is fully reviewed with a decision, and at least one answer is accepted;
otherwise the summary says why it is unavailable. The `/research` stream carries no per-request
credit field (tabstack 2.8.5), so usage has to come from a separate verified record.

`pilot_only` and `synthetic` attempts are reported in their own scopes and excluded from live
scored totals.
