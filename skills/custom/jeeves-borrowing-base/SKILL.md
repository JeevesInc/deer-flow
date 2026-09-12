---
name: jeeves-borrowing-base
description: Use this skill when the user asks to build, run, or update the borrowing base, data tape, or portfolio report. Covers US (Bridge), MX (SOFOM), and end-of-month portfolio reports. Also triggers for "run tape", "build the BB", "update the borrowing base", or "generate the data tape."
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Borrowing Base & Portfolio Report Pipeline

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


## ⚠️ HARD RULES — scheduler edits and completion truth (added 2026-08-10)

0. **New or changed SQL born in this skill belongs in cfo-org-kb, and you CANNOT write it there with `write_file`/`str_replace` or with `skill_manage.py patch`.** Both fail: the clone is permission-denied to the write tools, and `skill_manage.py` edits the local clone with no git push, silently breaking the "GitHub is the only store, commit every query" rule. **Pattern:** write a python script to `WORKSPACE_PATH` that does plain `open().write()` into the clone, then `subprocess` the git add / commit / push with `cwd` set to the clone dir, and run it with `uv run python`. This is already a documented HARD RULE in cfo-org-kb/SKILL.md that recurred a 6th time on 2026-08-19 (Permission denied writing the new `nb_dq30_monthend_detail.sql`) because the work started HERE, in the borrowing-base skill, and cfo-org-kb/SKILL.md was never loaded. Read that rule before touching any .sql under cfo-org-kb.

1. **Editing `report_scheduler_cron.py` does NOT take effect until the gateway restarts.** The cron is
   a daemon thread inside the gateway and the module is imported once at process start, so a patched
   file on disk is inert while the old code keeps running in memory. On 2026-08-09 three patches were
   applied at 18:43 to a scheduler that started at 15:48 — none were live, and behaviour did not change
   for the rest of the evening. **After patching it, tell Brian a restart is needed, or verify the new
   code path actually executed in `deerflow-gateway.err.log`.** Scripts invoked as subprocesses
   (`build_mx.py`, `merge_template.py`, `zscaler-reauth/reauth.py`) DO pick up edits immediately —
   only the long-running module is affected.

2. **`_report_scheduler_state.json` is not proof the build ran.** Verify the artifact:
   `deer-flow\skills\custom\jeeves-borrowing-base\outputs\tape_sofom_daterange_<start>_<end>.xlsx`.
   On 2026-08-09 state read `last_mx_bb: 2026-08-09` while the newest tape on disk was
   `20260804_20260806` (built 2026-08-07) — the 8/8 and 8/9 MX builds never produced a file.
   Downstream gates (`_wait_for_bb_pipeline`, morning brief, CICO dashboard) treat a bare `<today>`
   value as "data is ready", so a false completion silently serves stale numbers.

3. **"The NB borrowing base didn't run" from Brian means the EMAIL, not the build — and `run_nb_bb` has no
   email step (verified in source 2026-08-26; `report_scheduler_cron.py` unchanged since 2026-08-17 16:22).**
   The job does build → upload → `_post_slack` → done. On 2026-08-24 it succeeded end-to-end at 09:22-09:27 PT
   (Drive id `1rkjOCGxIVC5v_Qr-4FiL0X2NN-RjKV0t`, state `2026-08-24` completed) and Brian still reported it as
   not run, because NB and Wilmington Trust received nothing. **Never answer a "did it run?" question from the
   state marker or the green Slack post — check Gmail `in:sent` for that week's thread.** Distribution config
   of record is `.deer-flow/nb_bb_distribution.json` (To: Max Fernandez, Jay Berger, Varun Surapaneni, Joseph
   Feil, J'Lynn Drennon; Cc: NBSF_Ops, NBSF_Finance, NB_ABC_Finance; **new thread every week**, subject
   `Jeeves Weekly Borrowing Base - As of {prior Sunday}`) — it is CONFIG ONLY, nothing reads it yet.
   Also: the uploaded workbook keeps the template's prefix (`Jeeves NB Borrowing Base - Form - {stamp}.xlsx`),
   not the `Jeeves NB Borrowing Base - {YYYYMMDD}.xlsx` name of record — rename before sending externally.

4. **The scheduler polls hourly, so a "08:30 PT" job fires at whatever tick follows** — 2026-08-24 fired at
   09:22:20. Do not tell Brian a job "didn't fire at 8:30"; find the tick in `deerflow-gateway.err.log` first.

5. **State value semantics:** `<today>` = completed, `<today>_running` = in flight, `<today>_failed` =
   fired and errored (stays failed until re-run by hand). `_state_completed_today` blocks re-fire on
   all three, so a stuck `_running` or `_failed` value must be cleared by hand to re-run same-day.

---

## ⚠️ SOFOM Distribution Tape (Daily — F/12339 > F/12340)

When asked to run the SOFOM distribution tape for Axel's daily distribution email, the file to generate and attach is **NOT** the Master BB. It is the **raw tape**:

- **Filename:** `tape_sofom_daterange_{start_YYYYMMDD}_{end_YYYYMMDD}.xlsx`
- **How:** Run `data_tape_sofom.sql` (one query per day using `'{}'\ format placeholder) for each of the 3 days: yesterday-2, yesterday-1, yesterday. Concat all rows. Save as above.
- **Draft reply:** Gmail message ID from Axel's email, body = "tape attached, please review", attach the raw tape file.
- **Never** attach `Jeeves SOFOM Borrowing Base - Master - *.xlsx` to this thread. That is a separate product.

**IT IS AUTOMATIC — do not tell Brian it needs a manual run (he corrected exactly that, 2026-08-17:
"That's not true. It's supposed be triggered by an email with the subject F12340 distribution and
today's date").** `report_scheduler_cron.py` polls Gmail hourly on weekdays from 08:00 PT
(`_find_sofom_distribution_email`): subject must match `F/12339 > F/12340 Distribution`, dated today,
ANY sender (not pinned to Axel), and it replies to the **OLDEST** match in the thread, never a later
reply-all. State key `last_sofom_distribution`; a `_failed` value here IS retried on the next tick
(unlike `last_nb_bb`). So "why hasn't the distribution run today?" is always one of four things, in
this order: (1) no matching subject/date email has arrived yet, (2) the gateway is running pre-patch
code (compare module mtime to the last "Report scheduler started" line), (3) the tape step failed —
on 2026-08-17 it was **Redshift unreachable and reauth did not restore it**, so the run died before the
draft, or (4) state already says today. Check the state file and the gateway log before answering; never
answer this question from the skill's prose alone.

---

## Request Router

Match the user's keywords to the correct pipeline. These are three DIFFERENT products — never substitute one for another.

| Keywords (any match) | Pipeline | Script | Date param | Upload folder |
|---|---|---|---|---|
| **monthly tested** portfolio report, tested portfolio report, CIM monthly report, CIM report ("for CIM") | **CIM Monthly Tested Portfolio Report** (see its own section below) | `build_us.py --date {month-end}` then merge 5 tabs into prior-month CIM file | month-end of the covered month | `Debt/CIM/Reporting/{YYYYMM}/` |
| portfolio report, portfolio reporting, monthly report, run reporting | **Portfolio Report** | `build_portfolio_report.py` | `--date YYYY-MM-01` (1st of month) | `Portfolio Reporting/{YYYYMM}/` |

**Router precedence (Brian corrected this 2026-08-04, cost ~70 turns):** the words **"tested"** or **"CIM"** anywhere in the request override every other match -- that is the 12-tab CIM Monthly Tested Portfolio Report, NOT `build_portfolio_report.py`. "Monthly tested portfolio report" and "portfolio report" are two different deliverables to two different audiences; never treat the longer phrase as a verbose way of saying the shorter one. If the request is ambiguous between the two, ask before building -- building the wrong one wastes an hour.
| borrowing base, BB, run the tape, US BB, bridge BB | **US Borrowing Base** | `build_us.py` → `merge_template.py` | `--date YYYY-MM-DD` (snapshot date, default yesterday) | `Debt/CIM/{YYYYMM}/US/` |
| SOFOM, MX BB, MX borrowing base, mexico BB | **MX Borrowing Base** | `build_mx.py` → `merge_template.py` | `--end-date YYYY-MM-DD` (default yesterday) | `Debt/CIM/{YYYYMM}/MX/` |

**Date rules:**
- Portfolio report: "4/1 portfolio reporting" → `--date 2026-04-01`. Script derives EOP (3/31) and BOP (2/28) automatically.
- Borrowing base: "run the BB" with no date → use yesterday. "BB for 3/27" → `--date 2026-03-27`.
- **Never use today's date.** Always use yesterday's date as the snapshot — even on Mondays (Sunday data is available). Do NOT fall back to last Friday on weekends. Scripts hard-reject today/future.

---

## Monthly Portfolio Report

The portfolio report is a 7-tab workbook with formula-driven Summary dashboards. It is NOT raw data — it uses a template from the previous month. **If the user asks for "portfolio reporting", THIS is what they want — not borrowing bases.**

**Template structure:**

| Tab | Type | Description |
|-----|------|-------------|
| **Summary** | Formulas | Dashboard: UPB, DQ buckets, charge-offs, receivable rollforward, MoM comparison (col L=current, col N=prior) |
| **Summary (2)** | Formulas | Second summary view |
| **Country** | Formulas | DQ by country (Mexico, Colombia, Brazil) |
| **loc** | Data | LOC tape at EOM |
| **rollforward** | Data | Balance rollforward BOP→EOP |
| **mods** | Data | GWC repayment plans (`loan_reference_number LIKE 'RPP%'`, ~95 rows) |
| **loans** | Formulas | Loan-level summary |

### Step 1: Find a valid template (previous month's report WITH formulas)

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

Navigate to the most recent month folder and find `Portfolio Reporting - {YYYYMM}01.xlsx`.

**IMPORTANT:** The template MUST be a real report named **`Portfolio Reporting - {YYYYMM}01.xlsx`** that contains Summary formula tabs (Summary, Summary (2), Country, loans). Do NOT use files named `Portfolio Report - *.xlsx` (no "ing") — those are broken raw-data files the bot generated incorrectly. A valid template has 7 tabs with formulas in the Summary sheets. Currently, the last known valid template is `Portfolio Reporting - 20260301.xlsx` in `202603/` (file ID: `1tQ4JwCHJHP2U4-k0iiFwalyDY0KzO5PC`).

**Summary tie-out fix (applied 2026-08-03 — use the 20260801 file as the template going forward, file ID `11fEFPPxowwGWM6G9OWbRisIrvEvj-Cxa` in `202608/`).** Every template through 20260701 had a broken Summary!L56 Check because the rollforward buildup mis-mapped three columns: L45 (FX) summed `rollforward!O:P`, which double-counted `adjustment_amount_usd` (col P) already picked up on L46, while `jeeves_pay_fee_amount_usd` (col K) and `fx_adjustment_amount_usd` (col N) were never picked up at all. Result: L56 = -SUM(P), i.e. -$3,943,575.18 in the July report (only -$44,609 in June, because June adjustments were small — the bug scales with adjustments). Correct formulas: L37 `=SUM(rollforward!H:H) + SUM(rollforward!K:K)`, L45 `=SUM(rollforward!N:N) + SUM(rollforward!O:O)`, L46 unchanged. Then L50 = SUM(rollforward!T:T) and L56 = 0.00. If you ever build off a pre-August template, re-apply these three edits. Row-level integrity check: rollforward col X (diff) should sum to 0 and V should equal W.

**Apply those three fixes to the `Summary` tab ONLY.** `Summary (2)` uses different row semantics (L37 = "(+) Jeeves Pay Interest" `=SUM(rollforward!J:J)`, L45 = "Check" `=SUM(rollforward!P:P) - L42`) and also carries pre-existing `#REF!`/`#ERROR!` cells inherited from the Google-Sheets-era template. Blind row-number patching clobbers it (caught and reverted 2026-08-04).

**Cadence (Brian, 2026-08-04):** the report is **due the 15th of the month and covers the prior month**. So `Portfolio Report - {YYYYMM}01.xlsx` holds the **previous** month data - `20260701` = the June report delivered 7/15, `20260801` = the July report due 8/15. When Brian asks for "the July report" after month end that means BOP 6/30 -> EOP 7/31, i.e. `--date 2026-08-01`, template = the **June** file. He may ask for the filename to carry the run date instead (`Portfolio Report - 20260804.xlsx`) for an interim/pre-delivery cut - build with `--date {first of following month}` and rename the output; never set `--date` to today (that shifts the period to the partial current month).

### Step 2: Run the builder

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_portfolio_report.py --date 2026-04-01 --template-id <PREVIOUS_REPORT_FILE_ID>
```

`--date` is the report date (1st of month). The script automatically derives EOP=3/31 and BOP=2/28. It queries LOC tape, rollforward, GWC mods (RPP% filter); downloads previous report as template; copies col L → col N in Summary tabs (MoM); replaces data tabs; saves as `Portfolio Report - 20260401.xlsx`.

### Step 3: Upload to the correct month folder

The folder matches the **report date month**. For `--date 2026-04-01`, upload to `Portfolio Reporting/202604/`. For `--date 2026-03-01`, upload to `Portfolio Reporting/202603/`.

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

Find the `{YYYYMM}/` folder matching the report month, then upload:

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Portfolio Report - {YYYYMM}01.xlsx" --folder "<MONTH_FOLDER_ID>"
```

Remind user to **open in Excel** to recalculate formulas.

### Step 2b (MANDATORY): the builder's MoM shift is a NO-OP — write col N yourself

`_shift_summary_mom_with_values` copies **cached** col-L values into col N. The templates on
Drive were written by openpyxl and never opened in Excel, so `Summary` has **0 cached col-L
values** (only `Summary (2)` has 4). Verified on the 20260801 AND 20260804 templates,
2026-09-01. Consequence if you ignore it: col N (prior period) silently keeps the
month-BEFORE-last's numbers, and worse, **`Summary!N4` drives the whole period header** —
`L4 = EOMONTH(N4,0)+1`, `L5 = EOMONTH(L4,0)` — so a stale N4 labels the report with the wrong
month and mis-filters rows 22-25 and 69. The builder never touches N4/N5.

After running `build_portfolio_report.py`, always:
1. Set `Summary!N4` = first day of the PRIOR period, `N5` = its month end (for the 20260901
   report: 2026-07-01 / 2026-07-31).
2. Recompute the prior period's col-L values in Python **from the template workbook's own
   loc / rollforward / mods tabs** and hard-code them into col N: rows 9-15 (Country F4:F9
   buckets off `loc`: `balance_usd>0`, `charge_off_flag=FALSE`, `is_in_repayment=FALSE`,
   `country_code` 484 MX / 170 CO / 76 BR, `days_past_due` buckets 0 / 1-30 / 30-60 / 60-90 /
   90-120 / 120+), 17, 21-25 (`loans`), 30-56 (`rollforward`), 53-55 (country totals),
   60 and 62-69 (`mods`; CY=balance_usd, DU=uninvoiced_interest_usd, DV=uninvoiced_vat_fee_tax_usd,
   EE=dq_bucket_daily, EK=charge_off_dt, EL=charge_off_flag, ER=is_out_of_policy_repayment).
3. **Two tie-outs must both hold before upload** — `SUM(rollforward!T:T) - L50 = 0.00` (L56) and
   the `loc` DQ-bucket total = `L52` (EOP excl. negative balances) **to the cent**. Both held
   for August 2026 at $224,219,947.93.

Known, pre-existing and NOT introduced by the build: 6 error literals on `Summary (2)`
(`L5 =loc!#REF!` plus five `ARRAY_CONSTRAIN`/`ARRAYFORMULA` Google-Sheets leftovers). `Summary`
itself is clean. Also: **the builder does NOT refresh the `loans` tab** (nor `Sheet1`) — it
carries the prior month's rows, which is why Summary rows 21-25 read 0.00 every month.

`Portfolio Reporting/202609/` folder id `1x4_CJ566Lx8ntLIPZTp9ymJ8ZmThkLZb` (created 2026-09-01).
Template of record for the September build (August period) is now
`Portfolio Report - 20260901.xlsx`, id `1ys-RpbS9XwoKPB725ZxZRliY7_mTG5dl`.

---

## CIM Monthly Tested Portfolio Report (due the 15th, covers prior month)

**Different product from the Portfolio Report.** Lives in `Capital Markets/Debt/CIM/Reporting/{YYYYMM}/` as `Monthly Tested Portfolio Report - {YYYYMMDD}.xlsx` (date = delivery date, e.g. `20260715` = the June report). 12 tabs: Summary, historical_draws, elig, tape_start, tape_end, rollforward, revenue, bank_accts, br_mc_liability, Monthly Portfolio Report, Borrowing Base, eligibility_summary. Recipient is CELF 30-I LLC; the two letter tabs date themselves with `=TEXT(TODAY(),"mmmm dd, yyyy")` and Summary Begin/End derive from `tape_start!B2 + 1` / `tape_end!B2`, so dates need no manual edit.

Build: `build_us.py --date {month-end}` (BOP = prior month-end automatically) then merge tape_start / tape_end / rollforward (index col in A) and eligibility_summary / revenue (no index col) into the **prior month file as template**, preserving all other tabs.

- **revenue tab** = `select company_id, sum(revenue_usd) from master_transactions_dm.transactions_ssot where posted_at in month and is_company_test = false group by 1` (Brian, 2026-08-04). Revenue is ONLY in transactions_ssot -- a loc_tape fee-sum reconstruction came in 36% light. Back-test against the prior month tab before trusting it (June back-test: $5,279,312 vs $5,249,646 in the delivered file, +0.57% from restatement).
- **br_mc_liability** (25-day spend, mc_collateral) carries forward month to month; confirm with Brian rather than deriving.
- **bank_accts** is NOT the Bridge BB bank_accts row set -- 14 rows (Brazil 6540; Colombia 8116/1681/4511/0337/4765/2865/JSE233/0850; Mexico 5592/4380/0007; US 3601/6167) and `cico_fetch_balances.build_bank_accts_df` maps a *different* Brazil set (BS2/BTG/QiTech), so do not drop its frame in. The 7/31/2026 CICO email screenshots covered only 9 of the 14. Values in this tab are USD-converted despite the `currency` column labels (US 3601 template June 459,683 ties exactly to the workbook 6/30 original-currency USD row; COP/MXN rows are the workbook figure divided by an FX rate) -- confirm units and balance date with Brian before populating.

## US Borrowing Base (Bridge)

### 1. Generate data

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_us.py --date 2026-03-27
```

Defaults to yesterday. Output: `$OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx` (4 tabs: tape_start, tape_end, rollforward, eligibility_summary)

### 2. Find template + merge

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU" --recursive --max-depth 3
```

Find latest `Jeeves Bridge Borrowing Base - *.xlsx` in `CIM/{YYYYMM}/US/`. Then:

```bash
python /mnt/skills/custom/jeeves-borrowing-base/merge_template.py "$OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx" "<TEMPLATE_DRIVE_ID>"
```

### 3. Upload to `Debt/CIM/{YYYYMM}/US/`

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx" --folder "<CIM_US_FOLDER_ID>"
```

---

## MX Borrowing Base (SOFOM)

### 1. Generate data

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_mx.py --start-date 2026-03-25 --end-date 2026-03-27
```

Defaults to 3-day range ending yesterday. Output: `$OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx` (1 tab: tape)

### 2. Find template + merge

Find latest `Jeeves SOFOM Borrowing Base - Master - *.xlsx` in `CIM/{YYYYMM}/MX/`. Then:

```bash
python /mnt/skills/custom/jeeves-borrowing-base/merge_template.py "$OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx" "<TEMPLATE_DRIVE_ID>"
```

### 3. Upload to `Debt/CIM/{YYYYMM}/MX/`

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx" --folder "<CIM_MX_FOLDER_ID>" --new   # --new is REQUIRED: without it the uploader strips date stamps, matches the prior dated master, and overwrites/renames it in place (destroys prior snapshot)
```

---

## Key Drive Locations

| Location | Folder ID |
|----------|-----------|
| `Debt/` (CIM parent) | `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU` |
| `Portfolio Reporting/` | `1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC` |

## Rules

- Always state the date range used in your response
- Never modify existing files on Drive — create new dated copies
- Remind user to open in Excel for formula recalculation
- Follow naming: `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx` (US), `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx` (MX), `Portfolio Report - {YYYYMM}01.xlsx` (monthly)
- **When Brian says "use the most recent tape available" (a tape runs daily), do NOT reuse a cached/earlier-session tape file.** Check the actual latest tape by file timestamp in the relevant Drive/output folder (or the cron's last-run date in its state JSON) before running any calc against it, and state that date explicitly. Corrected 2026-07-11 (NB BB thread) after a stale tape was used despite a same-day tape existing.



<!-- Evolution: 2026-07-11 | source: self-improving-agent -->
## MX Servicer Report (Monthly)


### Run recipe (verified 2026-08-10, July 2026 period)

`build_mx_servicer_report.py` in this skill dir is STALE: dates hardcoded to April 2026 AND it still
writes only the BOP and EOP snapshots to the tape tab. Delivered workbooks require a FULL-MONTH DAILY
tape (June file = 30 daily slices, 22,929 rows) because Exhibit B G10 average daily balance is
`SUM(tape!AC:AC)` divided by `(D6-D5)`. Loop `sql\data_tape_sofom.sql` once per calendar day, apply
`calculate_eligibility_fields_sofom`, and assert the resulting column list equals the template tape
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
## HARD RULE — "use X as a starting point" means CLONE X, not redesign

When Brian names an existing artifact as the starting point (e.g. "use the CIM bridge
version"), **open that actual file and edit a copy of it**. Keep tab names, row order,
the label column, column positions and the visual skeleton. "Trimmed down" means
*delete rows/criteria that do not apply*, never re-architect the layout. Cost 87 turns
and three corrections on the NB borrowing base certificate (2026-07-28) — the first
draft was structurally original and Brian rejected it with "this doesn't look anything
like the CIM version. I want a familiar format." Before delivering, diff your output
against the source form tab-by-tab and be able to say what you deleted and nothing else.

## NB borrowing base certificate — facility-specific rules (2026-07-28, Brian)

1. **Collateral is NOT netted out of eligible receivables.** Credit support (AIG
   insurance, cash collateral, pledged receivables) reduces the **excess concentration
   amount only**. Total Eligible Receivables stays gross of collateral; the deduction
   line sits inside the concentration block.
2. **Do NOT run the CIM eligibility calculator for NB.** `eligibility.py` encodes CIM
   Bridge eligibility criteria. The NB tape must be driven **solely by NB credit
   agreement definitions**, so the CIM-derived eligibility columns do not belong in the
   NB tape — and their absence also changes how each exclusion is determined.
3. Concentration grid of record (NB proposal, Jay Berger 2026-07-27), months 0-6 then
   7-24: Top 1 five and five; Top 5 twenty-five then twenty; Top 10 thirty-five then
   thirty (all percent of the eligible pool). Tests aggregate **non-duplicatively** —
   see the concentration brief rules in cfo-org-kb `diligence/ARTIFACTS.md`.
4. Mexico is excluded from the NB pool.
5. Deliverable of record: `Form - NB Borrowing Base Certificate - {YYYYMMDD}.xlsx`.
5b. **Account balances on the NB borrowing base come from the DAILY CICO BALANCES
   EMAIL -- not Redshift, not the prior day's workbook, not a screenshot** (Brian,
   2026-08-14: "You should be populating from the daily CICO email"). Workflow: find
   the most recent CICO daily balances email (sender Mariana, subject carries the
   `YYMMDD` stamp; confirm it is the LATEST one out before using it), download its
   attachment, and match each row to the **non-excluded** accounts on Schedule 5.22
   (Excluded = N) **by trailing account digits** -- the schedule and CICO write account
   numbers differently, so a full-string match silently drops rows. Report the
   as-of date and the source email timestamp alongside the balances.
   Gemini action item from the 2026-07-28 "Jeeves 3 Financings Check Point" assigns
   Brian to propose this form to NB **after** the excess-concentration detail is final.
6. **No frozen refs, ever (Brian, 2026-08-04).** Every figure on the form must be a live
   formula / dynamic range against the tape tabs, not a pasted value or a hard-coded row
   window. When Brian says "these are frozen refs, they should not be", he means sweep the
   WHOLE workbook, not just the cells he pointed at. File of record lives in a
   CIM-mirroring tree: `Capital Markets/Debt/Neuberger Berman/Reporting/{YYYYMM}/` -- NOT under Legal/
  (verified 2026-08-05). Reporting folder id `17Eji0ZgVcaAZcdoPDKNNJaClN8PKuTLW`; month folder 202608 id
  `1AG9A6UDndJ2ymFjAtsI61Yk5wi6pwThp`. **Upload into the MONTH subfolder, never the Reporting parent.**
  Uploading to the parent on 2026-08-05 drew "why didnt you upload it?" from Brian -- the upload had
  succeeded and returned a link, but the file was invisible in the folder he actually opens. After ANY
  upload of a dated deliverable, verify the file's full parent chain (files().get fields="parents" walked
  up), not just that the uploader printed a link.

  **The re-upload is the trap, not the first upload (2026-08-06).** Calling upload_to_drive.py with
  --file-id and no --folder to update a deliverable in place SILENTLY RE-PARENTS it into the default
  "DeerFlow Output" folder. The first upload landed correctly in 202608 and the parent chain verified
  clean; a second upload minutes later (adding the bank_accts rows) moved it out, and Brian again asked
  "why did you not upload it?" while the link still worked. Two rules: (1) ALWAYS pass --folder alongside
  --file-id on every re-upload, and (2) verify by LISTING the destination folder after the LAST upload
  and confirming the filename appears next to its siblings - a chain walk done before a later upload
  proves nothing. Repair without re-uploading: files().update(fileId, addParents=DEST,
  removeParents=current).
  **Root defect fixed in the tool 2026-08-07:** upload_to_drive.py now only re-parents when --folder is
  passed explicitly; --file-id alone leaves the file in its existing parents and prints a NOTE. Rules (1)
  and (2) still stand as hygiene, but a missing --folder can no longer silently move a filed deliverable.

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

## NB borrowing base — the Monday pipeline (`build_nb.py`, 2026-08-17)

Cadence: **weekly, MONDAYS, as of the PREVIOUS DAY (Sunday)** — Brian 2026-08-11, and again
2026-08-17 as a correction ("I told you it needs to run every monday as of the previous day")
when the 8/17 run did not happen. Root cause: NB had no key in `_report_scheduler_state.json`
and no builder; MX SOFOM/CICO/SOFOM-distribution were wired but NB never was. Now:
`build_nb.py` + the `last_nb_bb` scheduler job (Mondays 08:30 PT).

`build_nb.py --as-of <Sunday> --template <prior form> [--out-date YYYYMMDD] [--tape-csv f.csv]
[--cico-msg <gmail id>]`:
1. Tape = `cfo-org-kb/sql/data_tape.sql` @ `dt = as-of`; the 20 data columns of the `tape` tab
   map 1:1 from it, and columns U-AE are re-written as **live formulas** (openpyxl `Translator`
   off row 2) — never pasted values.
2. `obligors` = eligible rows aggregated by EIN group, key `(no EIN) {company_id}` when EIN is
   null, groups with total <= 0 dropped, **sorted by NET exposure (balance - collateral) desc**.
   Verified by reproducing the prior workbook off its own tape — balances tie to the cent.
   Do this replication check first whenever the form's derived tabs are rebuilt.
3. Bank balances from the latest CICO email (vision), matched to col P labels by trailing digits.
   Accounts whose label says "not in use" (Coopcentral 0850) keep their prior value instead of
   failing the run; anything else unmatched raises rather than zero-filling.
4. FX (col R) refreshed from the tape's `spot_rate` (USD per 1 local unit, median per currency).

**A Monday-morning run always carries FRIDAY's balances** — verified 2026-08-18: Mariana does send a
Monday file (`260817 CICO- Daily balances`, Mon 2026-08-17 21:30 CT) but it lands ~13 hours AFTER the
08:30 PT job, and there is no Saturday/Sunday file at all. So the newest email at run time is Friday's
(`260814` for the 8/17 run). Say that in the delivery and do not wait for a Monday email — but never
state "there is no Monday CICO email" as a fact, because there is one; it is just late.

**Controlled accounts drive ALL the cash (Brian, 2026-08-17).** `controlled_acct` = the accounts
**we will perfect**, not only executed DACAs: Citibank 31393601 and JPMorgan 933056167 (Goodwin
Section 6.18 ACA scope, Jeeves Intermediate Holdco). With every row set to `No` — how the 8/14
form shipped — the clause (i) cash pool AND Section 7.01(a) Liquidity both compute to $0, which
reads as a covenant failure that isn't real. Never deliver an NB form with zero cash without
first checking the flags against the perfection scope in
`.deer-flow/wiki/deals/nb_facility_closing.md`. Colombian/Mexican **trust** liquidity lines
(liquidity!E8/E9) stay $0 — "there would be no trust in colombia or mex yet."

**No LibreOffice on this box**, so the saved workbook carries no cached values. Compute the
headline figures (eligible, excess concentration, cash pool, liquidity, BB, step-up prongs) in
Python by replicating the tab formulas, report them, and tell Brian to open in Excel to recalc.

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

## `max_dpd` is NOT current delinquency (diagnosed 2026-08-07)

**And it is not a column of `loc_tape` at all.** 2026-08-27 a portfolio-balance/DPD query
failed with `column "max_dpd" does not exist in loc_tape`. `max_dpd` is a DERIVED column that
`sql/data_tape.sql` adds via its `max_dq` CTE — it exists on the *generated tape*, never on the
raw table. Querying the warehouse directly, the only delinquency column is `days_past_due`.
Standing rule (SOUL, re-stated here because it keeps firing): on any `column X does not exist`,
do NOT guess a second name — list the real ones:
`select column_name, data_type from information_schema.columns where table_name = 'loc_tape' order by ordinal_position;`

The tape carries `days_past_due` (row-level, current as of `dt`) AND `max_dpd` right next to it.
`max_dpd` comes from the `max_dq` CTE in `sql/data_tape.sql`, which is `max(days_past_due)`
grouped by company_id with **no date filter** (`where 1=1`) - the worst DPD that obligor has EVER
hit since the table starts in 2020, stamped on every row of that company. Any DQ rate computed off
`max_dpd` is ~10x too high: on the 2026-08-05 NB tape, max_dpd>=1 = 60.68% of balance vs
days_past_due>=1 = 6.25%.

Rules: current DQ always uses `days_past_due`. NB Section 7.01(b) **DQ30 Rate** = unencumbered
balance >30 DPD / total unencumbered, at month ends on a 3-month rolling average (cap 2.5%,
step-up prong <1.50%) - not a spot mid-month figure. Sanity check on any NB tape: eligible-pool
DPD>30 must be exactly 0 because `elig_iv_dpd30` already excludes it.

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
## NB DQ30 backup - `dq30_detail` tab (2026-08-19)


Brian, 2026-08-19: "we need to add backup to the dq30 calc on the nb borrowing base." The three
month-end figures on the `dq30` tab were pasted values with only a prose source note. Now:

- **`dq30_detail` tab** = loan-level backup, one row per non-charged-off, non-repayment-plan
  `capital_markets_dm.loc_tape` record at each of the three month ends (20,311 rows for
  May/Jun/Jul 2026). Raw fields are values; **encumbrance (col L), the >30 DPD numerator (col M)
  and the DPD bucket (col N) are in-sheet formulas**, so the rule is auditable, not asserted.
- **`dq30` rows 7-9** C/D are live `SUMIFS` over that tab keyed off new date cells `G7:G9`;
  rows 30-41 add a DPD-bucket recon (Current / 1-30 / 31-60 / 61-90 / >90) whose totals must
  reproduce rows 7-9 exactly (row 40 check = 0.00%).
- **Spot memo C25/C26** were pasted python values that did NOT tie to the tape tab despite B28
  claiming they did; they are now `SUMIFS(tape!$R:$R,tape!$W:$W,...)`. The old values used
  `row_flags`' **OR** rule (`transfer_flag='on'` OR `sofom_balance_usd>0`) while the tape tab's
  col W is effectively **`sofom_balance_usd>0` only** (its `N($P2)=1` test can never fire on text).
  The gap is 148 rows, all negative/overpaid balances, -$64,650.45 -- it moves the spot denominator
  but NOT the eligible pool ($118,731,470.95 under either rule at 8/16), so leave `row_flags` alone.
- Query of record: `cfo-org-kb/sql/nb_dq30_monthend_detail.sql`. **Never `coalesce(jvs_remaining,0)`**
  -- NULL means no financed tranche, i.e. UNENCUMBERED; coalescing flips such rows (1 row, $518 at
  5/31/2026) and breaks the tie-out.
- `build_nb.refresh_dq30_backup(wb, as_of, force=False)` runs on every build and re-pulls **only
  when the 3-month window rolls** (`month_end_window` = the 3 most recently ENDED months), so the
  weekly Monday run is cheap and the window updates itself on the first run after month end.
- Verified 2026-08-19: 5/31 1.7317% | 6/30 2.0116% | 7/31 2.2203% -> **3-mo average 1.9879%**
  (cap 2.50%; step-up prong needs <1.50% for three months). Every figure ties to the cent to what
  was delivered on the 8/07 and 8/17 forms.




<!-- Evolution: 2026-08-19 | source: self-improving-agent -->
## NB DQ30 daily reading cron (2026-08-19)


Brian, 2026-08-19: "add a daily measurement of this to the morning daily crons? I want a
simple message that I can forward internally."

- `backend/scripts/nb_dq30_daily.py` — one aggregate Redshift query (~2-6s) for the spot
  reading at the as-of date PLUS the three most recent month ends, so the message carries
  both the daily number and the live covenant test. Writes `.deer-flow/_nb_dq30_state.json`
  and shows the day-over-day bps move off the prior reading. `--print [YYYY-MM-DD]` for a
  dry look; `NB_DQ30_DRY_RUN=1` to exercise the send path without posting.
- **Delivery: `cm_morning_brief.py` spawns it DETACHED (`Popen`) after the brief sends.**
  Deliberately not a `cron_supervisor` entry: (a) `run_morning_brief` gives the brief script
  a 60s subprocess budget and a slow Redshift morning must not cost the brief, and (b) a new
  supervisor registration needs a gateway restart, while a subprocess picks up edits on the
  next run. Weekday cadence, as-of the prior day, so Monday reads Sunday.
- Methodology is intentionally the SAME as the form's spot memo (total balance incl. interest
  and fees, all countries). Brian reviewed the principal-only reading available under the
  "Outstanding Receivables Balance" definition (-16 bps, but ~-$1.08M of BB availability at
  the 90% advance rate) and said "let's leave it as is" — do not silently switch bases.
- The message states that the daily figure is NOT a covenant test point, because the spot
  reading sits ABOVE 2.50% (2.70% at 8/18) while the covenant 3-month average is 1.99%.
  It is written to be forwarded internally: no emoji, no file paths, no bot voice.

