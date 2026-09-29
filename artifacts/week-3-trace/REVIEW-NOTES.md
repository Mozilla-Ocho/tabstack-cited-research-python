# Review notes for `review-sheet.csv`

**First-pass review, 2026-09-29.** Technical-reviewer sign-off, which the Build spec requires, is
still pending. Check every row before any of it is quoted.

## Method

- Rubric: Week 3 Understand draft, "What makes a citation useful", section "A rubric you can
  reuse". Support: `2` supported at the stated scope, `1` partial or needs a qualifier, `0`
  unsupported or contradicted, `U` couldn't inspect.
- Pages were fetched with `curl` at 2026-09-29T18:50:05Z. Every row's `retrieved_at_utc` records
  that fetch. SHA-256 of each response:
  - `https://docs.ollama.com/capabilities/web-search.md`: `faf06fd3fd8990caa6577158fdef7281c0cb089acf4cdcce86e53b0741bb6997`
  - `https://docs.ollama.com/capabilities/web-search` (HTML): `8340deb5b049b71393ee3ece6f2c8802bb95aa6c293d8e5b86dc484b08a85b5a`
  - `https://ollama.com/blog/web-search-subagents-claude-code` (HTML): `7a2285c51c0453f176e53078e7509fa160ec055d7fd368bdd3cbf5dbc0468298`
- Cited pages 1 and 2 serve the same content: the HTML page contains the same key sentences as
  the `.md` version. Page text is not stored here, per the rubric's "don't paste whole pages".
- Every `passage` except C04x's was checked by script to occur word for word in the fetched
  text. C04x's passage is a reviewer note, in parentheses.
- Uncited sentences were scored against whichever returned page holds the evidence; `reason`
  records that the report itself cited nothing.
- The same rule was applied throughout: a claim that adds a ranking, frequency, or scope the
  page does not state ("primary", "often", "specific models") scores `1`, even when its core
  fact is on the page.

## Changes to the generated rows

- C03 split into C03a, C03b, C03c: three decision-driving facts in one sentence.
- C04x added: the citation set `[1][2]` is one page. This follows the Understand post's own
  worked example, which logs the overstatement as its own `1`.

## Tallies

13 scorable rows: `2` = 5, `1` = 8, `0` = 0, `U` = 0. Without the C04x citation-set row:
12 claims, `2` = 5, `1` = 7.

Every claim's core fact was found on a returned page. The `1`s come from qualifiers the report
dropped or added. The ones that matter most for the question:

- **C04** calls `content` the content "of a relevant web page". The docs say "relevant content
  snippet". The question asked what each approach returns, so snippet versus page matters.
- **C07** says the MCP route returns "the same structured JSON output". The page shows the MCP
  server uses the same API key but does not say what it returns.
- **C06** expands MCP as "Multi-Context Processor". The page does not expand it; it is Model
  Context Protocol.
- **C08** narrows built-in search to "specific" cloud models. The post says it works with any
  cloud model.

Five sentences (six rows after the split) had no inline citation. That includes the endpoint,
key requirement, and `max_results` limits, which are the most decision-driving facts in the
answer.

## Not scored

- **Coverage.** The rubric asks for required elements to be frozen before reading the output.
  That was not done, so no coverage rate is reported. Informally, the "what output does each
  approach return" half is the weak one. Only the REST API's output is documented on a cited page
  (with the snippet caveat). The MCP and Anthropic-layer outputs are the report's inference.
- **Citation accuracy in general.** One run, one reviewer, one question. This says nothing about
  the product's citation quality and should not be quoted that way.
- **Source quality.** Cited pages 1 and 2 are the same vendor docs page, and page 3 is the
  vendor's blog. No independent source was cited.
