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

- **HARD RULE — facility terms come from the EXECUTED document, resolved through the closing-binder index, never from a draft, a term sheet, or your own prior summary.** Before quoting any covenant, threshold, basket, reporting date or pricing figure, locate the executed copy first: the counterparty closing-binder folder (Debt > {Counterparty} > Legal > "Closing Binder - {Facility} - {YYYYMMDD}", tab "01 - Credit Agreement") and drive_registry.json. On 2026-08-11 an email to CFO Alex was drafted off a non-executed version while the executed set sat in a closing binder this agent had itself built and indexed the day before; Brian had to reply "you have the final copies, use that". Building an artifact is not the same as remembering it — re-read your own index before answering.
- **Never claim a final/executed copy does not exist** without first listing the closing-binder folder AND searching Drive (owner:brian.mauck) and Gmail in:sent with context words — then name the searches you ran.
- **HARD RULE — a scope list pulled off a credit-agreement schedule must show the FULL schedule population with the in/out flag, not just the in-scope rows.** Asked for "all the US banks that require DACAs under the NB agreement" (2026-08-13), the answer listed only the two non-excluded accounts (Citibank 31393601, JPMorgan 933056167 at Jeeves Intermediate Holdco) and Brian had to come back with "so the bank of america account is excluded?". Deliver every row of the schedule (Schedule 5.22 / Perfection Certificate Schedule 10 for accounts) with bank, account, Loan Party, purpose and Excluded Y/N, then state the in-scope subset - the excluded rows are the decision surface, because an exclusion can be reversed (Alexander, same day: "we will most likely be splitting between JPM and BofA"). Also say whose determination the flag is (Jeeves' vs. counsel's) when it is not on the face of the executed document.
- **HARD RULE — a period-close figure is looked up in the period's Drive folder FIRST, not in email.** Email is what a counterparty happened to send you; Drive is the record. For anything feeding a monthly close (hedge premiums and trade dates, accrued interest, balances, valuations, the tape) open `Debt/{Counterparty}/Reporting/{YYYYMM}/` before running a single Gmail search, and say which file you took each figure from. On 2026-09-14 the August Anexo J stalled twice on "two numbers I will not guess" — the hedge cost and the accrued interest — after six Gmail searches; Brian answered *"You have the fucking valuation report with the premiums and the trade dates"* and *"trade date is in the file that's in drive"*, and `VALUATIONS_20260911225702.xlsx` in `Reporting/202609/` produced both in under two minutes. Asking Brian for a number that is sitting in the period folder is the same defect as guessing it.
- **Never modify lender documents** — these are shared with external counterparties
- **Save files to the right place** — match the output type to the correct Drive folder
- Summarize rather than echo large files — the user wants insights, not raw text
- Always provide Google Drive links when referencing files: `https://drive.google.com/file/d/{ID}/view`
- When a folder ID is unknown, start at the workspace root and navigate down
- For SQL queries, load the `cfo-org-kb` skill instead — all SQL templates are local now
- **HARD RULE — "what is the most recent {X} file we have?" is a request for the FILE, not for its location.** Answer by attaching the actual file in the same Slack reply (download the Gmail attachment or export the Drive file, then post it), and put the provenance line — filename, date, who it went to — underneath. Describing where the file lives and stopping there forces a second round trip: on 2026-08-07 the AIG invoice ask was answered with a description and Brian had to reply "No just cant find the file, can you share it here?". Same for "send me", "pull up", "share", "do we have" — deliver bytes first, narrate second.
