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

**Search returns AT MOST 10 results with no pagination.** A broad query that returns exactly 10 is probably truncated -- re-run with narrower slices (per-day `after:`/`before:`, per-sender) and union the results.

### Read a specific email
```bash
python /mnt/skills/custom/gmail/gmail_tool.py read <message_id>
```

### Download attachments
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

**`draft-new` has NO `--cc` flag.** Its arg parser only recognizes `--attach`/`--body-file`; any other flag (e.g. `--cc "x@y.com"`) is silently dropped with no error -- the draft is created successfully but the CC never lands. Cost a missed CC on a FinOps ask (2026-07-08). To CC on a brand-new (non-reply) email: put ALL recipients -- To and Cc together -- comma-separated in the single `to` positional arg, or create the draft then add the Cc field directly in Gmail before sending. Only `draft` (reply) genuinely supports `--cc`. To get a REAL Cc on a new email, write a workspace script that sys.path.inserts the gmail skill dir, imports `gmail_tool`, and calls `_build_mime(body, my_email, to, subject, cc="a@x.com, b@x.com")` then `service.users().drafts().create()` -- `_build_mime` supports `cc`, only the CLI wrapper does not. ALSO: `--body-file` is positional-sensitive -- for `draft-new` the body is argv[4], so passing a body string AND `--body-file` makes the string win and silently ignores the file (2026-07-29: shipped a draft whose entire body was the word PLACEHOLDER; Brian: "maybe actually write the email?"). Correct form: `draft-new "to" "subject" --body-file <path>` with no body string, then verify the created draft body before reporting done.

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

- **Dreams (`dreams_cron.py`)** -- once nightly in the 2-8am PT window. Behavior/infra review + silent memory consolidation (STRATEGIC_CONTEXT.md, SOUL.md size budget). Applies changes directly -- there is NO approve/discard step. Messages Brian ONLY if it found real problems; a silent dream is a successful dream. Manual run: `uv run python dreams_cron.py once`.
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

- ALWAYS search first to find the relevant email before reading or replying
- NEVER send emails directly -- only create drafts for the user to review
- ALWAYS reply-all by default unless Brian explicitly says otherwise
- The draft command handles reply-all automatically (fetches To/Cc, excludes Brian)
- **HARD RULE -- if the ask is a REPLY, it MUST be `draft <message_id>`. NEVER `draft-new`.** `draft` supports everything `draft-new` does (`--attach`, `--body-file`) PLUS `--to`/`--cc`, and it is the only command that sets `threadId` + `In-Reply-To` + `References`, i.e. the only one that actually lands in the counterpartys thread. A new email with a matching subject is NOT a reply -- it starts a second thread the counterparty has to reconcile. Brian corrected this twice: 2026-05-12 (episode logged, did not stick) and again 2026-07-24 on the Kroll/Nick P2 closeout ("you didnt put it in the thread, you just drafted a new email"). If you cannot find the message_id: search harder (`search "from:<sender>"`, check Trash/All Mail -- the Kroll thread had been trashed and restored), or ask Brian for the thread. NEVER fall back to `draft-new`, and never silently -- a non-threaded reply is a failed deliverable, not a partial one. After creating any reply draft, confirm the printed Thread ID matches the source messages threadId before reporting done.
- If someone is mentioned in the body but not CC'd, look up their email via Slack and add them
- When drafting, match Brian's style: direct, short, key points up front
- Show what you drafted and confirm it is in the Drafts folder
- Prefer drive:<ID> for files already on Drive rather than re-uploading. External/counterparty-facing attachments -- naming rule: the goal is a CLEAN, professional filename, not necessarily the unchanged original. Never include (a) internal process/editing words -- Trimmed, Draft, v2, Redacted, WIP, etc. -- these expose internal editing history to the counterparty, and (b) any other counterparty/party name that does not match who you are sending it to (e.g. never send NB a file whose name references BBVA). Rename when needed to strip either of those, but keep the rest of the original name/content descriptor intact.
- **STATUS RULE -- never carry a "drafted, awaiting Brian's send" claim forward without re-checking `in:sent` for that thread.** Brian sends drafts himself and that leaves no bot-visible signal, so any later restatement (EOD review, dream comms scan, STRATEGIC_CONTEXT entry) must run `search "in:sent newer_than:Nd"` (or read the thread) first. The Kroll/Hexa reply was reported as sitting unsent for two days after Brian had already sent it 2026-07-24 14:48 PT.
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

