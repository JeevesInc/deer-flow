---
name: jeeves-servicer-report
description: Use this skill for the MONTHLY servicer reports - the CIM MX servicer report with its Anexo J distribution instruction letter (the 15th, covering the prior month) and NB monthly reporting (the 14th - month-end borrowing base certificate, cash report, financials chase). Triggers on "servicer report", "Anexo J", "carta de instrucción", "Monex", "Marisol", "waterfall", "distribucion", "monthly reporting", "month-end certificate". NOT for the recurring daily/weekly borrowing bases (jeeves-borrowing-base).
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Servicer Reports (monthly)

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.

## The three deliverable families

Capital Markets produces **five recurring artifacts in three families** (Brian, 2026-09-14). Each
family is a separate skill. Never substitute one for another — they have different cadences,
different audiences, and different source data.

| Family | Artifact | Cadence | Skill |
|---|---|---|---|
| **Borrowing base** | CIM MX SOFOM BB | **daily**, 08:30 PT | `jeeves-borrowing-base` |
| **Borrowing base** | NB BB | **weekly**, Mondays, as of the prior Sunday | `jeeves-borrowing-base` |
| **Servicer report** | CIM MX servicer report + Anexo J | **monthly**, the 15th, covering the prior month | `jeeves-servicer-report` |
| **Servicer report** | NB monthly reporting | **monthly**, the 14th | `jeeves-servicer-report` |
| **Portfolio reporting** | Portfolio Report (internal) | **monthly**, the 1st | `jeeves-portfolio-report` |

**The relationship that matters:** a servicer report is monthly and **contains** borrowing-base
figures; a borrowing base is a standalone weekly or daily deliverable. Being asked for "the BB"
never means the servicer report, and month-end does not turn a borrowing base into one.

**Retired 2026-08-10 — do not build, do not route to:** the daily **US Borrowing Base (Bridge)** and
the **CIM Monthly Tested Portfolio Report**. The CIM US bridge facility was terminated and repaid in
full ($76,055,829.63), replaced by NB. `build_us.py` hard-exits unless `BRIDGE_BB_ENABLED=1`.
CIM today means **MX SOFOM only**.

**A servicer report CONTAINS borrowing-base figures but is not one.** The tape, the eligibility
fields and the Exhibit math come from the same machinery documented in `jeeves-borrowing-base`
— load that skill for the build mechanics. What lives here is everything monthly: the period
close, the Exhibits, the waterfall, the Anexo J, and the counterparty interaction.

---

## MX Servicer Report (Monthly)


### Run recipe (verified 2026-08-10, July 2026 period)

`build_mx_servicer_report.py` in this skill dir is STALE: dates hardcoded to April 2026 AND it still
writes only the BOP and EOP snapshots to the tape tab. Delivered workbooks require a FULL-MONTH DAILY
tape (June file = 30 daily slices, 22,929 rows) because Exhibit B G10 average daily balance is
`SUM(tape!AC:AC)` divided by `(D6-D5)`. Loop
`/mnt/skills/custom/jeeves-borrowing-base/sql/data_tape_sofom.sql` once per calendar day, apply
`calculate_eligibility_fields_sofom` (from `jeeves-borrowing-base/eligibility.py` — the tape and
eligibility machinery stayed with the borrowing-base skill when this one split off on 2026-09-14),
and assert the resulting column list equals the template tape
header EXACTLY (138 cols) - every Exhibit formula is a column-letter reference (AC=sofom_balance,
I=dq_bucket, S=late_payment_penalty_amount, O=disbursement_amount, H=days_past_due). Fastest safe
refresh of a 23k-row tab: `del wb['tape']` then `wb.create_sheet('tape', idx)` - no formulas live on
tape or lista, and cell-by-cell blanking of 3M cells is needlessly slow.

Naming and filing of record: template = prior month's `Jeeves Mexico Servicer Report - {YYYY}{MM}15.xlsx`
in `Capital Markets, Debt, CIM, Reporting, {YYYYMM}` (Reporting folder id
1oDi6HjzArnX_jPQbI9UfEs4R5DC9TTrA; 202607 id 11sGwEQFDwe3gbJu0VG7oSWx70yReOlVG, 202608 id
1eEv4-_bidc7xILB-s8oOVGI-2_fEx16U). The file dated the 15th covers the PRIOR month (20260715 = June
period, BOP 6-1 EOP 6-30). Upload the new month with `--folder <month id> --new`.

### HARD RULE — EDIT SCOPE: change the named cells and nothing else (added 2026-09-12)

Brian's servicer-report instructions are surgical and literal. On 2026-09-11 he said *"paste it into the
servicer report. remove the trade date column"*; the edit also touched the payment-rate and charge-off
lines, and he came back with **"No, I told you to delete the trade date. The payment rate stays, so does
the charge off calc. Just relabel the column."** Three rules:

1. **A named-column instruction covers that column only.** "Delete the trade date column" is not licence
   to restructure the tab, re-derive a metric, or drop anything adjacent. If a second change looks
   obviously right, **say so in the reply and leave the workbook alone** — he decides.
2. **A LABELLING question gets a LABEL fix.** When a CIM/Aleja review question is about a column being
   mislabelled (e.g. the GPY BQ/BR headers, whose true defect is the `jp_fee` / `jp_disbursement` swap in
   `loc_tape`), relabel the header **at the report layer only** — headers changed, values, formulas and
   metric definitions untouched. Never delete or recompute a column to resolve a naming complaint.
3. **State the blast radius when you report done**: which cells/headers changed, and explicitly that no
   values or formulas moved. "Done — headers only, no values touched" is the shape of the answer.

### HARD RULE — Marisol's "confirm the number" asks (added 2026-09-08 after a five-message correction loop)

When Monex trust administration (**Lizbeth Marisol Caballero Quintana, lcaballero@monex.com.mx**) writes
on **F/12340** asking Jeeves to confirm a figure, the figure is the **Exhibit B average daily balance
(G10) for the report month** — NOT the EOP balance and NOT any tape total. Procedure, in ONE turn:
(1) open the workbook **you** sent her that month (it is in the CIM Reporting folder and attached to your
own sent mail); (2) read Exhibit B **G10**; (3) compare digit-for-digit to the number in her email;
(4) answer **"Yes, confirmed — <number>"** or **"No, our figure is <number>"** in one line, **reply-all**.
Do not restate methodology, do not offer an alternative metric, do not ask which balance she means, and
do not answer from a tape re-run when the sent file is the thing she is asking about. Precedent:
2026-08-11 she asked to confirm **1,226,804,057** (July average daily balance) and Brian replied "Yes, I
confirm"; 2026-09-08 the August figure was **1,305,967,747**. His escalation when this gets hedged
instead of answered: *"bro you sent the file. just look at it and see if it's the same number"* …
*"so confirm or not, why are we still talking about this"* … *"that's not the number in her email"*.

Write ONLY these cells: Exhibit A L6 (FX spot), L10:L25 (rollforward values), K31 (EOP account count);
Exhibit B H52 and H53 plus H58 and H59 (roll the prior month's cached H51, H52, H57, H58 forward),
G144 and G154 (interest period days = days in month), D36 (collection-account available amount - blank
it, Brian fills); bank_accts column C; and the tape and lista tabs. Everything else is a live formula
(L31, L33, L35, concentration block H40:L50, L56 from hedge, L83 from historical_draws, all Exhibit B
DQ SUMIFS). FX source that keeps the workbook internally consistent: reciprocal of
`loc_tape.spot_rate` at EOP (7-31-26 gives 17.3476); prior months used a slightly different market
print (June report 17.47 vs tape 17.4972), so always state which source was used.

Exhibit A L12 plus L13:L22 minus L25 does NOT tie to zero and never has (June 220.9M, July 69.7M gap).
It is a display block, not a check - do not try to fix it or report it as a build error. `repayment`
(Modified Receivables) came back identical to the cent two months running (37,299,580.41) - it is a
stock, not a flow; flag it rather than assuming a query bug.

Brian's standing fills (2026-08-10): bank balances = 0, no draws in a month with no new draw rows,
hedge MTM and collection-account available amount are his; he wants the FX spot filled by the agent.

### Cash balance — show BOTH dates (Alejandra Granados, CIM, 2026-08-27)

Aleja on the July 2026 MX MSR: *"Could you please confirm the cash balance as of July 31st? The
current report includes the cash as of the payment date. In future reports, please include the cash
balance for **both dates**, as both are needed for different purposes."*

**Standing requirement for every MX servicer report from August 2026 forward: two labelled cash
lines — cash as of PERIOD END (last calendar day of the reporting month) and cash as of the
PAYMENT DATE.** Never one or the other. The `bank_accts` tab is populated from the CICO email
closest to the target date, so this means sourcing TWO CICO snapshots per report, not one; pick the
CICO file closest to each date and label which file fed which line. If the two dates coincide, still
show both rows with the same figure rather than collapsing them.

### Format rule (confirmed 2026-07-10)

Output must be **in the same form as previous months** with **all formulas persisting** —
merge new-month data into the existing template (like the BB merge step), never rebuild
the workbook from scratch even if the underlying script only had to fix a data bug (e.g.
the BOP/EOP-only vs full-month-loop bug fixed 2026-07-08/09). Default to updating the
existing dated file in place only when correcting a known-bad prior version of the SAME
report date; a genuinely new period still gets a new dated file per the BB `--new` rule.

### Fee and payable lines — sweep EVERY representation, and check the counterparty's pending list

Booking a fee/payable change in one place is not the job. Brian corrected this twice on the
same artifact in one thread (2026-08-13, July 2026 period):

1. *Completeness.* "Did you add all the items we owe Monex to the single line? And the total
   amount we owe CxC?" - the month's newly-issued invoices are NOT the amount owed. Carried
   balances (A-19888 116,000 and A-19627 20,300, unpaid since June/July) existed only in the
   counterparty's own pending-invoice email (Gustavo Villarreal, "CXC Pending Invoices",
   2026-08-06). ALWAYS pull the counterparty's pending/aging summary and reconcile it against
   the invoice stream before writing any fee total, and list every invoice number that rolls
   into the figure so Brian can audit it.
2. *Propagation.* "put these in the waterfall too" - one figure appears in several places and
   all of them must move together. For the CIM MX monthly package the map is:
   Exhibit B **G128** and **G132** (CxC), **G133** (Trust/Monex fees), the source-note cells
   **Q35/Q36** (invoice detail), the **waterfall clause (iii)** text, **G127** servicing fee
   (formula-driven: (1%/12) x average daily balance - it moves when the expense block moves),
   clause **(x) remanente** (only computable once D36 is filled by Brian), AND the matching
   paragraph in the **Anexo J** Word file. After the edit, re-read each of those cells and
   report the before/after per location - never report "added" off a single write.

### No Excel on this host — replicate the workbook math in Python, then validate on the prior month

Excel COM automation does not work here: `xlwings`/`win32com` import fine and `xw.App()` starts, but
`app.books.open(...)` dies with `Open method of Workbooks class failed` (service session, no interactive
desktop), and LibreOffice is not installed. So an agent-built servicer report / BB carries NO cached
values — every formula reads `None` — and any figure needed for the Anexo J (which is plain text, not
formulas) has to be computed outside the workbook. Do NOT burn retries on Excel.

Working method (2026-08-14, July 2026 period):
1. Read the formulas you need with openpyxl (`data_only=False`); array formulas come back as
   `ArrayFormula` objects — use `.text`.
2. Re-implement them in Python straight off the `tape` tab in `read_only=True` mode (23.8k rows x 138
   cols loads in ~40s). Column letters map to the tape header: AC=`sofom_balance`, EF=`elig`,
   EA=`elig_nn`, CH=`credit_limit_usd`, CF=risk score, CI=`state_name`, CJ=`city_name`,
   CK=`naics_industry_id`, CL=`is_startup`, CC=onboarding date.
3. **Validate against the PRIOR month's file, which does have cached values** — recompute D90:D100,
   L35, L52, L81, N87, G36 and G47 from the June tape and require a to-the-cent match before you
   trust the current month. That check reproduced all 11 concentration cells and G47 = 38,108,614.19
   exactly, which is the only reason the July remanente could be signed off.
   Quirks the replication must honour: D92/D94 test against empty `D8` so their IF is always TRUE
   (1st largest, and sum of 2nd-4th largest, of eligible rows with `credit_limit_usd` < C90 = 2,000,000);
   D91/D93 are 0 whenever the eligible pool exceeds 40,000,000 x FX; D100 uses `TODAY()`, so state the
   as-of date you assumed.
4. Exhibit A L54 (collection-account cash, "Prerecycling") stays 0 per Brian; the borrowing base can
   therefore run a deficit that flows into the waterfall via G36 = D36 + `Exhibit A`!N87 (July: N87 =
   -8,716,170.42, June: -124,933,097.33). A deficiency is NOT booked into clause (vii) — G44 stays 0.
5. Tell Brian to open the workbook in Excel to materialise the values, and give him the computed
   waterfall in the message so he can check Excel against it.

### A change to the expense block silently invalidates the Anexo J servicing fee

G127 = (1%/12) x average daily balance − G128, so **every edit to G128/G132 moves clause (iv)**. On
2026-08-11 the July Anexo J was written with (iv) = MXN 950,416.71 (G128 = 71,920). The 8/13 CxC
correction raised G128 to 208,220 and nobody re-derived (iv) — the Word file sat wrong by exactly
136,300 until 8/14. Whenever the CxC/Monex/trust expense figures move, recompute
(1%/12) x G10 − G128 and rewrite clause (iv) in the Word file in the same pass. Cross-check:
(1%/12) x G10 must equal (iv) + G128 (July: 1,022,336.71 = 814,116.71 + 208,220).

### Compute the whole waterfall before you report any of it

The clauses are coupled — G127 = (1%/12) x G10 − G128 means any CxC move drags (iv),
and (x) is the residual so it moves when *anything* moves. Publishing clause-by-clause as
each figure lands therefore restates the answer every turn and destroys confidence in all of
it. On 2026-09-14 the August Anexo J was reported three times in one thread and four lines
changed underneath Brian: CxC 249,400 -> 303,920, Administrador 803,799.80 -> 749,279.80,
coberturas 122,040 -> 120,600, remanente 116,247,028.37 -> 116,271,513.55. Every one of those
was a legitimate correction; the defect was announcing before the set was closed.

Close the full set first — every clause sourced, the cross-check
`(1%/12) x G10 == (iv) + G128` passing, and (x) reconciled against the account balance —
then send ONE message with the complete table. If a figure genuinely cannot be sourced, say
which clause is blocked and on what, and report nothing else as final. Stay silent while
computing (SOUL: no mid-task progress narration).

### HARD RULE — every Anexo J input has a FILE. Never ask Brian for one (added 2026-09-15)

Escalation that produced this rule (2026-09-14): *"1. You have the fucking valuation report with the
premiums and the trade dates, so you know what was paid in Aug. 2. You have the invoice so you know
the interest. Why is this so fucking incompetent"* and *"trade date is in the file that's in drive"*.
Source map — pull these BEFORE composing anything, and never ask him to supply them:

| Anexo J input | Where it lives |
|---|---|
| Hedge premiums **and trade dates** (clause for coberturas) | the JPM valuation workbook Brian dropped in Drive `Debt/CIM/Reporting/{YYYYMM}/` (e.g. `VALUATIONS_20260911225702.xlsx`) — it has a **Trade Date** column; only trades dated inside the report month were paid in that month |
| Monex / trust fees | the **honorarios invoice PDF attached to Marisol's own email** — download it (`gmail_tool.py download <message_id>`) and read it |
| MX interest invoice | **Sam McNulty (Ocorian)**, subject *"External confirmation required: Jeeves (Mexico) Interest Calculation / Invoice for {Month} {YYYY}"* — NOT from CIM directly |
| Monex trustee facturas / ISR / CxC facturas | `facturacionpf@monex.com.mx` (FACTURA###BCO); ISR payslip from Roberto Paez (`robertoconta@`, *"JVS Soluciones \| Withholding tax payment"*); CxC facturas from `reportes-cxc@cxc.com.mx` (*"Tecnologia en CXC - Factura"*, ZIP of CFDI XMLs — folios may cite F/12339 even when paid from F/12340) |
| CxC / servicing expenses | the workbook expense block (G128/G132) — see the clause (iv) coupling rule above |
| F/12340 account balance used to reconcile clause (x) | the balance posted in Slack **#ops-mx-spv**, or Marisol's stated trust balance in the thread |

**Shortfall procedure (precedent 2026-09-14):** if the clauses sum to more than the trust balance,
trim **clause (x) remanente by exactly the shortfall** and say so in one line — August: payments
MXN$131,703,909.95 vs balance MXN$131,586,670.83 at ~17.15, shortfall **MXN$117,239.12**, so (x)
went 116,271,513.55 -> **116,154,274.43**. Do not re-scale the other clauses.

**FILE THE LETTER.** The final Anexo J `.docx` goes to Drive `Debt/CIM/Reporting/{YYYYMM}/` in the
same pass as the send — Brian, 2026-09-14: *"Anexo J is not in drive where it belongs."* Sending it
from a local temp path and leaving Drive empty is an incomplete deliverable.

**Once he has authorised the send, SEND.** *"you have all the invoices, do it"* then *"no, just send,
don't ask for follow up"* — a second confirmation question on an already-authorised send is the defect.
Every peso amount must be filled before it goes (unfilled amounts hard-fail in `gmail_tool`).

### Payment Rate — do NOT use a DPD-bucket stock proxy for a single bill's amount

CIM's contractual Payment Rate is a **flow** definition (collections $ as % of opening/
billed balance per the Borrowing Base/Compliance Certificate template), not a snapshot
DPD-bucket ratio. A proxy like `1 - (1-30 DPD balance)/(Current + 1-30 DPD balance)` is
invalid for amounts tied to *one specific month's bill*: DPD ages per-invoice/per-billing-
cycle, so a charge from last month's bill can only ever be 1-30 DPD — it cannot reach 31+
DPD while still being defined as "last month's charge." Before proposing any DPD-based
proxy for a flow metric, check whether the bucket can even reach the range the proxy
assumes for a single billing cycle. (Open follow-on as of 2026-07-10: whether "Current"
bucket contamination from new unbilled spend still breaks a corrected proxy attempt —
unresolved, needs the CIM 3-month trailing grid built from actual flow data instead.)




<!-- Evolution: 2026-07-29 | source: self-improving-agent -->
NB Borrowing Base Certificate


<!-- Evolution: 2026-07-29 | source: dream-74 -->

---

## NB Monthly Reporting (the 14th) — `nb_monthly_reporting_cron.py`

**HARD RULE — signature/status tracking lives ONLY in `check_cert_watches()` + `_nb_cert_watch.json` (added 2026-09-24).** Never write a standalone watcher script for a Box Sign or lender send. Before ANY "still not signed / still unsent" alert or reply: (1) `find_executed_cert()` on the signed area, (2) Gmail `in:sent (to:nb.com OR to:wilmingtontrust.com)` for the subject, (3) the watch entry status. An orphan watcher timed out and told Brian the July certification was missing 22h after Alex signed and Brian had sent the package; the first correction reply then repeated the error by skipping in:sent.

Scope set by Brian 2026-09-14, from the executed NB Credit Agreement, Article VI. The cron fires on
the **14th of each month** (`NB_MONTHLY_DAY` / `NB_MONTHLY_HOUR`).

| Section | Deliverable | Due | Who produces it |
|---|---|---|---|
| **6.01(c)** | Monthly unaudited consolidated financials | 40 days after month-end (30 days from the month ending 2027-03-31) | **Accounting, not Capital Markets** — Kacper Chrobot prepares, Shalom Hassan reviews, Alexander Melikian approves |
| **6.02(b)(y)** | Month-end **Borrowing Base Certificate** (Exhibit G) | 15 Business Days after month-end | Capital Markets |
| **6.02(f)** | Month-end **cash report** (Liquidity of the Loan Parties) | concurrently with the monthly BBC; e-mail delivery permitted | Capital Markets |

**The financials are CHASED, never built.** Brian: *"skip the financials."* If the prior month's
package has not arrived by the 14th, the cron **drafts** (never sends) a reminder to Shalom / Alex /
Kacper and DMs Brian the verbatim draft for approval.

**The month-end BBC is a variant of the weekly NB borrowing base, not a second product.** The
workbook itself comes from the weekly `build_nb.py` pipeline in `jeeves-borrowing-base`; what makes
the month-end one different is the extra certifications it must carry:

- account opened/closed delta for the month,
- Effective Advance Rate of each SPV Financing,
- the Immaterial Subsidiary threshold test.

**Built by `jeeves-borrowing-base/nb_certifications.py` (2026-09-23)** -- renders the full Exhibit G (items 1-9, Exhibits A-C, Schedules 1-3) from `.deer-flow/nb_cert_config.json` (signer Alexander Melikian, CFO; SPV = Mexican Warehouse Facility; Schedule 1.01(b) subs) and per-period `.deer-flow/nb_cert_inputs.json` (officer confirmations, Accounting's Immaterial Subsidiary figures). EAR = SOFOM master historical_draws minus config repayments / sum eligible_balance_usd at as-of. Exhibit B diffs bank_accts vs the prior month-end snapshot (`--save-snapshot` after delivery), else closing Schedule 5.22. Anything unsourced -> PENDING -> no draft. Weekly BBCs get the items 1-6 certificate from report_scheduler_cron.

This cron exists so the month-end variant is never silently skipped behind the weekly one. When the
14th falls in the same week as a Monday NB run, they are still two separate obligations.

### Full-package schedule (added 2026-09-23 — Brian: "I want all of it done before the requirement")

`run_schedule()` in the same cron, hourly tick, drafts only:

| Milestone | Target | Contract due |
|---|---|---|
| 6.02(b)(y) month-end BBC + 6.02(f) cash report | BD+3 (`NB_ME_BBC_BD`), as of month end, month-end CICO email (`YYMMDD CICO` of last BD) via `run_nb_bb(variant='month_end')`; fail-closed covenant gate; agent adds certifications + draft | BD+15 |
| 6.01(c) package (NB workbook + officer cert → Box → `_nb_cert_watch.json`) | the day Accounting's package lands (daily Gmail probe from ME+5) | ME+40 |
| 6.01(c) escalation draft to Kacper/Shalom/Alex | due − 10 days — **REJECTED by Brian 2026-09-30** (Aug draft built 9/30 for a 10/10 due date: *"No it's too early, get rid of it"*, draft deleted). Until Brian picks a new lead time in the cron, do NOT build this draft; the due − 3 BD deadline guard DM below is the only nudge | ME+40 |
| Deadline guard DM to Brian | daily from due − 3 BD while Gmail Sent lacks it | — |

Manual: `nb_monthly_reporting_cron.py month-end-bbc --period YYYY-MM-DD`, `dates`, `guard`, `status`. Month-end subject: `Jeeves Monthly Borrowing Base Certificate - As of {Month D, YYYY}`. build_nb refuses to zero-fill an unmatched Schedule 5.22 row — a month-end CICO file missing an account is a hard stop, not a default.

### HARD RULE — 6.01(c) certification mechanics, defined-term cites, and the outbound draft (added 2026-09-16 after three follow-ups in one thread on 2026-09-15)

Three things Brian had to ask for in a single thread about Kacper's Jul-2026 monthly package:

1. **Certification, not a certificate form.** Monthly financials under 6.01(c) carry **no Exhibit C** —
   6.02(a) attaches the Compliance Certificate only to 6.01(a) and 6.01(b) (annual and quarterly).
   The monthly needs a **one-paragraph certification signed by a Responsible Officer**: CEO, president,
   CFO, COO, treasurer, chief compliance officer, general counsel or secretary. **A Controller is NOT a
   Responsible Officer** unless designated in writing to the Administrative Agent, so Kacper's package
   needs Alexander's signature. **The officer signs the certification, not every financial statement
   page** (Brian: *"Does he need to sign the financials or just the certificate?"*). Monthly covenant
   compliance rides on the **Exhibit G borrowing base certificate**, not on the financials.
2. **Every defined term carries its agreement and section cite, and you never assert a term is absent.**
   NB **defines** Consolidated Adjusted EBITDA (it is the 7.01(c) EBITDA-coverage input); the
   **free-cash-flow proxy is a CIM definition**. Do not attribute one agreement's defined term to the
   other and do not say a term is missing until you have read that agreement's definitions article
   (Brian: *"the free cash flow proxy on the P&L is a definition in the CIM agreement. Are you telling
   me there's no definition of EBITDA in NB?"*).
3. **A compliance read is not delivered until the outbound draft exists.** When Brian asks whether
   someone's financials satisfy a covenant, the same pass produces the reply draft on the actual
   thread, with the verbatim text in Slack — he should never have to say *"can you just reply to
   Alex's email to Kacper with your findings"*.

**HARD RULE — the period Brian names applies to the WHOLE set, and the advance rates are yours
(added 2026-09-15).** Two corrections in one thread on 2026-09-14: *"are the other deliverables
done?"* (one deliverable of the set was built, the rest were described) and *"didn't I say 'do
august'?"* (the run defaulted to the current month after he had named August). So: when he names a
period, produce **every** deliverable in the table for **that** period in the same pass, and report
them together with links. And the **Effective Advance Rate of each SPV Financing is the agent's to
derive** — Brian: *"you own the spv advance rates"* — compute them from the SPV facility documents
and the tape (CIM MX SOFOM, MX SPV LSA), never ask him for the number.

---

## Rules

- Always state the date range used in your response
- Never modify existing files on Drive — create new dated copies
- Remind user to open in Excel for formula recalculation
- Follow naming: `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx` (US), `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx` (MX), `Portfolio Report - {YYYYMM}01.xlsx` (monthly)
- **When Brian says "use the most recent tape available" (a tape runs daily), do NOT reuse a cached/earlier-session tape file.** Check the actual latest tape by file timestamp in the relevant Drive/output folder (or the cron's last-run date in its state JSON) before running any calc against it, and state that date explicitly. Corrected 2026-07-11 (NB BB thread) after a stale tape was used despite a same-day tape existing.



<!-- Evolution: 2026-07-11 | source: self-improving-agent -->

---

## HARD RULE — "use X as a starting point" means CLONE X, not redesign

When Brian names an existing artifact as the starting point (e.g. "use the CIM bridge
version"), **open that actual file and edit a copy of it**. Keep tab names, row order,
the label column, column positions and the visual skeleton. "Trimmed down" means
*delete rows/criteria that do not apply*, never re-architect the layout. Cost 87 turns
and three corrections on the NB borrowing base certificate (2026-07-28) — the first
draft was structurally original and Brian rejected it with "this doesn't look anything
like the CIM version. I want a familiar format." Before delivering, diff your output
against the source form tab-by-tab and be able to say what you deleted and nothing else.

---

## When a scheduled report fails: FIX IT, RE-RUN IT, DELIVER IT — never just diagnose (2026-08-17)

Brian's escalation "i asked you to do this" came 3 minutes after a clean, correct root-cause writeup
of the 15:22 NB failure. A diagnosis is not a deliverable. On any scheduler alert: patch the defect,
re-run the job, produce the workbook, upload it, and report the numbers — in one turn, without asking.

And when you patch one step of a pipeline, **sweep the whole chain before saying done**: data source →
math → template selection → output filename → Drive folder → upload flags → the Slack/email delivery.
On 2026-08-17 the truncation bug was fixed and reported as done, and Brian then had to ask two more
questions one at a time — "and is it writing to the right folder?" and "no it should always use the most
recent as a template" — both of which were inspectable in the same file at the same time. Three
round-trips for one fix is the failure mode; enumerate and verify every link before reporting.

---

## `--new` is REQUIRED on EVERY dated NB/CIM upload, not just the MX master (2026-08-07)

Uploading `Jeeves NB Borrowing Base - 20260807.xlsx` with only `--folder` made the uploader
strip the date stamp, match the prior day's `- 20260806.xlsx` in the same folder, and UPDATE it
in place - renaming it to 20260807 and destroying the 8/6 snapshot (printed "Action: UPDATED
existing file"). The date-stamp-stripping match is not MX-specific; it fires for any
`Jeeves NB Borrowing Base - {YYYYMMDD}.xlsx` sibling. Always pass `--new` for a new dated
snapshot, and read the "Action:" line - "UPDATED existing file" on what should be a new
snapshot means you just overwrote yesterday's file. Recovery: re-upload the prior day's local
copy with `--new` (it gets a NEW file id, so any previously shared link to the old id now
points at the newer file - tell Brian).

---

## Editing existing lender workbooks with openpyxl - hard rules

- **Never assign to `.value` without a MergedCell guard.** Clearing or rewriting a
  rectangular range in a formatted lender form hits merged header/label cells and raises
  `AttributeError: MergedCell object attribute value is read-only`, which aborts the sweep
  mid-loop and leaves the workbook half-edited (cost several retries on the NB BB form,
  2026-08-04). Pattern:

  ```python
  from openpyxl.cell.cell import MergedCell
  c = ws.cell(row=r, column=col)
  if isinstance(c, MergedCell):
      continue          # write only to the top-left anchor of the merged range
  c.value = new_value
  ```

  Enumerate `ws.merged_cells.ranges` once up front and target anchors only.

- **HARD RULE — after ANY programmatic edit to a lender workbook, OPEN IT AND VALIDATE IT
  before saying it is done (Brian, 2026-08-27, July MX servicer report).** His words:
  *"when you made changes historically you've created some circular references. Have you
  opened the file and looked for any Excel errors?"* Writing cells with openpyxl does not
  recalculate anything and does not tell you what you broke: a formula whose range you
  shifted can now point at itself, at a deleted row, or at a blank. The edit is not the
  deliverable — the validated workbook is. Run this sweep over EVERY sheet, every time,
  and report the counts in the completion message:

  1. **Error literals** — scan all cached values (`data_only=True` load) and all formula
     strings for `#REF!`, `#VALUE!`, `#DIV/0!`, `#NAME?`, `#N/A`, `#NULL!`, `#NUM!`.
  2. **Circular references** — for each formula cell, parse the referenced ranges and
     assert the cell's own coordinate is not inside its own dependency closure. A one-hop
     self-reference (`H12 = SUM(H10:H14)`) is the common openpyxl-induced case.
  3. **Dangling / out-of-bounds refs** — references past `ws.max_row`/`ws.max_column`, or
     to a sheet name that no longer exists in `wb.sheetnames`.
  4. **Zero/blank regression** — compare the edited totals against the prior period's file.
     A line that was non-zero last month and is now blank or 0 is a broken formula until
     proven otherwise, not a real movement.

  There is no Excel on this host, so `data_only=True` returns the values Excel last cached —
  a cell edited this run has `None` there. That is expected; what is NOT acceptable is
  skipping the check because of it. Validate the formula strings even when the cached
  values are unavailable, and say in the report which of the four checks you actually ran.


- **Any workspace script importing a skill module needs the path insert as its first
  statement.** Hit twice on 2026-08-04 (`find_parent_bb.py` and the Vista funds-flow
  script) as `ModuleNotFoundError: No module named google_auth`:

  **The snippet below was WRONG until 2026-08-08 and that is why this error kept recurring
  (5th time on 2026-08-07, `inspect_nb.py`).** `google_auth.py` does NOT live in the
  `google-drive` skill dir - it lives in `custom/_shared/`, and its only export is
  `get_credentials(required=True)`. There is no `get_drive_service`. Correct form:

  ```python
  import os, sys
  sys.path.insert(0, os.path.join(os.environ["SKILLS_PATH"], "custom", "_shared"))
  from google_auth import get_credentials
  from googleapiclient.discovery import build
  drive = build("drive", "v3", credentials=get_credentials())
  ```

  Better still, do not write the script at all: to pull a Drive file down for inspection run
  `python C:/Jeeves/redshift-bot/deer-flow/skills/custom/google-drive/download_file.py <file_id_or_url> --output-dir "$WORKSPACE_PATH"`.

  Before writing any new Drive script, check the existing helpers first:
  `list_drive_folder.py`, `fetch_doc.py`, `upload_to_drive.py`, `get_comments.py`.

- **Keep host paths and bare slash-tokens out of bash commands entirely.** A Windows path,
  a WSL-style mount path, or even a filename written with a leading slash makes the string
  guard reject the whole command - no variant will pass. Hardcode the real path inside a
  script written with `write_file` and run it with `$PYTHON_PATH`. Files Brian attaches in
  Slack are already saved locally under the `slack_downloads` folder in `.deer-flow` as
  `YYYYMMDD_HHMMSS_<original name>`; reach them from python, never by globbing in bash.





<!-- Evolution: 2026-08-19 | source: self-improving-agent -->

