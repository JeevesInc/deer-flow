---
name: jeeves-diligence
description: >
  Use this skill when preparing due diligence materials, data packages, or responses to investor/lender DDQs. Enforces strict source discipline -- no fabricated narratives, unverified claims, or invented data in any document shared externally.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Jeeves Diligence

<!-- Created: 2026-04-18 | source: self-improving-agent -->
<!-- Updated: 2026-04-17 | source: BBVA + Vista Credit DD processes -->
<!-- Updated: 2026-06-10 | preflight, canonical artifacts (cfo-org-kb), dd_verify gate, update-in-place uploads -->

---

## Start-of-Task Preflight (mandatory, before ANY diligence work)

1. **Pull the kb and read the artifact definitions**:
   `cd /mnt/skills/custom/cfo-org-kb && git pull origin main && cat diligence/ARTIFACTS.md`
   Every DDQ request maps to a generic artifact there (loan tape, DQ/CO monthly,
   rollforward, ...). Counterparty item codes (VCP-1, FDD-10, ...) are ephemeral —
   the artifact definitions are not. Identify what is being asked for in artifact
   terms BEFORE building or requesting anything.
2. **Check the canonical Diligence Registry in Drive** before building or
   requesting any item: latest `Diligence Registry - Capital Markets - YYYYMMDD.xlsx`
   in `Debt/` root (folder `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU`). Most diligence items
   already exist from a prior counterparty's process — reuse/refresh, don't rebuild.
3. **Re-read the Hard Rules below.** They each cost real rework when violated.

## Hard Rules — Tracker + Correspondence (learned Jun 9-10 2026, hours of rework)

1. **Never produce a *content* file without being explicitly asked** (data answering
   a DD item — financials, revenue, DQ tables). Ownership stays with the named person;
   a Redshift reconstruction of an accounting record is a fabrication. (FDD-3 and
   FDD-10 were built unsolicited — both wrong, both reverted.)
   **But DO proactively handle *logistics*** — creating/organizing the DD folder,
   keeping the tracker current, verifying links, chasing owners, AND finding+filing documents we already have rather than emailing for them. Brian was explicit
   that deferring the obvious DD folder was wrong. Content = ask first. Logistics = just do it.
2. **Read the full email thread AND Brians sent mail (in:sent) before drafting any follow-up or setting tracker statuses - statuses must reflect what Brian actually sent/answered, not just agent drafts.** Fetch every reply,
   identify what has been received vs. outstanding, then draft. Never draft from the
   original request alone. (Brandon/Thiago follow-up missed all the delivered items.)
3. **Match the format of the last communication.** Find the most recent email Brian
   sent and mirror its structure exactly. Do not invent a new format. (Alex reply was
   by status; correct was by person — visible in the thread.)
4. **State recipients whenever you create a draft.** `gmail_tool.py draft` prints a
   RECIPIENTS block — repeat To/Cc in your reply to Brian. A threaded reply inherits
   the WHOLE group (including external counterparties) unless you pass `--to`/`--cc`.
   Replies stay on the thread (never switch to `draft-new` for a conversation). BUT a NEW topic going to counsel/counterparty gets its OWN clean standalone email — do NOT append it to an unrelated existing thread (e.g. another item's punch-list thread) just to consolidate. And never reuse a topical thread that is internal-only, or its quoted internal chatter leaks to external counsel. Check both the topic and the participants before reusing any thread.
   (A draft to Alex nearly went to Vista's entire team.)
5. **Run `dd_verify.py` before reporting tracker/VDR work as done**:
   `python dd_verify.py --tracker <xlsx> --folder <VDR_FOLDER_ID>`
   It verifies every link resolves and every uploaded file actually opens, and flags
   duplicates. An API call returning 200 is not verification. (Broken FDD-11 link,
   corrupted VCP-2 upload — both would have been caught.)
6. **Fix the class, not the instance.** When Brian flags one defect (an unlinked item,
   a broken link, a wrong status), sweep the ENTIRE artifact for every other instance
   of that defect class and fix them all before replying. (Half of the Jun 9
   back-and-forth was Brian manually iterating an audit the agent should have run itself.)
7. **Never delete-and-reupload a counterparty-facing Drive file.** `upload_to_drive.py`
   now updates in place by default (same file ID, same link). Deleting changes the ID,
   breaks every link already shared, and risks corruption. External-facing workbooks
   with formulas: upload converted to a native Google Sheet so formulas evaluate.
8. **Deliver complete work once.** Think from "what does correct finished work look
   like?" before starting. Back-and-forth is unacceptable.
9. **Never mark a diligence item pending/incomplete without checking if it was
   already finished in a prior session.** Before writing "pending" or "still needed"
   in any draft or tracker row, search Slack/Drive/Gmail for confirmation of the
   specific action (e.g. the exact deck slide-trim, the exact file update) -- the
   task may have already been completed off-thread. (NB ODD deck-trim item was
   drafted as PENDING when it had already been done, 2026-07-01/02.)
10. **A tracker/Notes-column description of a document is NOT the document.** If a
    tracker cell says e.g. "BTG account, 45 months", that is someone's summary, not
    a verified fact -- open the actual underlying file before repeating the figure
    in any external-facing draft. Cost a correction on 2026-07-08 (Kroll FinOps ask:
    stated a loan term copied verbatim from the tracker Notes column, hadn't opened
    the source file). If asked how a figure was obtained, answer with the literal
    source (tracker note vs. opened document), not the figure's plausibility.

11. **A screenshot or page-render of a workbook is NOT the workbook. Never build a
    reconciliation, a count, or any external-facing figure off vision extraction when
    a native file exists or could be requested.** Before extracting a table from
    images, SEARCH for the source file yourself. **Check `.deer-flow\slack_downloads\`
    FIRST** — `app\channels\slack.py` auto-saves EVERY file Brian shares in Slack
    there, named `YYYYMMDD_HHMMSS_<original name>`; the 260729 CICO workbook had been
    sitting there since the previous day when the 2026-07-31 recon was built off
    screenshots. Then Gmail attachments, then Drive. Do not ask Brian where the file
    is; find it. Vision extraction fails
    silently in two specific ways: it truncates rows below the visible region, and it
    strips column context so a currency column is read as USD. Cost the whole
    2026-07-31 Schedule 5.22 vs CICO cycle — a recon built from 4 CICO screenshots
    dropped ~55 rows and read original-currency balances as USD; Brian had to ask
    "could that be an artifact of poor vision?" and then supply Axel's actual
    workbook, which showed 212 Jeeves accounts vs 80 disclosed and 151 unmatched.
    If images are genuinely the only source: state the extracted row count, say
    explicitly that it is vision-derived, and label every derived figure
    [Unverified — image extraction]. More than one image needed = stop and get the file.

12. **The requester's attached file is the format of record — enumerate attachments
    BEFORE building anything.** Rule 11 covers finding a source file for *data*; this
    covers *format*. A counterparty data request that arrives with a workbook attached
    is telling you the exact shape it wants back: build inside that workbook's tab and
    row/column labels rather than producing a well-formed file of your own design.
    Run `gmail_tool.py download <message_id>` on the request itself as step one, and
    when Brian asks "is this the same format as the file he attached?" the answer must
    be traceable to the opened attachment, not to a deck slide or a prior report.
    Cost two rebuild cycles on the 2026-08-14 Mastercard Brazil receivables aging file
    (Alex's Sheet1 template: FAIXA ATRASO rows × month columns).

---

## Core Rule: Never Fabricate

**Every claim in a diligence document must have an explicit, traceable source.**

If you do not have a source, you have three options:
1. Pull the data from Redshift (for portfolio/financial metrics)
2. Fetch the actual document from Drive (for policy/legal/operational claims)
3. Flag it as "Needs Confirmation" and leave it blank

**There is no fourth option. Do not invent plausible-sounding content.**

---

## Confirmed Facts — Do Not Deviate

- **Jeeves investor list (confirmed)**: CRV, Tencent, Andreessen Horowitz, GIC, Stanford StartX. Do NOT list Visa or any other investor not on this list. Visa was hallucinated into the NB Diligence deck — it is not a Jeeves investor.
- **Kroll P2 internal team roles (confirmed 2026-07-01, cost a full redo cycle when assigned wrong):** Brandon = lawyer (handles clickwrap/click-through acknowledgments, NOT bank statements). Thiago = lawyer, works for Brandon. Isa (Isabel) = FinOps lead (bank statements, financial ops docs). William and Axel work for Isa (FinOps). Do not reassign document types across these roles without an explicit correction from Brian.
- **Kroll P2 / diligence document requests are NEVER split up by country.** Brian repeated this correction multiple times on 2026-07-01: "we're not breaking this up by country at all." Organize by artifact type or owner, never by country segmentation, unless Brian explicitly asks for a country cut.
- **When rebuilding a rows-based artifact (e.g. the Sample Selection file) via script, never regenerate from scratch** — start from the last-known-good copy, merge/append, then diff row counts (and country coverage, e.g. Mexico rows) before vs. after as a sanity check. This file has had Mexico rows silently dropped on rebuild multiple times (2026-06-25 and again 2026-07-01).
- **For any investor-facing deck or diligence document**: all company facts must be sourced from a document Brian has provided or a verified data pull. If no source exists, flag as "[Needs Confirmation]" — never fill gaps with general knowledge.

---

## What Counts as Fabrication

These are all fabrications, even if they sound reasonable:

- Stating a specific FX rate (e.g., "BRL ~5.3 in mid-2024") without a data source
- Describing a hedging strategy (e.g., "Jeeves hedges BRL via forwards") without reading the hedging policy doc
- Stating a recovery rate without querying recoveries from Redshift
- Describing license arrangements for a jurisdiction without reading the actual license doc
- Explaining the cause of a DQ spike without data to support the explanation
- Echoing a counterparty number (e.g., Vista says "$82K CO") without verifying against our own data
- Describing customer revenue profiles (e.g., "USD-denominated revenues") without a source
- Stating a CO rate in bps when the actual figure is in % (unit confusion = fabrication)
- Reporting a row count, a total, or an unmatched-item count from a table read out of a
  screenshot or image render, when the native workbook was available (vision truncation
  and currency-column loss = fabrication, not an approximation)

---

## Pre-Flight Checklist Before Writing Any Narrative

For each claim you are about to write, ask:

| Question | If NO |
|----------|-------|
| Do I have a Redshift query result supporting this? | Remove the claim or run the query |
| Do I have a Drive document I have actually read? | Remove the claim or fetch and read the doc |
| Is this number from the counterparty own document? | Verify against our data before repeating it |
| Am I describing a policy/process I have not read? | Remove the claim or fetch the policy doc |
| Am I using general knowledge about markets/FX/industry? | Flag as "management to confirm" |
| Am I mixing % and bps? | Verify units explicitly before writing |

---

## Known Traps (Lessons from Past DD Rounds)

### Portfolio Performance Narratives

- **CO rate math**: Always compute from actual data. CO rate = monthly CO $ / EOM balance. Do NOT estimate in bps without computing. Known error: 3.47% is NOT 110 bps -- these are different units. Always state the unit explicitly.
- **Recovery rates**: Do NOT state a recovery rate without a verified Redshift query. Use loc_tape WHERE charge_off_flag=true AND dt > charge_off_dt AND payment_amount_usd > 0. Actual tracked recoveries are very low (~1-2%) but methodology may be incomplete -- flag and ask management (Shalom to provide figure).
- **DQ spike causes**: Do NOT explain a DQ spike without country-level breakdown data. Always break down by country_code before attributing a cause. Known error: Feb 2026 DQ spike was attributed to Colombia billing cycle change -- it was actually Brazil.
- **Counterparty-cited events**: If a lender/investor cites a specific data point (e.g., "$82K CO in May 2025"), verify the figure against our own data before repeating it. Known discrepancy: Vista cited $82K CO -- our data showed $99K. Vista cited 44 Dec 2025 accounts -- our data showed 145. Always flag discrepancies explicitly.

### License Matrix

- **Only include rows with source documents in hand.** If you have not read the actual license/registration doc, mark the row "Needs Legal Confirmation."
- **US**: Confirmed -- sourced from BBVA DD 6.02 and 7.04
- **MX**: Confirmed -- sourced from BBVA DD 6.02 and 7.04
- **Colombia**: Confirmed -- sourced from BBVA DD 6.02 and 7.04
- **Brazil, Canada, UK**: No license docs in current DD package -- always flag as unconfirmed
- **Stablecoin/Bridge**: Bridge Agreement is in BBVA DD 6.02, but custodial/counterparty details require Legal confirmation

### Hedging / FX

- Do NOT describe Jeeves hedging strategy without reading the Hedging Policy doc (BBVA DD 4.02, Drive ID: 1vT6kVBkttUIimHVs2CT-6KjT8vi2t2)
- Do NOT state specific FX rates (BRL/USD, COP/USD, etc.) without a data source
- If asked about FX hedging, fetch and read the policy doc first -- Tab 2 narratives and Tab 4 hedging content in any data package remain dependent on management confirmation until the doc is read

### Financial Metrics (Verified)

- **2023 CO rate**: ~3.5% monthly average (NOT 110 bps -- that was a unit error: 3.47% is not 110 bps)
- **Recovery rate**: Do not state 15-25% or any figure without a verified query. Actual tracked recoveries in loc_tape are very low (~1-2%) but the query methodology may be incomplete

---

## VDR vs. Available Distinction

When writing sitreps or status docs for a specific counterparty, always distinguish:

- **"In the VDR"** = the counterparty already has access to this document in the shared data room
- **"Available"** = we have the document but have not yet shared it with the counterparty
- **"Needs Preparation"** = the document does not yet exist and must be created

Never conflate these three states. Never use BBVA DD folder references in a Vista sitrep (or vice versa) -- each counterparty has their own VDR.

---

## Sitrep Language Rules

When writing a diligence sitrep (status document for internal review):

- Use **GREEN / YELLOW / RED** status per item
- Write in present tense only -- describe what exists NOW, not what is "being prepared"
- No forward-looking language ("will be provided", "is being prepared") unless explicitly flagged as a future action with an owner
- Distinguish between what the counterparty already has access to vs. what is available but not yet shared
- Every item must have: status, what exists, where it lives, what is missing, who owns the gap

---

## DDQ Response Document Rules

When writing a DDQ response document that will be shared externally (e.g., Vista IC):

- Use **verbatim questions** from the counterparty -- do not paraphrase or summarize
- Write responses in **third person** as standalone written answers (no internal prep language)
- Do not include "Alex," or any internal addressee prefix -- responses must read as clean standalone answers
- Do not include forward-looking "being prepared" language -- only state what currently exists
- Every factual claim must have a source; flag unverified items as "[Management to confirm]"

---

## Tracker and Folder Hygiene

Lessons from BBVA DD reconciliation:

- **Trackers are ALWAYS native Google Sheets shared via link, never .xlsx** (Brian, 2026-06-17 explicit correction). upload_to_drive.py uploads xlsx-as-xlsx with NO conversion; instead create the file with mimeType 'application/vnd.google-apps.spreadsheet' (Drive converts the xlsx on import), share the link (domain writer for tryjeeves.com), and trash the prior xlsx so there is one canonical Sheet.
- **Tracker and Folder alignment is mandatory**: Every item in the tracker must map to exactly one folder. Every folder must have a corresponding tracker row. Reconcile before declaring complete.
- **No duplicate files**: When a new version of a file is uploaded, delete the old version. Drive folders must contain exactly one copy of each document.
- **Duplicates are always deconflicted (Brian, 2026-06-10)**: Whenever discovery,
  triage, or any audit surfaces the same document under multiple Drive IDs,
  deconflict immediately — pick one canonical copy, remove/archive the rest, and
  never present duplicate copies to Brian as independent triage rows. The registry
  refresh script now groups duplicates into a "DUPLICATES TO DECONFLICT" section
  and flags each affected item.
- **Same filename != same content -- verify md5Checksum before trashing (learned 2026-07-01)**:
  Filename-only duplicate detection produces false positives. Before deconflicting,
  pull md5Checksum/size/createdTime/lastModifyingUser via the Drive API for
  every copy in a group. Three real patterns seen:
  (1) True duplicates - identical md5, safe to trash one immediately.
  (2) Iterative resaves - same filename, different md5, all created within
  minutes/hours of each other by the same editor (classic "regenerated PDF/deck each
  time I fixed a comment" pattern). Keep the LAST-created/modified copy, trash the rest.
  (3) Genuinely different documents that happen to share a filename (different
  owner/editor, big size gap, days/weeks apart) -- do NOT auto-resolve; flag for Brian.
  ~BROMIUM folders are junk, not documents: HP Sure Click/Bromium endpoint
  security software silently creates a ~BROMIUM subfolder inside legitimate DD
  folders containing tiny (2-4KB) sandboxed stub copies of real files under the
  identical filename. These get picked up by recursive crawls and misreported as
  "new items" and false-positive "duplicates" of the real, full-size document
  elsewhere in the same tree. Always check the parent folder name for ~BROMIUM
  before treating a small same-named file as a duplicate to reconcile -- it's not
  a document, it's a security-software artifact. Trash it outright; do not add it
  to the registry. Exclude ~BROMIUM folder names from future recursive crawls of
  BBVA DD entirely.
- **No cross-contamination**: Never reference BBVA DD folder links in a Vista tracker (or vice versa). Each counterparty tracker must reference only that counterparty VDR/folder structure.
- **When updating a tracker, delete any notes Brian left for the agent (e.g. "Draft an email", "Ask X") once actioned - they are directives to the assistant, not durable tracker content. Empty folders are not done**: A folder with no file is an open item, not a placeholder. Either upload the file or mark the tracker row RED.
- **When a reconciliation reveals a fixable gap** (file ID + folder ID both known): execute the fix immediately -- do not list it as a user action item. Only put items on the user's plate when they require a human decision or a document Brian must provide.

---

## Diligence Tool (`diligence_tool.py`)

CLI tool for automating common DD workflows.

### Commands

```bash
# Pull all key portfolio metrics into a structured JSON data package
python diligence_tool.py gather-portfolio --date 2026-04-30

# Check a claim against Redshift data (outputs raw data for manual comparison)
python diligence_tool.py verify-claim "90+ DPD rate is under 5%"

# Create a DDQ response scaffold from a list of questions
python diligence_tool.py ddq-scaffold --input questions.txt --output ddq_draft.md
```

### gather-portfolio

Runs 6 pre-built queries against Redshift and outputs a JSON data package:
- Portfolio summary (balance, DPD rates, account counts)
- Country breakdown
- Charge-off trend (6 months)
- GWC/Jeeves Pay summary
- DQ bucket distribution
- Top 20 exposures with company names

Output: `dd_portfolio_YYYY-MM-DD.json` in the outputs directory.

**Use this before writing any DD narrative.** Every number in a DD document must trace back to this data package or a specific Redshift query you ran.

### verify-claim

Takes a text claim and runs relevant queries based on keyword detection. It does NOT determine truth — it presents raw data for manual comparison.

If no query covers the claim, the claim cannot be verified from Redshift data and must be flagged as [Needs Confirmation].

### ddq-scaffold

Reads a text file of DDQ questions (one per line) and creates a Markdown response template where every answer slot requires an explicit source citation.

---

## Workflow: Responding to a DDQ

1. **Receive questions** → save to a text file
2. **Run scaffold**: `python diligence_tool.py ddq-scaffold --input questions.txt`
3. **Pull data**: `python diligence_tool.py gather-portfolio --date YYYY-MM-DD`
4. **For each question**:
   - If answerable from Redshift: cite the query and data
   - If answerable from a Drive doc: fetch and read the doc first
   - If neither: mark as **[Management to confirm]**
5. **Before sending**: run `verify-claim` on every factual statement in the document
6. **Never fill gaps** with general knowledge, estimates, or assumptions

---

## Diligence Registry

The central store for all diligence items across counterparties is:

**Drive:** `Diligence Registry - Capital Markets - YYYYMMDD.xlsx` in `Debt/` root  
**Debt/ root folder ID:** `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU`

The registry has three tabs:
- **Master Registry** — every DD item across all counterparties (Drive ID, status, owner, notes)
- **Counterparty Summary** — one-row-per-lender stage/status overview
- **Monthly Update Runbook** — 8-step procedure for the monthly refresh

### Monthly Update Job (AUTOMATED as of 2026-06-10)

`diligence_registry_cron.py` runs inside the gateway (cron_supervisor) — on the
1st of each month (or if >35 days stale) it crawls all counterparty folders,
diffs against the **canonical registry downloaded from Drive Debt/ root**, and
DMs Brian a summary of NEW items for triage. Discovery only: the registry Excel
is hand-curated, so the cron never overwrites it.

Manual runs (when Brian asks):

```bash
# Discovery only — crawl + summary, no rebuild/upload
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/jeeves-diligence/diligence_registry_refresh.py --dry-run

# Full refresh — discover + rebuild Excel + upload to Drive (updates the
# canonical file in place — only run after new items are triaged into the builder)
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/jeeves-diligence/diligence_registry_refresh.py
```

The refresh script:
1. Downloads the latest canonical registry Excel from Drive Debt/ root (falls back to `backend/.deer-flow/diligence/`)
2. Crawls all active counterparty folders (BBVA DD, NB Diligence, FP Diligence, Vista, CIM, Covalto, Gramercy, Fasanara, Debt Root)
3. Identifies NEW files not yet in the registry and RECENT files (last 45 days)
4. Saves a `Diligence Refresh Summary - YYYYMMDD.txt` with actionable item list (in `backend/.deer-flow/diligence/`)
5. (full mode only) Rebuilds the registry Excel with today's date and uploads to Drive Debt/ root
6. Logs a self-improvement episode if new items were found

### Verification Gate (`dd_verify.py`)

```bash
python dd_verify.py --tracker <path/to/tracker.xlsx> --folder <VDR_FOLDER_ID>
```

Verifies every hyperlink in a tracker resolves (Drive API + HTTP), downloads
and opens every file in a VDR folder (catches corrupt uploads), and flags
duplicate documents and stray subfolders. Exit 0 = clean. **Mandatory before
reporting any tracker/VDR work as done.**

### Rebuilding the Registry from Scratch

```bash
python C:/Jeeves/redshift-bot/deer-flow/skills/custom/jeeves-diligence/build_diligence_registry.py
```

### Counterparty Drive Folder IDs (active as of May 2026)

| Counterparty | Folder | ID |
|---|---|---|
| BBVA | Due Diligence/ | 1pA5_GOqtHMTatJE5vIIYCwm-p742d5yT |
| BBVA | Root | 12ns4FGnFiA6K3jH3h6cECJ2S8TD8irEf |
| Neuberger Berman | Diligence/ | 19fmtr7f3714EGe9j8fYFBUHmZ7_aWRz0 |
| Neuberger Berman | Legal/ | 18uJghRNqHmPLklxrRcMFl3as_JOB4Ss3 |
| Francisco Partners | Diligence/ | 1Z82iHprfIyXKdxNeuvwMUSiYXeOCH67X |
| Francisco Partners | Root | 1LdmMpCmQQ5Y1UUDoxNnAZ1toWIrytJp4 |
| Vista Credit | Root | 1ah1x2cD_wIBQrRku7xuLelS52-D0L3I8 |
| CIM | Diligence/ | 1bmZJORaHbvxqYeWAE-KCx4_cy4hZdtsE |
| CIM | Legal/ | 1bdqcBmngeKXBkUf5x5QR6zcggTA5Abuc |
| Covalto | Root | 11v7G67k_XSGVXn7igUTRJlVNeojmcpZO |
| Gramercy | Root | 1k-R1fldUnw90kZpJCS7VR5Yu7SNu0TXn |
| Fasanara | Root | 125_p3cKygzuyh-dbcarZjMP9HI74ohhx |
| Debt Root | — | 1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU |
