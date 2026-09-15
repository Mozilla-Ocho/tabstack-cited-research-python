# Scoring rubric (protocol v1, section 7)

Score every metric separately. Never collapse them into one winner score. Report numerators and
denominators from the same complete set of evaluated rows. Failures, timeouts, and empty answers
stay in the denominator.

## Unit of analysis

- **Material factual claim:** a statement a reader could act on and that a public source could
  confirm or contradict. Opinions, transitions, and clearly labeled interpretation are not claims.
- **Claim-citation pair:** one material claim and one source cited for it. A claim with two
  citations is two pairs.
- **Required answer element:** listed per question in `questions.jsonl`. Fixed before any run.

## 7.1 Answer correctness (per material claim)

| Score | Meaning |
|---|---|
| 2 | Supported and accurate against a primary source the evaluator checked |
| 1 | Partly supported, imprecise, or missing a necessary qualification (date, plan, version) |
| 0 | Contradicted, or unsupported after a good-faith check |
| unscorable | Evaluator could not adjudicate; report the count, never fold into 0 or 1 |

## 7.2 Citation correctness (per claim-citation pair)

| Score | Meaning |
|---|---|
| 2 | Cited page directly supports the claim |
| 1 | Page is relevant but support is indirect or partial |
| 0 | Page does not support the claim, or the evaluator could not access it |

## 7.3 Citation completeness

`cited material claims / material factual claims`, as a percentage. Only externally verifiable
claims count in the denominator.

## 7.4 Question coverage (per required element)

| Score | Meaning |
|---|---|
| 2 | Answered fully |
| 1 | Answered partially |
| 0 | Missing |

Report `points earned / points available`.

## 7.5 Source quality (per cited source)

| Score | Meaning |
|---|---|
| 3 | Primary or official source for the claim |
| 2 | Strong independent secondary source |
| 1 | Weak, derivative, undated, or unclear |
| 0 | Irrelevant or unusable |

Also record distinct source domains. More domains is not automatically better.

## 7.6 Freshness (claims with a date requirement only)

| Score | Meaning |
|---|---|
| 2 | Source and claim meet the required window |
| 1 | Date unclear, no contradiction found |
| 0 | Outside the window or stale |

## 7.7 Uncertainty handling (per answer)

| Score | Meaning |
|---|---|
| 2 | Names material gaps or conflicts and scopes the conclusion |
| 1 | Vague qualification without naming the gap |
| 0 | Presents unresolved evidence as certain |

## 7.8 Latency

Client-observed wall-clock ms from request dispatch to terminal success or failure. Record
first-event ms where the transport exposes it. Medians and ranges only with three or more runs per
cell. One run is one observation, never "typical."

## 7.9 Cost

Authoritative receipts only. For System A: provider usage fields (tokens) converted at the
published rate on the run date, plus search-call count times the published search price. For
System B: console credit balance delta across the paired block, or `unavailable`. Never compute
Tabstack cost from the per-action rate.

## 7.10 Terminal status

`complete | partial | unanswered | http_failure | transport_failure | task_failure | timeout |
evaluator_unable_to_score`. Every status is a row.

## Blind scoring procedure

1. Score from `blind/<id>.md` only. Do not open `result.json`, event logs, or timing first.
2. Record scores in `results.csv` against `blind_answer_id`.
3. Lock scores (commit the CSV), then map IDs back to systems.
4. The engineer who built the systems is not the sole evaluator.
