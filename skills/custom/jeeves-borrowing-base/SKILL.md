---
name: jeeves-borrowing-base
description: Use this skill for the RECURRING borrowing bases - the CIM MX SOFOM borrowing base (daily) and the NB borrowing base (weekly, Mondays as of the prior Sunday) - plus the daily SOFOM distribution tape and the NB DQ30 daily reading. Triggers on "run the BB", "build the borrowing base", "run the tape", "NB BB", "borrowing base certificate", "SOFOM", "distribution tape", "DQ30". NOT for the monthly servicer reports (jeeves-servicer-report) or the monthly portfolio report (jeeves-portfolio-report).
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Borrowing Bases (recurring)

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

---

## Request Router

Match the request to a pipeline. If it is not in this table, it is **not this skill** — see the
family map above and load the right one.

| Keywords (any match) | Pipeline | Script | Date param | Upload folder |
|---|---|---|---|---|
| borrowing base, BB, run the tape, MX BB, SOFOM, mexico BB | **CIM MX SOFOM Borrowing Base** (daily) | `build_mx.py` → `merge_template.py` | `--end-date YYYY-MM-DD` (default yesterday) | `Debt/CIM/{YYYYMM}/MX/` |
| NB BB, Neuberger, weekly borrowing base, borrowing base certificate, BBC | **NB Borrowing Base** (weekly) | `build_nb.py` | `--as-of <prior Sunday>` | `Debt/Neuberger Berman/Reporting/{YYYYMM}/` |
| distribution tape, F/12339, F/12340 distribution | **SOFOM Distribution Tape** (daily) | `data_tape_sofom.sql` × 3 days | 3-day range ending yesterday | e-mail reply only — never the Master BB |
| DQ30, delinquency reading | **NB DQ30 daily reading** | `backend/scripts/nb_dq30_daily.py` | `--print [YYYY-MM-DD]` | Slack |

**Routed elsewhere:**
- servicer report, Anexo J, Monex, Marisol, carta de instrucción, waterfall → **`jeeves-servicer-report`**
- portfolio report, portfolio reporting, monthly report → **`jeeves-portfolio-report`**
- US BB, bridge BB, tested portfolio report → **RETIRED**, see the family map. Say so; do not build one.

**Date rules:**
- **Never use today's date.** Always use yesterday as the snapshot — even on Mondays (Sunday data is
  available). Do NOT fall back to last Friday on weekends. Scripts hard-reject today/future.
- "run the BB" with no date → yesterday. "BB for 3/27" → `--end-date 2026-03-27`.
- NB is as of the **prior Sunday**, not yesterday.

---

## ⚠️ HARD RULE — a covenant failure KILLS the outbound draft (Brian, 2026-09-16)

His words, escalating twice in one thread: *"NEVER ALLOW IT TO BE CREATED WITH A COVENANT FAILURE
THAT ISN'T FLAGGED"* and, on the disclosure-style draft that followed, *"no we're not fucking
sending that, don't ever draft an email like that."*

- **Any failing Section 7.01 test (Liquidity, DQ30, EBITDA coverage) or a BB that does not cover
  drawn + requested = NO outbound artifact of any kind.** Not a clean email, not a disclosing
  email, not a "heads-up" email, not a draft left in Drafts for Brian to decide on. A draft that
  exists can be sent by accident; the absence of a draft is the control.
- **Fail-closed:** if the covenant check cannot be evaluated (missing tab, unreadable formula,
  build error), treat it as a FAILURE and withhold. Never infer "probably passes."
- **The escalation path is Slack DM to Brian, internal voice, with the failing test, the computed
  value, the threshold and the gap.** Never lender-facing language about a shortfall — Brian owns
  every word a lender sees about a covenant.
- **Never read the pass/fail off the workbook.** openpyxl writes formulas with no cached value, so
  every covenant cell is `None` on read. Re-compute in Python: `nb_covenant_check.check_workbook()`
  (gate wired into `report_scheduler_cron.py` §3.4, 2026-09-16).
- **When Brian adds bank accounts, sweep the WHOLE account set in one pass.** On 2026-09-16 accounts
  were added one at a time across four corrections (Brazil account -> "add all the major accounts"
  -> the four DACA'd accounts), costing 76 turns on one thread. Pull every account off the current
  CICO file, add them all, and state which ones carry `controlled_acct = Yes` and why — the DACA
  scope drives the cash pool and Section 7.01(a), so the flag matters more than the row.
- **"New controlled account" on a certificate = a control agreement Jeeves actually granted since
  Schedule 5.22 — never a diff of workbook columns.** 2026-09-23: the Aug month-end cert listed rows
  22-38 as new / no-longer-excluded because the `excluded_acct` column read that way; Brian: *"BOFA i
  agree with but we didn't give control on the others"* — only BofA 1273 belonged. Before any account
  appears as new/controlled on a lender cert, tie it to an executed DACA/ACA in
  `.deer-flow/wiki/deals/nb_facility_closing.md` or Brian's sent mail; otherwise it stays off.
- **Month-end cert data gaps: fill from the closest available date and say so in one line** (Brian,
  2026-09-23: *"Use the closest date and just fill from that"*; Privy from 9/11 data). Do not stop
  to ask, and do not leave explanatory comments on the lender-facing certificate itself — Brian had
  to say *"Remove errant comments."* Notes go in the Slack delivery, never in the artifact.
- **There is no LibreOffice/soffice on this box** — do not write recalc/convert scripts that shell
  out to it (two failed attempts 2026-09-23, `recalc.py` and `rc.py`). Recompute in Python; for PDF
  page images use a Python PDF library.

---

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

   **Two-line proof, run it every time you patch this file (added 2026-09-16, Dream #121):** compare
   the gateway process start time to the module mtime — `Get-Process python | Select Id,StartTime`
   via `subprocess` vs `Path(cron).stat().st_mtime`. If mtime > StartTime, the edit is NOT live, full
   stop, and saying "the gate is in place" is false. Yesterday the gateway started **11:14:22** and
   `report_scheduler_cron.py` was written at **11:15:12** — 50 seconds later — so both that day's
   changes were assumed dead. **Dream #122 (2026-09-18) claimed both changes were live, on a diff of a pre-patch backup `20260916T181512Z-report_scheduler_cron.py`. Re-checked 2026-09-19: that backup NO LONGER EXISTS on disk (only `.bak_20260914` / `.bak_20260916`, both 2026-09-14 09:15 and both predating the whole patch session — neither contains `_CovenantGate` or `run_aig_tex_file`), so the claim is no longer supported by anything observable. Hard rule: **liveness of an in-process cron edit is a claim with a SHELF LIFE — an evidence artifact that is not in git is gone within days, and a retention-janitor sweep silently turns 'verified' into 'asserted'. Never carry a liveness verdict forward across dreams; either re-derive it from an artifact that still exists, or restart the gateway and stop reasoning about it.** `__pycache__/<mod>.cpython-312.pyc` is timestamp-based: its 16-byte header holds the source mtime+size it was compiled from and `marshal.loads` + `dis` shows the cached bytecode, which answers 'which source version was last compiled' — it does NOT answer what a long-running process holds, because the patch script recompiles on write (here 11:15:12.77, 0.09s after the source). Current state: the 11:15:12 content is definitively NOT in memory; the gate is [Unverified]. Until the gateway is bounced, treat the weekly NB borrowing base as UNGATED and run `nb_covenant_check.py` by hand before any artifact leaves.** The earlier blanket claim that both changes sit in a module version the running
   process never imported. `sc start deerflow-gateway` alone returns **rc=1056 "instance already
   running"** and does nothing; a real restart is `sc stop` -> wait for the port to free -> `sc start`,
   then confirm a NEW "Report scheduler started" line in `deerflow-gateway.err.log`.

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

---

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

---

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

---

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
- **GAP RULE — a NULL column is a finding, not a formatting problem. Report it in the same
  message, never coalesce or omit it.** 2026-09-21: `loc_tape` had `spot_rate` NULL on all 9,051
  rows for `max(dt)`, so `balance_usd` was NULL too; the digest printed the MXN figures and said
  nothing, and Brian had to ask (*"Is spot rate missing from loc tape for the max day?"*), then:
  *"If the spot rate or any other column isn't populated you need to let me know, not just
  coalesce over it."* Before reporting any tape-derived number, check every column the output
  depends on for NULLs and state the gap and its blast radius up front. `data_tape_sofom.sql`
  carries an `fx_fallback` CTE (cfo-org-kb `fc8ac0d`) — the fallback still gets **reported**, never
  used silently.
- **A scheduled run is never "late".** The pipelines fire at the same time every day including
  weekends. If a figure is missing the cause is a data or build failure — diagnose it and say so;
  describing it as "weekend lag" was corrected 2026-09-21 (*"Weekend isn't 'late' it runs at the
  same time every day"*). Equally, a self-evidently broken reading (ineligible == total
  receivables, eligible = 0, BB = n/a) is an incident to flag, not a number to print.
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

---

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

---

## Key Drive Locations

| Location | Folder ID |
|----------|-----------|
| `Debt/` (CIM parent) | `1-0K8EM8slr1_I4Iik7_ZZn0t4SSAMKLU` |
| `Portfolio Reporting/` | `1T6E5zV-rrqZZBre5X3OH0JaztQbsk-QC` |

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



## Section 7.01 test date — month-end only (Brian, 2026-09-28)
Executed CA 8/10/26: 7.01(a) Liquidity is tested "on the last Business Day of each month"; 7.01(b) DQ30
"as of the last day of any calendar month"; 7.01(c) on the last Business Day of a month. Brian: "in the
borrowing base, when it's built, I want the test as of the end of the previous month."
- `build_nb.py` computes the test date (`liquidity_test_date`: last NY Business Day of the as-of month if
  as-of is on/after it, else of the prior month), pulls that date's `<YYMMDD> CICO- Daily balances` email
  plus that date's tape FX, and writes bank_accts cols S (local bal) / T (FX) / U (Liquidity contribution).
  liquidity!E7 = SUM(col U); E15/E16 memo and B22 label point at the test date.
- The BB cash pool (clause (i), col K) stays on the as-of balances — only Liquidity moves.
- `nb_covenant_check.py` uses S*T when bank_accts U1 says "month-end"; detail prints "as of DD-Mon-YY".
- No test-date CICO email, or a controlled account missing from it after nearest-date fill -> build fails
  (fail-closed). Never substitute another date.
- Draw weeks: the CA 4.02(d)-style Delayed Draw condition still needs pro forma 7.01 compliance
  immediately before/after the draw on the latest data — not covered by the month-end test.
