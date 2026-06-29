---
name: latent-learning
description: Use this skill when asked to /learn, "analyze usage patterns", "propose new agents", or "learn from history". Reads memory facts and thread history to identify recurring task domains, then drafts specialist sub-agents for approval.
allowed-tools:
  - bash
  - write_file
  - read_file
---

# Latent Learning Skill

Analyze completed work to discover recurring task patterns, then propose specialist agents tailored to each domain.

## When to Use

- Triggered by `/learn` command (manual or nightly cron)
- When the user asks to "propose agents", "learn from history", or "analyse what I do"

## Process

### Step 1 — Gather signal

Fetch memory and existing agent inventory in parallel:

```bash
# Memory facts and summaries
curl -s http://localhost:8001/api/memory | python3 -c "
import json, sys
d = json.load(sys.stdin)
print('=== WORK CONTEXT ===')
print(d.get('workContext',''))
print('=== RECENT MONTHS ===')
print(d.get('recentMonths',''))
print('=== FACTS ===')
for f in d.get('facts',[]):
    if f.get('confidence',0) >= 0.7:
        print(f'[{f[\"category\"]}] {f[\"content\"]}')
"

# Existing live agents (avoid duplicates)
AGENTS_DIR="${DEER_FLOW_HOME:-$HOME/.deer-flow}/agents"
ls "$AGENTS_DIR" 2>/dev/null || echo "(none)"

# Existing drafts (avoid re-drafting)
DRAFT_DIR="${DEER_FLOW_HOME:-$HOME/.deer-flow}/agents-draft"
ls "$DRAFT_DIR" 2>/dev/null || echo "(none)"

# Thread workspace summary (recent files = recent task types)
ls /mnt/user-data/workspace/ 2>/dev/null | head -30
```

### Step 2 — Identify task domains

Think carefully about what you've read. Look for:
- **Repeated tool combinations** — e.g. "Redshift + SQL + analysis" always appear together
- **Recurring output types** — reports, dashboards, emails, models
- **Domain-specific vocabulary** — names of systems, data sources, processes
- **Frequency signal** — facts/history items that appear multiple times or span months

Cluster into **3–6 candidate domains**. For each, estimate:
- How often it appears (rough frequency: occasional / regular / frequent)
- What tools/skills it needs
- Whether a specialist would be meaningfully better than the general agent
- Whether an agent already exists that covers it

**Only propose agents for domains that are:**
1. Clearly recurring (not one-off)
2. Not already covered by an existing agent
3. Genuinely benefiting from a specialist persona (domain knowledge, guardrails, tool focus)

### Step 3 — Draft agent files

For each approved domain, create two files under `$DRAFT_DIR/<agent-name>/`:

**`config.yaml`** format:
```yaml
name: <kebab-case-name>
description: "<One sentence: when should the lead agent delegate to this specialist? Be specific about task types.>"
model: null  # inherit default
tool_groups:
  - <relevant group names, e.g. search, code, data>
```

**`SOUL.md`** format — this is the specialist's system prompt. Write it as if briefing the agent directly:

```markdown
# <Agent Name>

You are a specialist in <domain>. You are called by the lead agent when tasks involve <specific triggers>.

## Your expertise

<2-3 sentences describing what you know deeply: data sources, systems, methodologies, domain vocabulary>

## Behavioral guardrails

- **Accuracy first**: Every fact, number, or claim must come from a verified source. Never guess or fill gaps with general knowledge. If you don't have a source, say so.
- **Scope discipline**: Stay within your domain. If the task requires capabilities outside <domain>, flag it back to the orchestrator rather than attempting it poorly.
- **Output quality**: <domain-specific quality bar, e.g. "SQL must be validated before presenting", "financial figures must cite their source table/column">

## Tools and data sources

- <primary tool 1>: <how you use it>
- <primary tool 2>: <how you use it>
- <relevant skill if any>: <when to load it>

## When you are done

Return: (1) a concise summary of what you accomplished, (2) key findings or outputs, (3) file paths if you created artifacts, (4) any issues or limitations encountered.
```

Write the files:
```bash
DRAFT_DIR="${DEER_FLOW_HOME:-$HOME/.deer-flow}/agents-draft"
mkdir -p "$DRAFT_DIR/<name>"
# write config.yaml and SOUL.md with write_file or heredoc
```

### Step 4 — Report for Slack approval

After writing all drafts, reply with a structured summary:

```
🧠 **Latent Learning — Draft Agents Proposed**

I analysed memory and recent work history. Here's what I found:

---
**Agent: `<name>`** — <one-line description>
*Domain*: <what it covers>
*Signal*: <why this is recurring — e.g. "appears in 8 memory facts across 3 months">
*Drafts written to*: `agents-draft/<name>/`

---
[repeat for each draft]

---
To activate an agent: `!promote <name>`
To reject a draft: `!reject <name>`
To see the full spec: ask me "show draft <name>"
```

If no new agents are warranted (everything is already covered or patterns aren't strong enough), say so clearly with your reasoning.

## Quality bar

A good specialist agent:
- Has a narrower but deeper focus than "general-purpose"
- Has domain-specific guardrails that prevent common mistakes in that area
- Knows which tools/skills to reach for without being told
- Can produce higher-quality outputs than the general agent for its domain

A bad specialist agent:
- Is just the general agent with a fancy name
- Covers too broad a domain to have meaningful specialisation
- Duplicates an existing agent with trivial differences
