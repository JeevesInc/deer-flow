---
name: jeeves-capital-markets
description: Use this skill when the user asks about capital markets files, lender documents, portfolio reports, Google Drive folder navigation, or asks "where is [X]?" in the Capital Markets workspace. Also use when the user references a lender or counterparty name, asks for a monthly report, or needs to find a credit agreement or term sheet.
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Capital Markets Drive Workspace

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


The team's capital markets work lives in a shared Google Drive folder.

**Workspace root folder ID:** `1Kb1M_mzLNtzS7Ml_Af37lZ2ISgmMMCHN`

## Folder Map

| Folder | ID | What's inside | When to look here |
|--------|----|---------------|-------------------|
| `.archive/` | — | Old/deprecated files, prior deal versions | User asks for historical versions or "old" docs |
| `Daily Digest/` | `1q4pE0obMcrHOE0eQCh1M1ENhqaLW-wNg` | Revenue digest CSVs (MTD vs prior month) | User asks about daily revenue tracking |
| `Debt/` | `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU` | One subfolder per lending counterparty. Key subfolder: `CIM/Reporting/` contains monthly period folders (`{YYYYMM}/`) with US (Bridge BBs) and MX (SOFOM) data tapes. Also: credit agreements, amendments, term sheets, legal docs, diligence materials | User references a lender name, asks about a credit facility, needs a contract, or asks to build a borrowing base |
| `Insurance/` | — | Insurance policies, certificates, renewal docs | User asks about insurance coverage |
| `Portfolio Reporting/` | `1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC` | Monthly subfolders (`YYYYMM`). Inside each: portfolio reports, data tape snapshots, charts, board materials | User asks for a monthly report, portfolio snapshot, or board deck |
| `Strategy/` | — | Strategic planning docs, market analysis | User asks about strategy or planning materials |
| `Treasury/` | — | Cash management, bank account docs, FX hedging, liquidity reports | User asks about treasury operations |
| `Vendors/` | — | Vendor contracts, SOWs, pricing proposals | User asks about a vendor or service provider |

## Counterparties in Debt/

Each counterparty has its own subfolder under `Debt/`. The typical structure:

```
Debt/{Counterparty}/
├── Diligence/        — DD materials, trackers, data packages
├── Legal/            — Credit agreements, amendments, term sheets
├── ~BROMIUM/         — Encrypted/sensitive copies
└── (other docs)
```

### Active Facilities

| Counterparty | Status | Key People / Counsel | Notes |
|---|---|---|---|
| **CIM** | Primary facility — active | Alejandra Granados (CIM), Goodwin (legal), CXC/Monex (MX SPV trustee/servicer) | Bridge Loan Agreement, 5th Amendment in progress. Monthly reporting under `CIM/Reporting/{YYYYMM}/`. MX recycling operations with CXC (Gustavo Villarreal, Iván Mendez) and Monex (Rodrigo Cue, Lizbeth Caballero). |
| **Neuberger Berman (NB)** | New facility — diligence | Goodwin (legal), BU Colombia (local counsel) | Colombia SPV credit facility. Active diligence (April-May 2026). Financial forecasts, collateral proposal, background checks in progress. Compare docs: `Analysis - NB vs CIM*.md` in Debt/ root. |

### Active Negotiations

| Counterparty | Status | Notes |
|---|---|---|
| **BBVA** | DD + legal docs | SBLC + overdraft + revolving. Due diligence tracker (April 2026). White & Case (counsel). |
| **Vista Credit** | DDQ in progress | DDQ list, data package, tracker (April 2026). |
| **Covalto** | Term sheet negotiation | Secured facility. Multiple redline rounds (March-April 2026). |
| **Fasanara** | Term sheet negotiation | Credit facility terms. Q&A + redlines (March-April 2026). |
| **Gramercy** | Term sheet negotiation | Corp facility. Redlines (March-April 2026). |

### Data Room / Early Stage

| Counterparty | Status | Notes |
|---|---|---|
| **i80** | Data room shared | Corporate deck, data tape, financials (Feb 2026). |
| **Accial** | Term sheet | March 2026. |
| **Lendable** | NDA only | Feb 2026. |
| **Rivonia Road** | NDA only | March 2026. |
| **PFG** | NDA only | Feb 2026. |
| **UBS** | NDA + discussion | Jan-March 2025. |
| **BTG AM** | Q&A stage | NDA + Q&A (Jan 2025). |

### Legacy / Closed

| Counterparty | Notes |
|---|---|
| **PSC-Atalaya** | Legacy facility (2022-2023). Forbearance, paydowns, compliance certs. Fully wound down. |
| **PSC** | Legacy (2022-2023). Closing docs, invoices, funding requests. |
| **GS (Goldman Sachs)** | Historical warehouse facility (2022). DD materials, legal docs. |
| **Empirica** | NDA only (Oct 2024). |

### Cross-Counterparty Analysis

The Debt/ root folder contains comparison documents:
- `Analysis - Lender Term Sheet Comparison - 20260401.xlsx`
- `Analysis - CIM vs NB Colombia Term Sheet - 20260410.xlsx`
- `Analysis - NB vs CIM (Bridge + CO SPV) - 20260430.md`
- `Summary - Facility Overview VDR - 20260423.docx`

## CIM Reporting Structure

CIM is the only active reporting lender. Monthly data tapes and BBs go here:

```
Debt/CIM/Reporting/{YYYYMM}/
```

Folders exist from 202312 through 202605. To find the latest:

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1oDi6HjzArnX_jPQbI9UfEs4R5DC9TTrA"
```

Other CIM subfolders: `Audit/`, `Diligence/` (CO SPV, Corp Credit, MX SPV), `Legal/` (Bridge Facility, Colombia SPV, Mexico SPV), `Modelling/`, `Vendors/` (CxC, Trustee).

## Portfolio Reporting

Monthly report folders follow the `YYYYMM` naming convention (e.g., `202605` for May 2026).

```bash
python /mnt/skills/custom/google-drive/list_drive_folder.py "1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC"
```

The last entry (sorted alphabetically) will be the most recent month. Also contains a `Vintage/` subfolder.

## Where to save files

| Output type | Save to | How |
|-------------|---------|-----|
| US Borrowing Base (Bridge) | `Debt/CIM/Reporting/{YYYYMM}/` | Browse CIM/Reporting to find the month's folder ID |
| MX Borrowing Base (SOFOM) | `Debt/CIM/Reporting/{YYYYMM}/` | Same folder as US BB |
| Lender data tape | `Debt/{Counterparty}/Diligence/` or `Data Tapes/` | List the counterparty folder to find the right subfolder |
| Portfolio report / board deck | `Portfolio Reporting/{YYYYMM}/` | List Portfolio Reporting to find the month's folder ID, create the month folder first if it doesn't exist |
| Redline / contract markup | `Debt/{Counterparty}/` (same subfolder as the source doc) | Upload next to the original document |
| Term sheet comparison | `Debt/` root | Cross-counterparty analysis goes in the Debt root |
| Ad-hoc analysis or one-off export | `DeerFlow Output/` (default) | General outputs that don't belong to a specific folder |

**Upload to a specific folder:**
```bash
python /mnt/skills/custom/google-drive/upload_to_drive.py "/mnt/user-data/outputs/<filename>" --folder "<TARGET_FOLDER_ID>"
```

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
- For SQL queries, load the `cfo-org-kb` skill instead — all SQL templates are local now
