---
name: jeeves-borrowing-base
description: Use this skill when the user asks to build, run, or update the borrowing base, data tape, or portfolio report. Covers US (Bridge), MX (SOFOM), and end-of-month portfolio reports. Also triggers for "run tape", "build the BB", "update the borrowing base", or "generate the data tape."
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Borrowing Base Pipeline

Automated pipeline for building the US (Bridge) and MX (SOFOM) borrowing bases, and the monthly portfolio report.

## Overview

There are three deliverables:

| Deliverable | Script | Template location (Drive) | Upload to |
|-------------|--------|---------------------------|-----------|
| **US Borrowing Base** | `build_us.py` | Latest `Jeeves Bridge Borrowing Base - *.xlsx` in `Debt/CIM/{YYYYMM}/US/` | `Debt/CIM/{YYYYMM}/US/` |
| **MX Borrowing Base** | `build_mx.py` | Latest `Jeeves SOFOM Borrowing Base - Master - *.xlsx` in `Debt/CIM/{YYYYMM}/MX/` | `Debt/CIM/{YYYYMM}/MX/` |
| **Portfolio Report** | `build_portfolio_report.py --date {EOM} --template-id <ID>` | Previous month's `Portfolio Reporting - {YYYYMM}01.xlsx` (has formula tabs) | `Portfolio Reporting/{YYYYMM}/` |

---

## Step-by-step: US Borrowing Base (Bridge)

### 1. Generate the data workbook

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_us.py --date 2026-03-27
```

- Defaults to yesterday if `--date` is omitted
- Queries Redshift for BOP tape (prior month-end) and EOP tape (target date)
- Applies eligibility calculations
- Queries rollforward between the two dates
- Saves a 4-tab workbook: `tape_start`, `tape_end`, `rollforward`, `eligibility_summary`
- Output: `$OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx`

### 2. Find the latest Bridge template on Drive

Browse the current month's US folder in CIM:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU" --recursive --max-depth 3
```

Look for the latest `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx` file in `CIM/{YYYYMM}/US/`. Note its Drive file ID.

### 3. Merge data into the template

```bash
python /mnt/skills/custom/jeeves-borrowing-base/merge_template.py "$OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx" "<TEMPLATE_DRIVE_ID>"
```

This downloads the template, replaces the 4 data tabs while preserving the formula/summary tabs, and saves the merged result.

### 4. Upload to Drive

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx" --folder "<CIM_US_FOLDER_ID>"
```

Upload to the CIM month folder's US subfolder. Name the file: `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx`

---

## Step-by-step: MX Borrowing Base (SOFOM)

### 1. Generate the data workbook

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_mx.py --start-date 2026-03-25 --end-date 2026-03-27
```

- Defaults to a 3-day range ending yesterday if dates are omitted
- Queries Redshift for SOFOM tape for each date in range (only SOFOM-transferred companies)
- Applies SOFOM eligibility calculations per day
- Saves a single-tab workbook: `tape`
- Output: `$OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx`

### 2. Find the latest SOFOM Master template on Drive

Browse the current month's CIM folder to find the MX subfolder:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU" --recursive --max-depth 3
```

Navigate to `CIM/{YYYYMM}/MX/` and find the latest `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx`. Note its Drive file ID.

### 3. Merge data into the template

```bash
python /mnt/skills/custom/jeeves-borrowing-base/merge_template.py "$OUTPUTS_PATH/Borrowing Base - SOFOM - {YYYYMMDD}.xlsx" "<TEMPLATE_DRIVE_ID>"
```

### 4. Upload to Drive

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx" --folder "<CIM_MX_FOLDER_ID>"
```

Upload to `CIM/{YYYYMM}/MX/`. Name the file: `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx`

---

## Step-by-step: Monthly Portfolio Report

Run at end of month (last day). The portfolio report is NOT just raw data — it is a multi-tab workbook with formula-driven Summary dashboards, a Country breakdown, LOC tape, rollforward, and a mods (repayment plan) tab.

**Template structure (7 tabs):**

| Tab | Type | Description |
|-----|------|-------------|
| **Summary** | Formulas | Dashboard: UPB, DQ buckets, charge-offs, receivable rollforward, MoM comparison |
| **Summary (2)** | Formulas | Second summary view |
| **Country** | Formulas | DQ breakdown by country (Mexico, Colombia, Brazil) |
| **loc** | Data | LOC tape — one row per account at EOM |
| **rollforward** | Data | Balance rollforward BOP→EOP |
| **mods** | Data | GWC repayment plans (`loan_reference_number LIKE 'RPP%'`, ~95 rows) |
| **loans** | Formulas | Loan-level summary |

**Summary MoM comparison:** Column L = current period (formulas). Column N = prior period (hard-coded values). Before inserting new data, you MUST copy the current col L values to col N so the MoM deltas (col P/Q) work correctly.

### 1. Find the previous month's report (template)

Browse `Portfolio Reporting/` to find the latest report:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

Navigate to the most recent month folder and note the file ID of the `Portfolio Reporting - {YYYYMM}01.xlsx` file. This is the template.

### 2. Run the portfolio report builder

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_portfolio_report.py --date 2026-03-31 --template-id <PREVIOUS_REPORT_FILE_ID>
```

This single command:
1. Queries LOC tape for EOM date
2. Queries rollforward (prior month-end → EOM)
3. Queries GWC mods (repayment plans: `loan_reference_number LIKE 'RPP%'`)
4. Downloads the previous month's report as a template
5. Copies current col L → col N in Summary tabs (preserves MoM comparison)
6. Replaces the 3 data tabs (loc, rollforward, mods) with fresh data
7. Saves as `Portfolio Report - {YYYYMM}01.xlsx`

Output: `$OUTPUTS_PATH/Portfolio Report - {YYYYMM}01.xlsx`

### 3. Upload to Portfolio Reporting

Find or create the month folder, then upload:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Portfolio Report - {YYYYMM}01.xlsx" --folder "<MONTH_FOLDER_ID>"
```

### 4. Remind the user

After upload, remind the user to **open in Excel** to let the Summary/Country/loans formulas recalculate (openpyxl preserves formulas but doesn't evaluate them).

---

## Key Drive Locations

| Location | Folder ID | Description |
|----------|-----------|-------------|
| `Debt/` | `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU` | Parent of all lender folders |
| `Debt/CIM/{YYYYMM}/US/` | *(browse to find)* | US Bridge borrowing base files |
| `Debt/CIM/{YYYYMM}/MX/` | *(browse to find)* | MX SOFOM Master borrowing base files |
| `Portfolio Reporting/` | `1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC` | Monthly report subfolders |

## Data Sources

- **US tape:** `capital_markets_dm.loc_tape` — all active, non-repayment, non-charged-off LOC accounts
- **SOFOM tape:** Same table, filtered to `transfer_flag = 'on'` (SOFOM-transferred companies only)
- **Rollforward:** Account-level BOP→EOP balance reconciliation
- **Eligibility:** ~40 criteria (jurisdiction, currency, DPD, charge-off, credit limit, UW score, etc.)

## Rules

- **CRITICAL — Never use today's date.** Redshift data is only available through yesterday. Always use yesterday or earlier. If the user says "run the borrowing base" or "run today's BB", use **yesterday's date** as the end date. The scripts will reject today's date with an error. "Today's borrowing base" means "through yesterday's data."
- **Always state the date range** in your response so the user knows what data is in the output
- **Never modify existing Bridge/SOFOM Master files on Drive** — always create a new dated copy
- After merging, remind the user to **open in Excel** to let formulas recalculate (openpyxl preserves formulas but doesn't evaluate them)
- If Redshift returns 0 rows for a date, warn the user — the data may not be loaded yet
- Follow the file naming convention: `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx` for US, `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx` for MX
