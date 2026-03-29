---
name: jeeves-capital-markets
description: Use this skill when the user asks about capital markets files, lender documents, portfolio reports, Google Drive folder navigation, or asks "where is [X]?" in the Capital Markets workspace. Also use when the user references a lender name (Castlelake, Atalaya, Victory Park, Fasanara, etc.) or asks for a monthly report.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Capital Markets Drive Workspace

The team's capital markets work lives in a shared Google Drive folder.

**Workspace root folder ID:** `1Kb1M_mzLNtzS7Ml_Af37lZ2ISgmMMCHN`

## Folder Map

| Folder | ID | What's inside | When to look here |
|--------|----|---------------|-------------------|
| `.archive/` | — | Old/deprecated files, prior deal versions | User asks for historical versions or "old" docs |
| `.github/` | `1Vs2emp9jbSF3AbTqn6jkdqIHNIOy50yK` | Analytics repo: ~56 SQL files in `sql/`, credit models, Python scripts | User needs a SQL query template or analytics code. Load `jeeves-sql-library` skill for the full catalog. |
| `Debt/` | `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU` | One subfolder per lending facility. Key subfolder: `CIM/` contains monthly period folders (`{YYYYMM}/`) each with `US/` (Bridge BBs) and `MX/` (SOFOM Master BBs) subdirectories. Also: credit agreements, amendments, data tapes, compliance certs, term sheets, legal docs | User references a lender name, asks about a credit facility, needs a contract, or asks to build a borrowing base |
| `Insurance/` | — | Insurance policies, certificates, renewal docs | User asks about insurance coverage |
| `Portfolio Reporting/` | `1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC` | Monthly subfolders (`YYYYMM`). Inside each: portfolio reports, data tape snapshots, charts, board materials | User asks for a monthly report, portfolio snapshot, or board deck |
| `Strategy/` | — | Strategic planning docs, market analysis, new product proposals | User asks about strategy or planning materials |
| `Treasury/` | — | Cash management, bank account docs, FX hedging, liquidity reports | User asks about treasury operations |
| `Vendors/` | — | Vendor contracts, SOWs, pricing proposals | User asks about a vendor or service provider |

## Lenders in Debt/

Each lender has its own subfolder under `Debt/`. The typical structure inside a lender folder:

```
Debt/{Lender}/
├── Credit Agreement/     — Executed agreements, amendments
├── Data Tapes/           — Periodic data tape deliveries
├── Compliance/           — Compliance certificates, covenants
├── Term Sheets/          — Proposed and executed term sheets
├── Correspondence/       — Key emails, memos
└── (other docs)
```

Known lenders with active folders:

| Lender | Notes |
|--------|-------|
| Castlelake | Senior lender |
| Atalaya | Credit facility |
| Victory Park Capital (VPC) | Credit facility |
| Fasanara | Credit facility |
| *(others)* | Browse `Debt/` folder to discover additional lenders |

To find a specific lender's folder, list the Debt directory:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU"
```

Then drill into the lender's subfolder to find specific documents.

## Portfolio Reporting

Monthly report folders follow the `YYYYMM` naming convention (e.g., `202603` for March 2026).

To find the latest report folder:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

The last entry (sorted alphabetically) will be the most recent month.

## Where to save files

When the agent generates output files, upload them to the **correct location** in the workspace — not just the generic DeerFlow Output folder.

| Output type | Save to | How |
|-------------|---------|-----|
| US Borrowing Base (Bridge) | `Debt/CIM/{YYYYMM}/US/` | Browse CIM to find the month's US folder ID |
| MX Borrowing Base (SOFOM) | `Debt/CIM/{YYYYMM}/MX/` | Browse CIM to find the month's MX folder ID |
| Lender data tape | `Debt/{Lender}/Data Tapes/` | List the lender folder to find the Data Tapes subfolder ID, then `upload_to_drive.py <file> --folder <ID>` |
| Portfolio report / board deck | `Portfolio Reporting/{YYYYMM}/` | List Portfolio Reporting to find the month's folder ID, create the month folder first if it doesn't exist |
| Redline / contract markup | `Debt/{Lender}/` (same subfolder as the source doc) | Upload next to the original document |
| SQL query / analytics code | `DeerFlow Output/` (default) | General outputs that don't belong to a specific folder |
| Ad-hoc analysis or one-off export | `DeerFlow Output/` (default) | Use default when there's no natural home |

**Upload to a specific folder:**
```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "/mnt/user-data/outputs/<filename>" --folder "<TARGET_FOLDER_ID>"
```

**Workflow for saving to the right place:**
1. Determine what type of output you're producing
2. Browse the Drive to find the correct target folder ID (use `list_drive_folder.py`)
3. Upload with `--folder <ID>`
4. If the correct subfolder doesn't exist yet, upload to the nearest parent and note the location

## Navigation Commands

**Browse a folder:**
```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "<FOLDER_ID>"
```

**Browse recursively (up to 2 levels deep):**
```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "<FOLDER_ID>" --recursive
```

**Read a specific file:**
```bash
python /mnt/skills/custom/google-drive/fetch_doc.py "<FILE_ID_OR_URL>"
```

**Upload to a specific folder:**
```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "<LOCAL_FILE>" --folder "<FOLDER_ID>"
```

## Rules

- **Never modify lender documents** — these are shared with external counterparties
- **Save files to the right place** — match the output type to the correct Drive folder
- Summarize rather than echo large files — the user wants insights, not raw text
- Always provide Google Drive links when referencing files: `https://drive.google.com/file/d/{ID}/view`
- When a folder ID is unknown, start at the workspace root and navigate down
- For SQL queries, load the `jeeves-sql-library` skill instead
