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
| **Portfolio Report** | `build_us.py --date {EOM}` | *(no template — raw output is the report)* | `Portfolio Reporting/{YYYYMM}/` |

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
- Saves a single-tab workbook: `tape_combined`
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

Run at end of month (last day).

### 1. Generate the data workbook

```bash
python /mnt/skills/custom/jeeves-borrowing-base/build_us.py --date 2026-03-31
```

The full borrowing base output at EOM **is** the portfolio report.

### 2. Upload to Portfolio Reporting

Browse `Portfolio Reporting/` to find or create the month folder:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Borrowing Base - US - {YYYYMMDD}.xlsx" --folder "<MONTH_FOLDER_ID>"
```

Name the file: `Portfolio Report - {YYYYMM}01.xlsx`

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

- **Always state the date range** in your response so the user knows what data is in the output
- **Never modify existing Bridge/SOFOM Master files on Drive** — always create a new dated copy
- After merging, remind the user to **open in Excel** to let formulas recalculate (openpyxl preserves formulas but doesn't evaluate them)
- If Redshift returns 0 rows for a date, warn the user — the data may not be loaded yet
- Follow the file naming convention: `Jeeves Bridge Borrowing Base - {YYYYMMDD}.xlsx` for US, `Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx` for MX
