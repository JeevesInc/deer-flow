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

## Specific Known Traps

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

- **Tracker and Folder alignment is mandatory**: Every item in the tracker must map to exactly one folder. Every folder must have a corresponding tracker row. Reconcile before declaring complete.
- **No duplicate files**: When a new version of a file is uploaded, delete the old version. Drive folders must contain exactly one copy of each document.
- **No cross-contamination**: Never reference BBVA DD folder links in a Vista tracker (or vice versa). Each counterparty tracker must reference only that counterparty VDR/folder structure.
- **Empty folders are not done**: A folder with no file is an open item, not a placeholder. Either upload the file or mark the tracker row RED.
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
