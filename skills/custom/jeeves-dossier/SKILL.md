---
name: jeeves-dossier
description: Use this skill when the user asks to prep for a meeting, review a contact's dossier, check relationship health, or asks about recent interactions with someone. Triggers for "prep me for", "dossier on", "relationship with", "how's my rapport with", "what have I discussed with", "meeting prep", "brief me on", "who is".
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Contact Dossiers — Relationship Intelligence

> **Accuracy is mandatory.** Every fact, number, and claim in your output must come from a verified source — a Redshift query result, a document you have actually read, or an explicit user statement. Never guess, assume, extrapolate, or fill gaps with general knowledge. If you do not have a source, say so. Mark unverified items as **[Needs Confirmation]**. Getting it wrong is worse than leaving it blank.


Build and maintain contact dossiers that track relationship health, communication patterns, sentiment, coaching notes, and open topics. Dossiers persist as JSON files and evolve over time.

**The user is Brian Mauck (brian.mauck@tryjeeves.com).** All dossiers are from Brian's perspective. When prepping for meetings, **never create a dossier for Brian himself** — only for the other attendees. The `prep` command already excludes Brian's email from the attendee list.

> **Honcho peer cards — use by default, not optionally.** All interaction history across Gmail, Slack, Calendar, and Gemini notes is stored in Honcho, which synthesizes it into a rich dialectic peer card (relationship trend, communication style, open threads). Whenever you work with a known contact — prepping a meeting, **drafting or replying to an email to/from them**, or handling a **Slack** thread that involves them — pull their peer card FIRST for relationship context. This applies to email and Slack equally; it is not just for explicit meeting prep.
> ```bash
> python /mnt/skills/custom/honcho-peers/peer_card_tool.py ingest <email> --days 30   # refresh recent history first
> python /mnt/skills/custom/honcho-peers/peer_card_tool.py read <email>                # full peer card
> python /mnt/skills/custom/honcho-peers/peer_card_tool.py query <email> "<question>"  # targeted question
> ```
> Use the card to match the contact's communication style and surface what's owed before you write. For ONA questions ("who is most connected?", "key bridges"), use the `honcho-peers` skill.

## Workflow: Prep for a meeting

1. **Get meeting attendees:**
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py prep next
```
Or for a specific event:
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py prep <event_id>
```

2. **For each attendee email, gather raw interaction data:**
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py gather <email> --days 30
```
This fans out to Calendar, Gmail, Slack, and Gemini meeting notes in one call.

3. **Read existing dossier (if any):**
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py read <email>
```

4. **Synthesize** — merge the gathered data into the dossier (see Synthesis Instructions below). Write the updated JSON to a temp file, then save:
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py write <email> --file /mnt/user-data/outputs/dossier_tmp.json
```

5. **Present the briefing** to the user — concise summary of each attendee's dossier highlights, coaching notes, and open threads.

## Workflow: Review a contact

```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py read <email>
```
Present the dossier in a readable format. If no dossier exists, offer to create one by gathering data.

## Workflow: Search Slack for context

If you need more context on a conversation or topic:
```bash
python /mnt/skills/custom/slack-search/slack_tool.py search "from:@person topic" --days 30
```

## Dossier JSON Schema

When creating or updating a dossier, produce JSON in this format:

```json
{
  "email": "person@company.com",
  "name": "Display Name",
  "last_updated": "2026-03-30T14:00:00",
  "relationship": {
    "health_score": 7,
    "trend": "stable",
    "summary": "Brief relationship description"
  },
  "communication_style": {
    "observations": [
      "Prefers concise bullet points",
      "Usually responds within 30 min on Slack"
    ],
    "formality_trend": "stable"
  },
  "recent_interactions": [
    {
      "date": "2026-03-28",
      "type": "meeting",
      "source": "gemini_notes",
      "summary": "Discussed term sheet revisions",
      "sentiment": "positive",
      "key_topics": ["term sheet", "pricing"],
      "action_items": ["Send updated numbers"]
    }
  ],
  "coaching_notes": [
    {
      "date": "2026-03-28",
      "note": "Person seemed rushed — was cut off twice",
      "suggestion": "Give more space, ask for input directly",
      "category": "emotional_awareness"
    }
  ],
  "open_threads": [
    {
      "topic": "Term sheet revision",
      "last_mentioned": "2026-03-28",
      "status": "pending",
      "context": "Waiting on updated pricing"
    }
  ],
  "meeting_frequency": {
    "meetings_30d": 5,
    "cadence": "weekly",
    "last_meeting": "2026-03-28"
  }
}
```

## Known failure mode — verify before claiming "no direct communication"

The automated dossier_cron.py synthesis (Claude call) has been observed to gather 30 Slack items for a contact (including real DMs) and then still write "No direct 1:1 communication observed" in the summary — the LLM synthesis step silently dropped the slack data. **Before ever writing or repeating a "no direct 1:1 contact" or "structural only" claim, cross-check it**: run `slack_tool.py history <user-id>` and `slack_tool.py search "<name>"` directly (the code-fenced tool names in this paragraph were previously stripped to empty backticks, which made the rule unactionable). If DMs exist, the claim is wrong — do not trust the synthesized dossier text at face value when the gathered data (the item count printed by `dossier_tool.py gather`) shows items but the written summary claims otherwise. When Brian pushes back on a dossier claim ("how is this even true"), always re-verify against raw Slack/Gmail search before defending or repeating the claim.

## Synthesis Instructions

When synthesizing a dossier, you are a **relationship analyst**. Read raw interaction data and extract actionable interpersonal intelligence.

### Relationship Health (1-10 score + trend)
- Score based on: response times, tone warmth, meeting frequency, topic depth
- Trend: `improving` / `stable` / `cooling` — compare recent interactions to older ones
- Flag concerning signals (ignored messages, cancelled meetings, formal tone shift)
- Default to 6 if insufficient data — be conservative

### Communication Style Observations
- How do they write? (terse vs verbose, formal vs casual, emoji usage)
- How has their style changed over time with the user specifically?
- Response patterns (quick responder? leaves you on read?)

### Sentiment & Emotional Signals
- Read between the lines of meeting notes and messages
- Flag emotional cues: frustration, enthusiasm, disengagement, stress
- Note context (end of quarter = everyone stressed, don't over-index)

### Coaching Notes
- Actionable suggestions: "Alex seemed rushed last meeting — next time ask how he's doing first"
- Communication improvements: "You tend to send walls of text to Victor — he prefers bullet points"
- Relationship opportunities: "Maria mentioned her daughter's recital — ask how it went"
- Categories: `emotional_awareness`, `communication_style`, `relationship_building`, `follow_up`

### Open Threads
- Topics raised but not resolved
- Action items assigned to user or contact
- Mark resolved when follow-up evidence exists in newer data

**HARD RULE — never surface a stored `open_threads` item in a meeting-prep briefing without
re-verifying it as still open, and never state who owes whom without checking message direction.**
On 2026-09-02 (thread b84ba7d2) the Weekly Risk Meeting prep listed items as owed by Brian that
were already discharged, and Brian corrected it twice in one message: *"I gave chin the dashboard
in credit models channel. and the last message in my dm with Ivan is from me."* Two distinct root
causes, both must be checked before the briefing goes out:

1. **Resolution evidence lives in CHANNELS, not just DMs.** The gather step is DM-weighted, so a
   deliverable handed over in a shared channel (e.g. #credit-model-building) never clears the
   open thread. For every open item, run a channel-wide Slack search on the contact's name AND
   the artifact noun (dashboard, model, tape, memo) since the item's created date — not just a
   DM history pull. If Brian posted it anywhere, the item is CLOSED.
2. **Ball-in-court is determined by the LAST message and its sender.** Before writing "unanswered
   by you" / "you owe", read the last message in the thread and check the author. If the last
   message is from Brian, the ball is with the CONTACT — say so, or say nothing. Never infer
   direction from the age of the item.

If either check cannot be run, omit the open-item line entirely. A briefing with fewer, correct
lines beats one that tells Brian he owes something he already delivered.

### Rules
- **NEVER fabricate interactions** — only cite what's in the gathered data
- **Preserve existing coaching notes** — append new ones, don't overwrite
- Keep `recent_interactions` to the last **10 entries** (drop oldest)
- If gathered data is empty for a source, skip it — don't flag it as concerning
- When updating, **MERGE** with existing dossier — don't replace the whole thing
- `health_score` should be conservative — default to 6 if insufficient data
- Present briefings concisely — bullet points, not walls of text

## Other commands

### List all dossiers
```bash
python /mnt/skills/custom/jeeves-dossier/dossier_tool.py list
```

### Look up someone's Slack ID
```bash
python /mnt/skills/custom/slack-search/slack_tool.py lookup person@company.com
```
