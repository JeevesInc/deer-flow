---
name: gmail
description: Use this skill when the user asks about emails, wants to search their inbox, read an email, download attachments, or draft a reply. Also use when the user says "email", "inbox", "draft", "reply to", "attachment", "download", or references a specific email thread.
allowed-tools:
  - bash
  - read_file
---

# Gmail -- Search, Read, and Draft Emails

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source. Never guess, assume, extrapolate, or fill gaps with general knowledge. Mark unverified items as **[Needs Confirmation]**.

Access the user's Gmail to search messages, read email content, and create draft replies with attachments.

## Commands

### Search emails
```bash
python /mnt/skills/custom/gmail/gmail_tool.py search "search query"
```
Uses Gmail search syntax. Examples:
- `"from:someone@example.com"` -- emails from a person
- `"subject:invoice"` -- emails with "invoice" in the subject
- `"is:unread newer_than:7d"` -- unread emails from the last 7 days

**`search` takes the result count as a POSITIONAL second arg, never a flag: `search "<query>" 25`.** `cmd_search(sys.argv[2], int(sys.argv[3]))` means `--max 25` dies with `ValueError: invalid literal for int() with base 10: "--max"` — this is the previously undiagnosed `gmail_tool.py line 553 main()` traceback that recurred three times on 2026-08-17/18 (root-caused 2026-08-19). Default is 10. **With no count arg, search returns AT MOST 10 results with no pagination.** A broad query that returns exactly 10 is probably truncated -- re-run with narrower slices (per-day `after:`/`before:`, per-sender) and union the results.

### Read a specific email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>
```

### Download attachments

**HARD RULE -- when a request email carries an attachment, that attachment IS the
specification.** Before building ANY deliverable in response to an inbound request,
run `download <message_id>` and open every attachment. If the requester attached a
workbook, build inside THAT workbook's structure (tab name, row labels, column
layout) -- do not recreate a look-alike, and never infer the format from a slide,
screenshot or a prior version of the report. `read` now prints an `Attachments (n)`
block; an empty body-only read of a request you are about to build against means you
have not verified there is no template.

**Resolve the template from the message that MADE the request — not from an earlier
message in the same thread.** On 2026-08-14 the format question was answered off
`6. Quarterly Chargeoffs - Mastercard.pdf` (a deck slide attached to a prior message)
while the actual request carried `7. Mastercard - Brazil Receivables Report Jun 2026.xlsx`;
the answer to Brian was wrong for three turns until the right message was opened
("Right — I was looking at the wrong message"). Print the message ID and the attachment
filename you are conforming to, so the claim is checkable. Cost two corrections on the 2026-08-14
Mastercard Brazil receivables aging file: the first build copied a deck slide while
Alex's Excel template sat attached to the same email ("No he attached an Excel file
along with the request. That's what he wanted to conform to").

```bash
python /mnt/skills/custom/gmail/gmail_tool.py download <message_id>
python /mnt/skills/custom/gmail/gmail_tool.py download <message_id> --output-dir /mnt/user-data/outputs
```

### Draft a reply
```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "Your reply text here"
python /mnt/skills/custom/gmail/gmail_tool.py draft <message_id> "See attached" --attach /mnt/user-data/outputs/report.xlsx
```

### Draft a new email

**`draft-new` has NO `--cc` flag.** Its arg parser only recognizes `--attach`/`--body-file`; any other flag (e.g. `--cc "x@y.com"`) is silently dropped with no error -- the draft is created successfully but the CC never lands. Cost a missed CC on a FinOps ask (2026-07-08). To CC on a brand-new (non-reply) email: put ALL recipients -- To and Cc together -- comma-separated in the single `to` positional arg, or create the draft then add the Cc field directly in Gmail before sending. Only `draft` (reply) genuinely supports `--cc`. To get a REAL Cc on a new email, write a workspace script that sys.path.inserts the gmail skill dir, imports `gmail_tool`, and calls `_build_mime(body, my_email, to, subject, cc="a@x.com, b@x.com")` then `service.users().drafts().create()` -- `_build_mime` supports `cc`, only the CLI wrapper does not. ALSO: `--body-file` is positional-sensitive -- for `draft-new` the body is argv[4], so passing a body string AND `--body-file` makes the string win and silently ignores the file (2026-07-29: shipped a draft whose entire body was the word PLACEHOLDER; Brian: "maybe actually write the email?"). Correct form: `draft-new "to" "subject" --body-file <path>` with no body string, then verify the created draft body before reporting done. **The same trap exists on `draft` (reply), where the body is argv[3]: passing an empty placeholder string -- `draft <id> "" --body-file <path>` -- makes the EMPTY STRING the body and ships a blank email (2026-08-18, Afsheen DEN-277 reply; Brian: "Email has no body"). `--body-file` must BE argv[3]: `draft <id> --body-file <path>`. Verify the created draft body (drafts().get, decode text/plain) before reporting done.**

```bash
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Subject line" "Body text"
python /mnt/skills/custom/gmail/gmail_tool.py draft-new "recipient@email.com" "Q1 Report" "See attached." --attach /mnt/user-data/outputs/report.xlsx
```

## Attachments

The `--attach` flag accepts:
- Local files (use `/mnt/user-data/outputs/...` -- never hard-code a `threads/<uuid>` path)
- Google Drive files: `drive:<FILE_ID>` (auto-exported for Sheets/Docs/Slides)

## Inbound email pipeline (what actually runs)

**`email_monitor_cron.py` is DISABLED (since 2026-05-20).** Inbound Gmail flows through: Gmail watch -> Pub/Sub -> ngrok -> `webhook_receiver.py` -> Haiku classifier -> Slack proposal DM to Brian. Nothing auto-dispatches from email. If "no proposals are coming in", first check the Gmail watch renewal (`gmail-watch-renew` cron in the gateway) -- an expired watch was a 33-day silent outage once.

If Brian asks to tweak dispatch config: `python /mnt/skills/custom/gmail/dispatch_config_tool.py show` (also `add-keyword`, `toggle`, `add-counterparty`, `set max-concurrent-runs`).

## Background crons (gateway daemon threads)

These live in this skill directory but run inside the gateway via `cron_supervisor.py`:

- **Dreams (`dreams_cron.py`)** -- once nightly in the 2-8am PT window. Behavior/infra review + silent memory consolidation (STRATEGIC_CONTEXT.md, SOUL.md size budget). Applies changes directly -- there is NO approve/discard step. Messages Brian ONLY if it found real problems; a silent dream is a successful dream. **Caveat verified 2026-08-17: the dream run is NOT actually silent.** `autonomous_dispatch.py::_run_and_report` posts `*Dream — completed:*` plus the first 3000 chars of the agent's final answer to Brian's DM unconditionally; `notification=None` suppresses only the start ping. Dream #89's full write-up landed in his DM 2026-08-16 02:10 next to the short review. Until that is fixed, treat a dream's (or latent-learning's) FINAL ANSWER as a message to Brian: short, no consolidation stats, no Drive links, no insights section. Manual run: `uv run python dreams_cron.py once`.
- **EOD review (`eod_review_cron.py`)** -- daily from 17:00 (first tick past the hour). Surfaces unhandled items as a HIGH/MEDIUM/LOW briefing to Brian's Slack DM. **Briefing only -- never creates Gmail drafts** (Brian deletes them). Manual: `uv run python eod_review_cron.py once`.
- **Weekly open items (`weekly_open_items_cron.py`)** -- Monday 08:00 (catches up after downtime). Sweeps the past 7 days for threads still awaiting Brian, live Gmail + Slack only.
- **Proposal learner (`proposal_learner.py`)** -- runs inside the EOD cycle; pairs webhook proposals with Brian's replies and stores classifier patterns in mem0 (`proposal-patterns` namespace).



<!-- Evolution: 2026-08-04 | source: self-improving-agent -->
### Action learner (action_learner.py) -- behavioral cloning from what Brian does unprompted

Sibling of proposal_learner.py. That one learns from how Brian reacts to agent
proposals; this one learns from his own unprompted actions.

- Sources: full sent-mail bodies paired with the message that triggered them and
  the reply latency; inbound threads he never answered (negative triage signal);
  Slack messages he sent.
- Output: pattern sentences in mem0 under user_id `brian-behavior`, tagged
  `[TRIAGE]` / `[VOICE]` / `[SUBSTANCE]`, stored with infer=False so the curated
  sentence survives verbatim.
- Daily incremental pass runs inside eod_review_cron (`_run_action_learner`).
- Consolidation runs weekly from dreams_cron via `consolidate_if_due()`:
  iterative rounds of 40-pattern chunks merge duplicates and resolve
  contradictions (more recent wins), capped at BEHAVIOR_TARGET_MAX facts.
- Chunks MUST stay near 40. Synthesizer thinking tokens count against
  max_tokens, so 110-item chunks truncate mid-JSON and return no usable plan
  (verified 2026-08-03).
- mem0 `get_all()` pages at 20 rows for this namespace, which silently made
  consolidation operate on a sliver. Read the namespace by scrolling Qdrant
  directly (`_all_behavior_memories`), never `get_all`.

```bash
python action_learner.py --status
python action_learner.py --backfill --since 2026-01-01
python action_learner.py --consolidate --force
```
## Rules

- **HARD RULE — there is NO `gmail_tool.get_service()`.** The accessor is private: `_get_service()`
  (and `_get_drive_service()`). Calling the public-looking name raised
  `AttributeError: module 'gmail_tool' has no attribute 'get_service'` in two separate threads on
  2026-08-03 (Vista swag aggregation, Mastercard file reply), each time inside a hand-rolled
  workspace script. Preferred order: (1) call the CLI — `gmail_tool.py search|read|draft|draft-new|
  download` — via subprocess with an argv list; (2) only if that genuinely cannot express the task,
  import the module and use `cmd_draft_reply` / `cmd_draft_new` / `cmd_search`, which already build
  MIME, handle attachments, and set threading headers. Do not re-implement message assembly against
  a raw service object — that is how the non-threaded-reply failure mode comes back.

- **HARD RULE -- an outbound email about a defect must be scoped to the POPULATION, not the one case that surfaced it (2026-08-19).**
  Brian: *"I don't [want] it limited to just this company, because it likely exists in other cases."* He had asked for a note to Christian Lestik about
  ADR WORK SAS (26730); the first draft argued that single company. The correct shape is: state the systemic cause, quantify the full affected set
  (34 invoices / $2.96M / 19 companies), then use the one company as the illustrating example. **Before drafting, run the query that sizes the population
  and put the count, dollars and entity count in the first paragraph.** This is a standing preference of Brian's, not an AIG-specific one -- any
  "draft an email about <specific instance>" ask means "sweep for every instance, then write". If you genuinely cannot size the population, say so in the
  draft rather than silently narrowing it to the one case.
- ALWAYS search first to find the relevant email before reading or replying
- NEVER send emails directly -- only create drafts for the user to review
- ALWAYS reply-all by default unless Brian explicitly says otherwise
- The draft command handles reply-all automatically (fetches To/Cc, excludes Brian)
- **HARD RULE -- if the ask is a REPLY, it MUST be `draft <message_id>`. NEVER `draft-new`.** `draft` supports everything `draft-new` does (`--attach`, `--body-file`) PLUS `--to`/`--cc`, and it is the only command that sets `threadId` + `In-Reply-To` + `References`, i.e. the only one that actually lands in the counterpartys thread. A new email with a matching subject is NOT a reply -- it starts a second thread the counterparty has to reconcile. Brian corrected this twice: 2026-05-12 (episode logged, did not stick) and again 2026-07-24 on the Kroll/Nick P2 closeout ("you didnt put it in the thread, you just drafted a new email"). If you cannot find the message_id: search harder (`search "from:<sender>"`, check Trash/All Mail -- the Kroll thread had been trashed and restored), or ask Brian for the thread. **"Draft an email to <person>" is NOT an instruction to use `draft-new`.** Brian phrases replies as new emails all the time; the wording describes the recipient, not the transport. Third correction 2026-08-19 (AIG/TEX, Christian Lestik): asked to "draft an email to christian, cc pablo", the agent created a standalone email and Brian came back with "no I want this replying to a previous thread where he said he reset the environment." **Before drafting anything outbound, search the counterparty for a live thread on that topic (`search "from:<their email> <topic word>"` and `in:sent`) and default to `draft <message_id>` inside it.** Only use `draft-new` when the search returns nothing, and say so when you report. NEVER fall back to `draft-new`, and never silently -- a non-threaded reply is a failed deliverable, not a partial one. After creating any reply draft, confirm the printed Thread ID matches the source messages threadId before reporting done.
- If someone is mentioned in the body but not CC'd, look up their email via Slack and add them
- When drafting, match Brian's style: direct, short, key points up front
- Show what you drafted and confirm it is in the Drafts folder
- Prefer drive:<ID> for files already on Drive rather than re-uploading. External/counterparty-facing attachments -- naming rule: the goal is a CLEAN, professional filename, not necessarily the unchanged original. Never include (a) internal process/editing words -- Trimmed, Draft, v2, Redacted, WIP, etc. -- these expose internal editing history to the counterparty, and (b) any other counterparty/party name that does not match who you are sending it to (e.g. never send NB a file whose name references BBVA). Rename when needed to strip either of those, but keep the rest of the original name/content descriptor intact.
- **STATUS RULE -- never make ANY statement about a draft's state without re-checking `in:sent` for that thread FIRST.** This covers status reports, EOD/dream restatements, AND in-thread clarifying questions -- on 2026-08-12 the agent asked Brian which action he wanted on the Shalom NB-requirements draft ("the draft is sitting unsent in your Drafts") at 14:46 PT, six hours after Brian had already answered Shalom himself at 08:57 PT. The right move was to check `in:sent`, see the sent reply, and say "you already answered this -- want the stale draft deleted?" A draft whose thread already has an outbound message from Brian is SUPERSEDED, not pending; report it that way and offer to delete it. Never carry a "drafted, awaiting Brian's send" claim forward without re-checking `in:sent` for that thread.** Brian sends drafts himself and that leaves no bot-visible signal, so any later restatement (EOD review, dream comms scan, STRATEGIC_CONTEXT entry) must run `search "in:sent newer_than:Nd"` (or read the thread) first. The Kroll/Hexa reply was reported as sitting unsent for two days after Brian had already sent it 2026-07-24 14:48 PT.
- CLASSIFIER RULE -- reply-all threads: Default to FYI_ONLY for Brian. Check the literal addressee first: the greeting line ("Hi X and team") and/or the To: field of the specific message, not just who is CC'd on the broader thread. Only flag ACTIONABLE for Brian if: (a) he is explicitly named as addressee/greeted, (b) the ask requires info or authority only Brian has with no other Jeeves person (counsel, exec, another CC'd employee) more directly addressed, or (c) the reply is responding to a message Brian himself sent. Being CC'd or present on a reply-all thread where someone else (e.g. outside counsel, another Jeeves exec) is the named addressee = FYI_ONLY, even if the topic is in Brian's general domain (e.g. Alex asking Goodwin's Eliza Chute for a Vista issues-list status, with Brian only CC'd, is FYI_ONLY -- not "Brian's team" or actionable for Brian). Corrected 2026-07-14 after Brian flagged repeated over-flagging: "you're highlighting a lot of messages that don't require action from me."
- **Never paste a list of more than ~5 items (customer names, account IDs, row data, etc.) inline in an email body.** Attach a file instead. Violated on 2026-07-01 (25 customers pasted inline, Brian: "that was ugly af") despite already being a standing rule -- treat as hard, not stylistic, for any draft with list-shaped content.
- **For live deal-negotiation replies (Vista/NB issue lists, term pushback, etc.), never invent Jeeves' business/legal rationale or positions on Brian's behalf** -- not even as a "draft to edit." Two attempts on 2026-07-08 (thin yes/no, then full invented rationale) were both rejected as garbage before Brian wrote it himself. These need Brian's actual arguments. If asked to draft inline replies to a negotiation issues list, ask Brian for his position on each point first (or what specifically was wrong after a first attempt) rather than guessing bigger on a second attempt.



<!-- Evolution: 2026-07-29 | source: dream-74 -->
**EXISTENCE RULE — never tell Brian a document "isn't available" until you have searched
his own sent mail with context words.** On 2026-07-28 the agent reported it could not find
the PwC quality-of-earnings report; Brian replied "False. We have it. We even have the
password." It was in `in:sent` — Brian had emailed it to Jay Berger on 2026-07-20 12:10 PT
with the open password in the body. Before any negative-existence claim about a deal
document, run all of:

- `in:sent` / `from:brian.mauck` for the document type,
- the same search with **context words** — `password`, `attached`, `NRL`, `non-reliance`,
  and the project codename (deal reports often travel as "Project J", not by their title),
- the counterparty's name (the doc is usually named in the covering email, not the subject),
- and a Drive search for files owned by brian.mauck.

Only after all four come back empty may you say it is not available — and say which
searches you ran. The same failure mode hit Drive the same day ("no board deck was built"),
so treat *any* "we don't have X" answer as a claim that requires evidence, not an absence
of evidence.

