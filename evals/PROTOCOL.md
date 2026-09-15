# Pre-registered evaluation protocol v1

**Working article:** Search + fetch + model versus a managed Research call: the evaluation plan  
**Status:** Must be committed before full execution  
**Full evaluation authorized by this document:** No. Build and pilot only.  
**Primary CTA:** Submit a workflow or evaluation question.

## 1. Question

For current public-web questions, what differences appear between:

- **System A:** a pinned search + fetch + model pipeline operated by the application; and
- **System B:** one Tabstack managed `/research` call in `fast` mode?

This is a comparison of complete configured systems. It cannot isolate whether an observed difference was caused by search, retrieval, model choice, orchestration, source availability, or the managed service unless a later controlled experiment changes one component at a time.

## 2. Hypotheses

These are directional hypotheses, not conclusions.

### H1: citation completeness

System B will have a higher median citation-completeness score because citations and source mappings are part of the managed output contract.

### H2: answer coverage

System B will have a higher median coverage score on multi-source and conflicting-evidence questions because its loop can evaluate gaps and continue searching.

### H3: latency

System A may have lower median wall-clock duration on simple questions because it can stop after a small fixed retrieval pass.

### H4: cost

No direction is pre-registered. The measured cost depends on the baseline model/search/fetch providers and Tabstack’s variable Research actions.

### H5: simple source discovery

On questions whose required output is mainly a list of official sources, System A will be no worse than System B on usefulness within the limits of this small sample.

## 3. Systems

### System A: search + fetch + model

Freeze before execution:

- search provider/API/version;
- fetch implementation/version;
- model provider and exact model ID/version;
- system and orchestration prompts;
- maximum search iterations;
- queries per iteration;
- results per query;
- maximum fetched pages;
- characters/tokens per page;
- total source-context cap;
- timeout and retry policy;
- citation schema;
- cost source.

Default resource ceiling if no existing product baseline is selected:

```yaml
max_search_iterations: 3
max_queries_per_iteration: 3
max_results_per_query: 5
max_pages_fetched_total: 12
max_chars_per_page: 12000
max_total_source_chars: 60000
total_wall_clock_timeout_seconds: 300
application_retries: 0
```

The baseline model must be one the team would realistically deploy. Do not choose a weak model to favor Tabstack.

### System B: managed Research

Freeze before execution:

```yaml
provider: Tabstack
endpoint: /research
mode: fast
nocache: true
fetch_timeout: default unless changed before test
application_retries: 0
sdk: exact locked Python package version
```

SDK-native transport retries must be documented for both systems. Do not silently count retried provider calls as one operation in cost reporting.

## 4. Test set

Use the frozen questions in `questions.jsonl` (this directory).

Composition:

- 12 total questions;
- 3 simple/source-discovery questions;
- 5 multi-source synthesis questions;
- 2 freshness-sensitive questions;
- 2 conflict/incomplete-evidence questions.

The test set intentionally includes cases where search may be sufficient. It is not designed only around Tabstack’s strongest use case.

Before execution, verify each question is:

- answerable from public sources;
- free of private or customer data;
- not a request for legal, medical, or financial advice;
- not about Tabstack or its direct competitors unless the question is explicitly neutral;
- specific enough to score;
- current as of the freeze date.

If a question fails preflight, replace it before any system sees any question, increment protocol version, and commit the replacement.

## 5. Run design

### Pilot

- Run Q01 once through each system.
- Purpose: validate logging, output packaging, timing, usage capture, and blind-review files.
- Mark `pilot_only=true`.
- Exclude pilot outputs from full results unless protocol v1 explicitly restarts both systems from a clean state and states inclusion before scoring.

### Full evaluation

Not part of the current coding task. When approved later:

- run all 12 frozen questions;
- minimum one run per system/question for the first published evaluation;
- recommended three independent runs per system/question if budget allows;
- interleave system order by a deterministic seeded schedule to reduce time-of-day bias;
- run paired questions within the shortest practical window;
- use UTC timestamps;
- preserve all attempts, including failures and incomplete outputs;
- never rerun only the losing system.

If using one run per pair, label the study exploratory and do not claim stable provider performance.

## 6. Required answer format

Both systems receive the same original question and this neutral output instruction:

```text
Answer the question directly. Support material factual claims with citations to public source URLs. Distinguish sourced facts from interpretation. State when evidence is incomplete or conflicting. End with a Sources section listing only sources used in the answer.
```

System-specific plumbing may differ, but the requested final artifact must be the same.

## 7. Metrics

Report every metric separately. Do not collapse them into one “winner” score.

### 7.1 Answer correctness

Unit: scorable material claims.

For each material factual claim:

- `2` supported and accurate;
- `1` partly supported, imprecise, or missing a necessary qualification;
- `0` contradicted or unsupported.

Report:

- correct claims / scorable claims;
- partially correct claims / scorable claims;
- incorrect claims / scorable claims;
- unscorable claims separately.

A domain reviewer or primary-source adjudication is required. If neither is available, report correctness as unavailable, not guessed.

### 7.2 Citation correctness

Unit: claim-citation pair.

- `2` cited source directly supports the claim;
- `1` source is relevant but support is indirect or partial;
- `0` source does not support the claim or is inaccessible to the evaluator.

Report numerator and denominator from the same complete evaluated rows.

### 7.3 Citation completeness

Unit: material externally verifiable factual claims.

```text
citation completeness = cited material claims / material factual claims
```

Do not count opinions, transitions, or clearly labeled interpretation as requiring citation.

### 7.4 Question coverage

Before runs, each question must list 2–5 required answer elements. Score each:

- `2` answered fully;
- `1` answered partially;
- `0` missing.

Report covered points / available points.

### 7.5 Source quality

For each cited source:

- `3` primary/official source for the claim;
- `2` strong independent secondary source;
- `1` weak, derivative, undated, or unclear source;
- `0` source is irrelevant or unusable.

Also report source-domain diversity without treating more domains as automatically better.

### 7.6 Freshness

For claims with a known date requirement:

- `2` source and claim meet the required freshness window;
- `1` date is unclear but no contradiction was found;
- `0` evidence falls outside the required window or is stale.

### 7.7 Uncertainty handling

- `2` clearly identifies material gaps/conflicts and scopes the conclusion;
- `1` uses vague qualification without naming the gap;
- `0` presents unresolved evidence as certain.

### 7.8 Latency

Measure client-observed wall-clock milliseconds from request dispatch to terminal success/failure. Report:

- all attempts;
- successful attempts separately;
- first-event latency where available;
- terminal latency;
- median and range only when there are enough runs to make them meaningful.

Do not call a one-run result “typical.”

### 7.9 Cost

Use authoritative provider usage/billing receipts. Include:

- search cost;
- fetch cost, if paid;
- model input/output usage and cost;
- Tabstack credits or cost actually consumed;
- failed/retried attempts when billed.

Do not estimate Tabstack call cost from the public per-action rate because Research uses a variable number of actions. If authoritative usage is unavailable, mark the cost unavailable for that run.

### 7.10 Reliability and completion

Terminal status:

- complete;
- partial;
- unanswered;
- HTTP/transport failure;
- task-level failure;
- timeout;
- evaluator unable to score.

All statuses stay in the denominator. Report success counts with the exact denominator and test window.

## 8. Blind evaluation

Where practical:

1. Normalize final answer formatting without changing words, links, or citation placement.
2. Assign random answer IDs that do not reveal the system.
3. Remove provider names from headers and metadata only. Do not remove provider names that are part of the substantive answer.
4. Have the evaluator score answer quality before seeing timing, cost, event logs, or system identity.
5. Reveal identity only after scoring is locked.

The engineer who built the systems should not be the only quality evaluator.

## 9. Data to preserve

For every system/question/run:

- frozen question ID and text;
- required answer elements;
- system config hash;
- start/end UTC;
- duration;
- terminal status;
- final answer;
- citation/source records;
- sanitized event/tool log;
- provider usage receipt;
- retry count;
- failure reason;
- blind answer ID;
- evaluator scores and notes;
- adjudication notes.

Do not store credentials, authorization headers, cookies, private account IDs, full environment dumps, or confidential inputs.

## 10. Stopping and exclusion rules

- Do not stop because one system looks better.
- Stop the complete study for security, runaway spend, provider outage affecting comparability, corrupted logging, or protocol violation.
- Pre-existing provider outage may justify rescheduling the whole paired block, not deleting one system’s failure after the fact.
- Exclude a run only for a documented harness failure that occurred before the provider received the request. Keep it in an exclusions ledger.
- A provider error, timeout, empty answer, or malformed citation is a result, not an exclusion.
- If scoring instructions change, create protocol v2 and rerun the full set.

## 11. Analysis plan

For each metric:

- show per-question paired results;
- show denominators;
- summarize medians/ranges only where appropriate;
- distinguish observation from interpretation;
- identify question types where each system was sufficient;
- report failures and missing cost data;
- avoid significance claims with this sample unless a qualified statistical design is added before execution.

The first 12-question evaluation is exploratory. It can reveal implementation tradeoffs and candidate hypotheses. It cannot establish universal superiority.

## 12. Publication requirements

The Prove article published this week should describe:

- the question;
- hypotheses;
- complete system definitions;
- frozen question-set composition;
- scoring rubric;
- cost and latency measurement;
- blind evaluation method;
- failure and stopping rules;
- limitations;
- public repository and protocol commit;
- invitation to submit a real workflow or evaluation question.

Do not publish Q01 pilot results as a benchmark conclusion. The article’s value is the transparent method.
