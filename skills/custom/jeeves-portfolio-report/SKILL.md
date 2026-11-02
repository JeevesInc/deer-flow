---
name: jeeves-portfolio-report
description: Use this skill for the INTERNAL monthly Portfolio Report (dated the 1st of the month, covering the previous month) built by build_portfolio_report.py and filed in Drive under Portfolio Reporting/{YYYYMM}/. Triggers on "portfolio report", "portfolio reporting", "monthly report", "run reporting". NOT the borrowing bases (jeeves-borrowing-base) and NOT the monthly servicer reports (jeeves-servicer-report).
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Internal Portfolio Report (monthly)

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

**Date convention (standing):** the report date is the **1st of the month**, the Drive folder is the
**current** month, and the data covers the **previous** month. `build_portfolio_report.py --date
2026-04-01` derives EOP (3/31) and BOP (2/28) automatically.

**There is no longer a second, external portfolio report.** The CIM Monthly Tested Portfolio Report
retired with the CIM US bridge facility on 2026-08-10. The 2026-08-04 router confusion between
"portfolio report" and "monthly tested portfolio report" that cost ~70 turns cannot recur, because
only one of them still exists. If someone asks for a "tested" portfolio report, say it was retired
rather than building the internal one as a substitute.

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
python /mnt/skills/custom/jeeves-portfolio-report/build_portfolio_report.py --date 2026-04-01 --template-id <PREVIOUS_REPORT_FILE_ID>
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
4. **Third tie-out: BOP continuity — this period's BOP must equal the prior report's EOP (L52 of the template), and any gap is TRACED BEFORE delivery, never just reported.** 2026-10-02 the Sept report shipped flagging a $93,805.28 BOP-vs-Aug-EOP gap as 'worth knowing'; Brian: *"So trace the break"*. Trace = company-level diff of template `loc` (prior EOP) vs this run's BOP rows; for each mover check `is_in_repayment` / RPP start date in GWC (`loan_reference_number LIKE 'RPP%'`), charge-off flag and restated balances. Known cause: accounts flagged into an RPP retroactively AFTER the prior report ran (Sept: companies 30957 + 28826, RPPJVLCOP3251/3250, flagged from 8/26-27) drop out of BOP and are not caught by the in-period Modification line. Deliver the explanation table with the report.

Known, pre-existing and NOT introduced by the build: 6 error literals on `Summary (2)`
(`L5 =loc!#REF!` plus five `ARRAY_CONSTRAIN`/`ARRAYFORMULA` Google-Sheets leftovers). `Summary`
itself is clean. Also: **the builder does NOT refresh the `loans` tab** (nor `Sheet1`) — it
carries the prior month's rows, which is why Summary rows 21-25 read 0.00 every month.

`Portfolio Reporting/202609/` folder id `1x4_CJ566Lx8ntLIPZTp9ymJ8ZmThkLZb` (created 2026-09-01).
Template of record for the November build (October period) is now
`Portfolio Report - 20261001.xlsx`, id `1ocnfvrXah-naXrJzVmiI6hpt1ygP_8JB`, in `202610/` (folder id `1mof8L-Cv3XQ9O1Rrxr9bgUiQcXrADZLu`). 2026-10-02: builder crashed on missing `sql/` dir (left in jeeves-borrowing-base at the 9/14 move) - patched to fall back to that dir. The 20260901 template HAD cached col-L values (opened in Excel 9/23), so the MoM shift worked and N4/N5 were correct - still verify every month.

---

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

