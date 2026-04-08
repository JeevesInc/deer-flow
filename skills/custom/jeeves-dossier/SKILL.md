---
name: jeeves-dossier
description: Use this skill when the user asks to prep for a meeting, review a contact's dossier, check relationship health, or asks about recent interactions with someone. Triggers for "prep me for", "dossier on", "relationship with", "how's my rapport with", "what have I discussed with", "meeting prep", "brief me on", "who is".
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Contact Dossiers — Relationship Intelligence

Build and maintain contact dossiers that track relationship health, communication patterns, sentiment, coaching notes, and open topics. Dossiers persist as JSON files and evolve over time.

**The user is Brian Mauck (brian.mauck@tryjeeves.com).** All dossiers are from Brian's perspective. When prepping for meetings, **never create a dossier for Brian himself** — only for the other attendees. The `prep` command already excludes Brian's email from the attendee list.

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
