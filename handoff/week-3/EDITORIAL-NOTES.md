# Editorial notes for the Week 3 Build article

These are decisions and checks outside the code. Each one lists what was found; the decision
belongs to editorial.

## 1. CTA

- Build spec: **Run the traced example.**
- Messaging package CTA table, Research intent: **Make a research call** (links to console signup).

The article is a tutorial built around this repo, so the spec's CTA fits the reader's next step.
The package's CTA fits a product page. Options: use the spec's CTA as the primary and the
package's as secondary, or ask the messaging owner whether Build articles count as "Research
intent". Undecided.

## 2. Trust line and the Privacy Notice

Approved line: **Never trained on. Private by default. Built by Mozilla.**

The live [Privacy Notice](https://tabstack.ai/legal/privacy) (last updated 2026-09-16, checked
2026-09-29) says:

- "Tabstack allows you to query selected LLMs offered by third parties (“Third-Party Models”)"
- "We store history (including your content and/or Outputs) for 90 days or unless you delete it."
- It does **not** contain the word "train" anywhere.

So "Never trained on" has no supporting text in the Privacy Notice. It may be backed by the
Terms, a DPA, or provider contracts; that was not checked. Trust or legal should confirm the
source before the article uses the line. Under the spec's gates, the article must not say
nothing leaves the user's stack. The question and fetched page content go to Tabstack and to
third-party models.

## 3. Current-answers page (tabstack.ai, `src/content/solutions/current-answers.mdx`)

The messaging package says copy that conflicts with product behavior does not ship until the
behavior is confirmed. For the page owner:

- The `applications` list includes "A draft-review step that validates claims before display."
  In context this lists it as a job Tabstack Research does. The Week 3 Understand post says "A
  completed request doesn't make the answer verified", and the Build runs support that. In fast
  mode `claims` was empty on all 15 cited pages across three runs. In the reviewed run, 5 of 10
  sentences had no inline citation, and the reviewed claims mostly scored `1` (partial) because of
  dropped or added qualifiers.
- The frame "returns a cited answer" holds at the report level: every run had citations. It does
  not hold at the sentence level: 5 of 10 and 1 of 7 sentences were uncited in the two Week 3
  runs.

Two runs of one question prove little, and this is not a finding that the copy is false.
Suggested action: the owner reviews line 38's wording (for example, "a step that drafts cited
answers for review before display") and decides.

## 4. Copy rules applied to repo text

The README, artifact READMEs, and handoff docs contain no em dashes and none of the package's
banned phrases. The trust line does not appear in the repo.
