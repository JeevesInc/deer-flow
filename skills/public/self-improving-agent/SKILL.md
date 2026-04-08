---
name: self-improving-agent
description: >
  A universal self-improving agent that learns from ALL skill experiences. Uses multi-memory
  architecture (semantic + episodic + working) to continuously evolve skill files and workflows.
  Triggers when: user says "self-improve", "learn from this", "update the skill", "what did you
  learn", or after any significant skill interaction that produced reusable insights.
allowed-tools:
  - bash
  - write_file
  - read_file
  - str_replace
---

# Self-Improving Agent

> "An AI agent that learns from every interaction, accumulating patterns and insights to continuously improve its own capabilities."

## Overview

This is a **universal self-improvement system** that learns from ALL skill experiences. It implements a complete feedback loop with:

- **Multi-Memory Architecture**: Semantic + Episodic + Working memory
- **Self-Correction**: Detects and fixes skill guidance errors
- **Self-Validation**: Periodically verifies skill accuracy
- **Evolution Markers**: Traceable changes with source attribution

## Skill Management Tool

Use the `skill_manage.py` CLI for all skill operations:

```bash
# List all skills
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py list

# Read a skill
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py read jeeves-redshift

# Create a new skill (when you discover a reusable workflow)
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py create new-skill-name \
  --description "When to trigger and what it does" \
  --content "## Instructions\n\nThe skill content here..."

# Patch an existing skill (surgical update)
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py patch jeeves-redshift \
  --old "old guidance text" \
  --new "corrected guidance text"

# Append to a skill section
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py append jeeves-redshift \
  --section "## Critical Rules" \
  --content "- New rule learned from experience"

# Log an episode
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py log jeeves-redshift \
  --episode '{"situation": "what happened", "root_cause": "why", "solution": "fix", "lesson": "takeaway"}'

# View learned patterns
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py patterns
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py patterns --skill jeeves-redshift

# Search past episodes
python /mnt/skills/public/self-improving-agent/scripts/skill_manage.py search "date lag"
```

## Session Search (Past Conversations)

Search across conversation history in checkpoints.db:

```bash
python /mnt/skills/public/self-improving-agent/scripts/session_search.py "borrowing base error"
python /mnt/skills/public/self-improving-agent/scripts/session_search.py "redshift" --limit 5
```

## When This Activates

### Autonomous Triggers (self-trigger without being asked)

You MUST run the self-improvement loop when ANY of these conditions are met:

| Condition | Why | Action |
|-----------|-----|--------|
| **Task used 5+ tool calls and succeeded** | Complex task = reusable procedure | Consider creating a new skill or patching an existing one |
| **You recovered from an error** | Error recovery = valuable lesson | Log episode + patch the skill that had wrong/incomplete guidance |
| **User corrected your output** | Corrections = highest-value signal | Log episode + patch skill + decrease confidence on old pattern |
| **You discovered a non-obvious workflow** | Novel solution = future time savings | Log episode + consider new skill creation |
| **A query/script pattern was reused 3+ times** | Repetition = skill candidate | Extract into a reusable skill or add to SQL library |

### Manual Triggers

- User says "self-improve", "learn from this", "update the skill"
- User says "what did you learn", "analyze today's experience"
- User asks to improve a specific skill

## Memory Architecture

All memory files live under `/mnt/skills/public/self-improving-agent/memory/`:

### 1. Semantic Memory (`memory/semantic-patterns.json`)

Stores **abstract patterns and rules** reusable across contexts:

```json
{
  "patterns": {
    "pat-2026-04-05-001": {
      "id": "pat-2026-04-05-001",
      "name": "Pattern Name",
      "source": "user_feedback|implementation_review|error_recovery",
      "confidence": 0.85,
      "applications": 0,
      "created": "2026-04-05",
      "last_applied": null,
      "category": "redshift_query|borrowing_base|redline|dossier|drive|gmail|calendar|general",
      "pattern": "One-line summary",
      "problem": "What problem does this solve?",
      "solution": "How to solve it",
      "target_skills": ["jeeves-redshift", "jeeves-borrowing-base"]
    }
  }
}
```

### 2. Episodic Memory (`memory/episodic/`)

Stores **specific experiences and what happened**:

```json
{
  "id": "ep-2026-04-05-001",
  "timestamp": "2026-04-05T10:30:00Z",
  "skill": "jeeves-redshift",
  "situation": "User asked for portfolio balance but query returned nulls",
  "root_cause": "Used today's date instead of yesterday — Redshift data lags by 1 day",
  "solution": "Always use yesterday's date for 'current' or 'latest' queries",
  "lesson": "Redshift data only available through yesterday",
  "related_pattern": "redshift_date_lag",
  "user_feedback": {
    "rating": null,
    "comments": null
  }
}
```

### 3. Working Memory (`memory/working/`)

Stores **current session context** (ephemeral):

```json
{
  "session_id": "2026-04-05-slack-thread-abc",
  "active_skills": ["jeeves-redshift", "google-drive"],
  "errors_encountered": [],
  "patterns_applied": [],
  "insights_pending": []
}
```

## Evolution Priority Matrix

Map insights to the correct Jeeves skill:

| Trigger | Target Skill | Priority | Action |
|---------|--------------|----------|--------|
| SQL query fix or optimization | jeeves-redshift | High | Update query examples or rules |
| Borrowing base workflow insight | jeeves-borrowing-base | High | Update pipeline steps |
| Redline/contract lesson | jeeves-redline | High | Update comparison rules |
| Dossier/relationship insight | jeeves-dossier | High | Update synthesis approach |
| Drive file management lesson | google-drive | Medium | Update naming/org rules |
| Gmail workflow improvement | gmail | Medium | Update search/draft patterns |
| Calendar scheduling insight | google-calendar | Medium | Update event handling |
| Analytics query pattern | jeeves-analytics | Medium | Add to query library |
| Slack interaction lesson | slack-search | Low | Update search patterns |
| General workflow improvement | (SOUL.md note) | Low | Flag for manual review |

## Self-Improvement Process

### Phase 1: Experience Extraction

After any skill interaction, extract:

```yaml
What happened:
  skill_used: {which skill}
  task: {what was being done}
  outcome: {success|partial|failure}
  thread_context: {Slack thread ID or session}

Key Insights:
  what_went_well: [what worked]
  what_went_wrong: [what didn't work]
  root_cause: {underlying issue if applicable}

User Feedback:
  explicit: {any direct feedback from Brian}
  implicit: {did he ask for corrections? did he accept the output?}
```

### Phase 2: Pattern Abstraction

Convert experiences to reusable patterns:

| Concrete Experience | Abstract Pattern | Target Skill |
|--------------------|------------------|--------------|
| "Query returned nulls because used today's date" | "Always use yesterday for current data" | jeeves-redshift |
| "Borrowing base had wrong month folder" | "Report date = 1st, folder = current month" | jeeves-borrowing-base |
| "Redline struck whole sentences" | "Strike only changed characters" | jeeves-redline |
| "Dossier missed Slack context" | "Always search Slack before synthesis" | jeeves-dossier |

**Abstraction Rules:**

```yaml
If experience_repeats 3+ times:
  pattern_level: critical
  action: Add to skill's "Critical Rules" section

If solution_was_effective:
  pattern_level: best_practice
  action: Add to skill's "Best Practices" section

If user_rating >= 7 or user accepted without correction:
  pattern_level: strength
  action: Reinforce this approach

If user_rating <= 4 or user corrected the output:
  pattern_level: weakness
  action: Add to "What to Avoid" section
```

### Phase 3: Skill Updates

Update the appropriate skill files with **evolution markers**:

```markdown
<!-- Evolution: 2026-04-05 | source: ep-2026-04-05-001 | confidence: 0.85 -->
## Pattern Added: Always verify date is not today for Redshift queries
```

**Correction Markers** (when fixing wrong guidance):

```markdown
<!-- Correction: 2026-04-05 | was: "Use current date" | reason: Redshift data lags 1 day -->
## Corrected: Use yesterday's date for all "current" or "latest" queries
```

### Phase 4: Memory Consolidation

1. **Update semantic memory** — add/update pattern in `memory/semantic-patterns.json`
2. **Store episodic memory** — write episode to `memory/episodic/YYYY-MM-DD-{skill}.json`
3. **Update pattern confidence** — increment `applications` count, adjust `confidence` based on feedback
4. **Prune outdated patterns** — remove patterns with confidence < 0.3 and no recent applications

## Self-Correction Workflow

Triggered when:
- A bash command returns non-zero exit code while following skill guidance
- User explicitly corrects the output ("no, that's wrong", "don't do it that way")
- A query returns unexpected/empty results

**Process:**

1. **Detect Error** — What skill guidance was being followed?
2. **Verify Root Cause** — Was the guidance incorrect, misinterpreted, or incomplete?
3. **Apply Correction** — Update skill file with corrected guidance + correction marker
4. **Validate** — If possible, test the corrected approach
5. **Log** — Store correction in episodic memory with `outcome: "self_correction"`

## Self-Validation

Periodically (when user requests or during quiet periods), validate skill accuracy:

```markdown
## Validation Report

**Date**: [YYYY-MM-DD]
**Scope**: [skill(s) validated]

### Checks
- [ ] SQL examples still produce correct results
- [ ] File paths and naming conventions match current Drive structure
- [ ] API endpoints and auth methods still work
- [ ] No duplicated or conflicting guidance across skills

### Findings
- [Finding 1]

### Actions
- [Action 1]
```

## Executing Self-Improvement

When this skill activates, follow this workflow:

```bash
# 1. Read current semantic patterns
python -c "
import json, os
path = os.path.join(os.environ.get('SKILLS_PATH', '/mnt/skills'), 'public/self-improving-agent/memory/semantic-patterns.json')
if os.path.exists(path):
    with open(path) as f:
        data = json.load(f)
    print(json.dumps(data, indent=2))
else:
    print('No patterns yet')
"

# 2. Read recent episodic memories
ls /mnt/skills/public/self-improving-agent/memory/episodic/ 2>/dev/null || echo "No episodes yet"

# 3. After extracting insights, write new episode
python -c "
import json, os
from datetime import datetime

episode = {
    'id': 'ep-YYYY-MM-DD-NNN',
    'timestamp': datetime.now().isoformat(),
    'skill': 'skill-name',
    'situation': 'what happened',
    'root_cause': 'why',
    'solution': 'how it was fixed',
    'lesson': 'reusable takeaway',
    'related_pattern': 'pattern_id_or_null',
    'user_feedback': {'rating': None, 'comments': None}
}

ep_dir = os.path.join(os.environ.get('SKILLS_PATH', '/mnt/skills'), 'public/self-improving-agent/memory/episodic')
os.makedirs(ep_dir, exist_ok=True)

filename = f'{datetime.now().strftime(\"%Y-%m-%d\")}-{episode[\"skill\"]}.json'
filepath = os.path.join(ep_dir, filename)
with open(filepath, 'w') as f:
    json.dump(episode, f, indent=2)
"

# 4. Update semantic patterns
python -c "
import json, os

path = os.path.join(os.environ.get('SKILLS_PATH', '/mnt/skills'), 'public/self-improving-agent/memory/semantic-patterns.json')
if os.path.exists(path):
    with open(path) as f:
        patterns = json.load(f)
else:
    patterns = {'patterns': {}}

# Add or update pattern here
# patterns['patterns']['pat-id'] = { ... }

with open(path, 'w') as f:
    json.dump(patterns, f, indent=2)
"
```

## Best Practices

### DO
- Learn from EVERY skill interaction, not just failures
- Extract patterns at the right abstraction level (not too specific, not too vague)
- Update multiple related skills when a pattern applies broadly
- Track confidence and application counts
- Ask Brian for feedback on improvements when appropriate
- Use evolution/correction markers for traceability
- Validate guidance before applying broadly
- Keep skill files clean — don't bloat them with low-confidence patterns

### DON'T
- Over-generalize from a single experience
- Update skills without confidence tracking
- Ignore negative feedback or corrections
- Make changes that break existing, validated functionality
- Create contradictory patterns across skills
- Update skills without understanding the full context
- Add patterns that duplicate what's already in SOUL.md or existing skill rules

## References

- [SimpleMem: Efficient Lifelong Memory for LLM Agents](https://arxiv.org/html/2601.02553v1)
- [A Survey on the Memory Mechanism of Large Language Model Agents](https://dl.acm.org/doi/10.1145/3748302)
- [Lifelong Learning of LLM based Agents](https://arxiv.org/html/2501.07278v1)
- Based on [charon-fan/agent-playbook](https://github.com/charon-fan/agent-playbook) self-improving-agent skill
