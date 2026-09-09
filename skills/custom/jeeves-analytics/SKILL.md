---
name: jeeves-analytics
description: Use this skill for ad-hoc analytical questions about the Jeeves credit portfolio — DPD rates, charge-off trends, portfolio balances, revenue breakdowns, vintage analysis, and concentration reports. Builds on the jeeves-redshift skill for data access.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Portfolio Analytics

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


Ad-hoc analytical questions about the Jeeves credit portfolio.

## HARD RULES - Metabase / self-serve dashboards (added 2026-08-26, Dream #99)

1. **NEVER curl the Metabase API from bash.** The bash path guard reads URL path fragments as filesystem
   paths and rejects them (`Unsafe absolute paths in command: /api/collection`, `/api, /card/`, `/=`) - four of the six tool
   errors in the 2026-08-25 dashboard build were this, not real path problems. Write a python script with
   `requests`, the full `https://` URL as a string and `METABASE_API_KEY` from env, then run it with
   `$PYTHON_PATH`. The same rule exists in cfo-org-kb/SKILL.md and was violated anyway because that skill is
   not loaded for a dashboard task - it now lives here too, where the work happens. **This also applies to the
   patch scripts you write about this rule**: a bash heredoc containing those tokens is itself rejected.
2. **Host is `https://jeeves.metabaseapp.com` and it sits behind Zscaler like Redshift.** An
   `SSLError / Max retries exceeded` against that host is a VPN symptom: run `zscaler-reauth/reauth.py`, then
   retry once, before suspecting the API or the token.
3. **Dashboard of record for borrowing-base self-serve is 10919** - "Capital Markets - Borrowing Base
   Reporting" in the Finance collection, built 2026-08-25 for **Axel Orendain and William Filho** (cash
   reporting). Two sections, NB and CIM; as-of-any-date **eligible vs ineligible receivables by country**
   (**Mexico is excluded from NB; only Mexico is eligible in CIM**) plus an MTD rollforward. It ties to the NB
   workbook to the cent (eligible $118,731,471 at 2026-08-16) - tie out any new card the same way before
   announcing it. Queries belong in cfo-org-kb.
4. **Announcing a dashboard: the bot must JOIN the Slack channel first** - `chat_postMessage` to a channel
   deerflow_analyst is not a member of fails (hit on **#ops-mx-spv**, 2026-08-25). Join, post, then quote the
   returned `ts` as proof.

## Examples

- "What's our 90+ DPD rate in Mexico vs last month?"
- "Which industries have the highest default rates?"
- "What's the total portfolio balance by country?"
- "Show me charge-off trends over the last 6 months"
- "Top 20 borrowers by balance"
- "How has DPD distribution changed month over month?"
- "Total revenue this quarter vs last quarter"

## Approach

1. Load `jeeves-redshift` skill for connection details and full schema reference
2. Install driver: `pip install psycopg2-binary`
3. Write and execute SQL against confirmed tables:
   - `capital_markets_dm.loc_tape` — LOC balances, disbursements, DPD, charge-offs (by company by day)
   - `capital_markets_dm.gwc_tape` — GWC/Jeeves Pay loan balances and obligations (by loan by day)
   - `master_customer_dm.companies_dm` — Company metadata (name, country, industry, credit limit)
   - `master_transactions_dm.transactions_ssot` — Revenue, GTV, transaction details
   - `analytics_sandbox.loc_vintage_data` — Vintage cohort analysis, BOP/EOP balances
4. Single metric → respond inline:
   > **[Metric]:** [value] (as of [date]) | vs prior: [delta]
5. Multi-metric or table → write to .xlsx with openpyxl, present the file

## Common Patterns

### Portfolio balance (latest snapshot)
```sql
SELECT country_code, COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS total_balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
GROUP BY country_code
```

### Delinquency distribution
```sql
SELECT dq_bucket, COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS balance
FROM capital_markets_dm.loc_tape
WHERE dt = (SELECT MAX(dt) FROM capital_markets_dm.loc_tape)
  AND charge_off_flag = false AND is_in_repayment = false
GROUP BY dq_bucket
ORDER BY dq_bucket
```

### Monthly charge-offs
```sql
SELECT DATE_TRUNC('month', charge_off_dt) AS month,
       COUNT(DISTINCT company_id) AS accounts,
       SUM(balance_usd) AS charged_off_balance
FROM capital_markets_dm.loc_tape
WHERE charge_off_dt IS NOT NULL
  AND dt = charge_off_dt
GROUP BY 1 ORDER BY 1
```

### Revenue by product
```sql
SELECT product, sub_product,
       SUM(revenue_usd) AS total_revenue,
       SUM(gtv_usd) AS total_gtv
FROM master_transactions_dm.transactions_ssot
WHERE posted_at BETWEEN '2025-01-01' AND '2025-12-31'
  AND is_company_test = false
GROUP BY product, sub_product
ORDER BY total_revenue DESC
```

## Forecasting / predictive-model asks — HARD RULES (added 2026-08-19 from the Jeeves Pay spend model, thread e405278b)

1. **Never report "no predictive value" or "too noisy" until calendar features are exhausted.** `dow` + `dom` + crude first/last-week flags are NOT calendar features. Test ordinal/compound ones before any negative conclusion: **last-weekday-of-month, nth-weekday (e.g. last Friday), business-day-of-month, days-to-month-end, payday proximity, holiday adjacency**. Brian pushed back twice on exactly this ("did you look at day of week? like the last friday of every month", "I dont believe that theres no predictive value here") and the features were in fact material.
2. **Fix the GRAIN before you model, and the grain is always customer x calendar day.** "i care about a name by name basis, not portfolio" (2026-08-18), and again on 2026-09-08 (thread dbfc6e26): *"We need every day split. So if Macropay needs to spend $4M, we need to have expected it, otherwise we have to move money"* then *"No, we need a customer level model. A macropay model, a Copastur model, etc etc"*. That is not a request for per-name validation of one pooled panel model — it is **one model per named customer, emitting a dollar forecast for each future day**, because the decision it feeds is a T-1 treasury funding decision for that customer on that date. Deliverable shape: a per-customer sheet/section for the largest Jeeves Pay names, daily forecast vs actual, plus charts (scatter, sorted per-name error bars, per-name time series). A portfolio-aggregate WAPE, or a monthly-total forecast, answers a question he did not ask. State the customer list you modeled and where it came from — he will correct a wrong name (*"copastur was wrong. the largest JP customers"*), so **derive the top-N from spend data, never from memory of who is large**.
3. **Pick the metric that matches the decision.** On a panel that is ~98% zeros, amount-error metrics (WAPE/MAPE) are dominated by noise and hide real skill; score **ranking and dollar concentration** (top-decile share of actual dollars, hit rate) alongside amounts, and say which metric drives the decision (T-1 cash movement = ranking + magnitude bands, not point accuracy).
4. **Always show out-of-sample walk-forward** with the calibration fitted only on pre-cutoff data, and state the cutoffs explicitly. Brian asks for validation every single time ("yes, and well need to see a validation") — build it into the first delivery.
5. **"ELI5" is a request to change the register, not to add a summary.** When Brian says ELI5 after a
   model write-up (2026-09-08, thread dbfc6e26), answer in **plain business English with no model
   vocabulary** — no "panel", "WAPE", "walk-forward", "features", no formulas, no Greek. Say what the
   money problem is, what the model guesses, how often it is right, and what he does differently
   because of it, in that order, in a handful of short sentences. Never re-explain the methodology in
   friendlier words and never make it longer than the technical version.
6. **Compute budget: a modeling run that will exceed ~15 minutes must be chunked.** The harness cancels a task at 20 minutes and the finished write-up is lost (happened 2026-08-18 14:51). Persist each stages output (Excel/notebook/JSON) to disk as it completes, then report from the artifact. Do NOT post an interim "building..." line — a bare `?` from Brian is the cost of that.

## Structuring & scenario models (collateral, SBLC, carve-out) — HARD RULES (added 2026-08-28, Dream #101, from the Mastercard Brazil collateral model, thread 6c24477e)

Ten corrections in one thread, tone escalating to *"dude stfu"* and *"this is not good work"*.
Every one was avoidable. Before building any structuring model:

1. **Open the workbook and this reply with a PINNED ASSUMPTIONS block, and never re-raise a
   constraint Brian has removed.** He said the facility size was not a constraint — *"We will
   upsize. I told you that is not a constraint"* — and the model kept re-deriving a $150MM ceiling
   anyway. When he overrides an assumption, write it into the model as a named input with his words
   next to it, restate it at the top of the next reply, and stop testing against the old world. A
   constraint he has explicitly waived reappearing in your output reads as not listening.
2. **Confirm the INSTRUMENT MECHANIC in one sentence before you build, not after he corrects it.**
   Three of the ten corrections were structural, not numerical: the requirement is 100% of Brazil
   card receivables (not a deficit figure); an SBLC **substitutes for** a stated % of the collateral
   MC requires (it is not an advance against the collateral); and with an SBLC the receivables
   **stay in the borrowing base** (no carve-out haircut). Get the one-line mechanic agreed first —
   "so: SBLC covers X% of the required collateral, receivables stay in the BB, aforo applies to the
   SBLC's own cash cover — correct?" — then model.
3. **Every non-zero driver MUST propagate to every output. Run a driver-sensitivity check before
   delivering.** *"how is the equity required for the SBLC 0 when the aforo is non 0? this is not
   good work."* A driver wired into one branch and dropped in the other is the single most damaging
   defect, because it makes the whole comparison worthless. Mechanical check: perturb each input
   ±10% one at a time and assert every dependent output moves; any output that does not move is an
   unwired driver until proven a genuine zero. Do this before the workbook leaves your hands.
4. **Answer the comparative question in the SHAPE he asked for — a breakeven, not a table.**
   *"Is an SBLC better than a carve out? If so, at what aforo levels, and if not, what percentage of
   Brazil card spend needs to stay on short cycle."* That is a request for two numbers (the aforo
   crossover, and the short-cycle % that closes the gap), each with the direction of the inequality.
   Lead with them; the table is supporting evidence underneath.
5. **Do not add scenario dimensions he did not ask for.** *"we don't need any of this 'current size'
   vs 'upsize' — I'm asking for the raw impact."* Every extra axis doubles the tabs and halves the
   signal. One axis unless he names a second.
6. **A model question is a MATH question.** *"I'm asking about a model, that has nothing to do with
   contracts."* When the ask is "is A better than B and at what level", do not answer with document
   language, consent mechanics or legal feasibility. If a contractual fact genuinely bounds the math,
   state it in one clause as an assumption and keep going.
7. **Default deliverable is ONE tab: the answer.** *"the workbook is too complicated now. Put this
   summary into a single tab workbook."* Drivers at the top as labelled live inputs, the comparison
   in the middle, the breakeven at the bottom. Working tabs stay on disk unless he asks for them.
8. **A multi-part ask is not done until the LAST part is done.** The instruction ended "...then reply
   with a very simple summary paragraph to Alex's email"; the workbook shipped, the draft did not, and
   the next message was *"you didn't draft the email"*. Re-read the original instruction verbatim
   before reporting, enumerate its clauses, and confirm each one produced an artifact.

## When Brian disputes your root cause — HARD RULES (added 2026-08-25 from the MX MSR column investigation, thread 7ed6747d)

1. **Test HIS hypothesis first, explicitly, and lead the answer with the verdict on it.** Brian does not push back vaguely — he supplies a specific mechanism ("we added the fee column and just didn't update the index", "it's just a naming artifact"). That is a testable claim. Run the test for HIS mechanism before defending yours, and open with "your theory holds / does not hold, here is the query". On 2026-08-24 he was right, and it took him two rounds and "I really don't believe you" to get there because the answer kept re-arguing the original position.
2. **Prove a column-mapping claim by magnitude against an independent source table, not by reasoning about names.** Sum the suspect columns for one period, sum the corresponding fields in the upstream/system-of-record table for the same period and currency, and match by order of magnitude. Two `SUM()` queries settled a 69-turn argument.
3. **"Duplicate columns" is a labelling hypothesis until proven a data hypothesis.** Identical sums mean one column under two names — that is a naming defect and nothing is double counted. Never tell a lender or a counterparty that a report double counts until you have shown the same row contributing twice to one total.
4. **"Can you look at <person>'s email about <report>" is never a request for a summary.** He has read it. He wants the underlying data question answered and a reply he can send. Answering with a recap earns "why would I just want you to summarize it?" (2026-08-24 08:07 PT).

## Movement / roll-rate explanations — HARD RULES (added 2026-09-04, Dream #108, from the mods 31–60 thread 60fba965)

1. **One bullet = one unit. Never mix metric types on a line.** "$462K (2 loans) cured to 1–30 — TRUCKA USA paid $38.9K, TPS Container paid $17.6K" mixed *balance that changed bucket* with *cash received*, and Brian's next message was **"38+17 does not equal 462"** (2026-09-03 12:27 PT). If a line carries two or more dollar figures they must either add up, or each figure gets its own labelled line: "balance moved", "cash applied", "DPD reset — no cash".
2. **Add up your own numbers before sending.** Brian audits arithmetic on sight. Print the components AND the total, and make the total equal the sum. An unexplained residual is a labelled line ("unexplained — $X, still tracing"), never a rounding shrug.
3. **A bucket delta is only explained when exhaustive, mutually exclusive causes sum to it**: cash paid, DPD reset / RPP re-bucketing, roll to the next bucket, charge-off, new inflow. August mods: $1.57M left 31–60 = ~$129K real cash + ~$1.0M rolled to 61–90 + the rest re-bucketed by RPP DPD resets. "The bucket emptied" is not an answer until the pieces tie.
4. **Obligor names attach to the figure that belongs to them**, on their own sub-line beneath the bucket total — never appended to a different number with an em-dash.

## Answering Brian directly — HARD RULES (added 2026-09-05, Dream #109, from the AIG/Michael currency thread caefcb01)

1. **A scalar question gets a scalar answer on line one.** "How much does the customer owe us
   total" is answered with **one number, one currency, one as-of date, in the first six words** —
   then the source table/file on line two, then caveats. On 2026-09-04 09:18 PT the total was
   withheld across several turns behind currency and file-reconciliation qualifiers and Brian
   escalated to *"can you stop being dense and just answer the fucking question."* Ambiguity is
   NEVER a reason to defer the number: give the number under the most likely reading, label the
   assumption in the same line ("$X USD, per <file>, as of <date> — assumes USD not MXN"), and
   put the alternative underneath. A question you answered with a question is a failed turn.
2. **Currency is stated on every figure, always — and "already in USD?" is a verification task,
   not an opinion.** When Brian asks whether numbers are USD or MXN, prove it: name the column,
   the file, and a magnitude check against a second source. Never answer from inference about
   what a field "should" be.
3. **When two sources disagree (our tape vs. a counterparty's file), lead with BOTH totals and
   the delta, in that order** — "we say $A, their file says $B, delta $C" — before any
   explanation of why. Brian's follow-ups ("so those numbers don't tie to the actual file we
   sent?", "so what does the AIG file say") are him extracting figures one at a time that
   should have been in the first answer as a three-line table.
4. **Detail belongs in the artifact, not the reply.** Chat = the number plus the so-what in
   <=5 lines; methodology, exclusions, row-level backup and reasoning go into the Excel/doc
   appendix uploaded to Drive. If detail matters enough to write, it matters enough to attach.

## Rules

- Always state the snapshot date in the response
- MX balances are USD unless explicitly asked for MXN. Use `spot_rate` from loc_tape for conversion — do NOT hardcode an FX rate.
- "Active" accounts = balance_usd > 0 AND charge_off_flag = false AND is_in_repayment = false
- Revenue questions → use transactions_ssot.revenue_usd
- Spend questions → use loc_tape.disbursement_amount_usd
- Always exclude test companies
- Use COALESCE when combining amount columns in arithmetic
