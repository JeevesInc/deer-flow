---
name: jeeves-borrowing-base
description: Use this skill when the user asks to build, run, or update the borrowing base, data tape, or portfolio report. Covers US (Bridge), MX (SOFOM), and end-of-month portfolio reports. Also triggers for "run tape", "build the BB", "update the borrowing base", or "generate the data tape."
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Borrowing Base & Portfolio Report Pipeline

## Request Router

Match the user's keywords to the correct pipeline. These are three DIFFERENT products — never substitute one for another.

| Keywords (any match) | Pipeline | Script | Date param | Upload folder |
|---|---|---|---|---|
| portfolio report, portfolio reporting, monthly report, run reporting | **Portfolio Report** | `build_portfolio_report.py` | `--date YYYY-MM-01` (1st of month) | `Portfolio Reporting/{YYYYMM}/` |
| borrowing base, BB, run the tape, US BB, bridge BB | **US Borrowing Base** | `build_us.py` → `merge_template.py` | `--date YYYY-MM-DD` (snapshot date, default yesterday) | `Debt/CIM/{YYYYMM}/US/` |
| SOFOM, MX BB, MX borrowing base, mexico BB | **MX Borrowing Base** | `build_mx.py` → `merge_template.py` | `--end-date YYYY-MM-DD` (default yesterday) | `Debt/CIM/{YYYYMM}/MX/` |

**Date rules:**
- Portfolio report: "4/1 portfolio reporting" → `--date 2026-04-01`. Script derives EOP (3/31) and BOP (2/28) automatically.
- Borrowing base: "run the BB" with no date → use yesterday. "BB for 3/27" → `--date 2026-03-27`.
- **Never use today's date.** Data only available through yesterday. Scripts hard-reject today/future.

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

---

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
python /mnt/skills/custom/google-drive/upload_to_drive.py "$OUTPUTS_PATH/Jeeves SOFOM Borrowing Base - Master - {YYYYMMDD}.xlsx" --folder "<CIM_MX_FOLDER_ID>"
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
