# Appendix

## Self-Validation Report Template

```markdown
## Validation Report

**Date**: [YYYY-MM-DD]
**Scope**: [skill(s) validated]

### Checks
- [ ] SQL examples compile and return expected results
- [ ] File paths and naming conventions match current Drive structure
- [ ] API endpoints and auth methods still work
- [ ] No duplicated or conflicting guidance across skills
- [ ] Redshift table schemas still accurate

### Findings
- [Finding 1]
- [Finding 2]

### Actions
- [Action 1]
- [Action 2]
```

## Memory File Structure

```
skills/public/self-improving-agent/memory/
├── semantic-patterns.json      # Abstract patterns and rules
├── episodic/                   # Specific experiences
│   ├── 2026-04-05-jeeves-redshift.json
│   ├── 2026-04-05-jeeves-borrowing-base.json
│   └── ...
└── working/                    # Current session context (ephemeral)
    └── current_session.json
```

## Continuous Learning Metrics

Track these in `memory/metrics.json`:

```json
{
  "metrics": {
    "patterns_learned": 0,
    "patterns_applied": 0,
    "skills_updated": 0,
    "avg_confidence": 0.0,
    "self_corrections": 0,
    "last_validation": null
  }
}
```

## Human-in-the-Loop Feedback

### Feedback Collection Template

```markdown
## Self-Improvement Summary

I've learned from our session and updated:

### Updated Skills
- `skill-name`: What was changed

### Patterns Extracted
1. **pattern_name**: Description

### Confidence Levels
- New patterns: 0.85 (needs validation)
- Reinforced patterns: 0.95 (well-established)

### Your Feedback
- Were the updates helpful?
- Should I apply this pattern more broadly?
- Any corrections needed?
```

### Feedback Integration

```yaml
User Feedback:
  positive (accepted without correction, explicit praise):
    action: Increase pattern confidence by 0.05
    scope: Expand to related skills

  neutral (no feedback, or mixed signals):
    action: Keep pattern, gather more data
    scope: Current skill only

  negative (user corrected output, explicit criticism):
    action: Decrease confidence by 0.1, revise pattern
    scope: Remove from active patterns if confidence < 0.3
```

## Jeeves Skill Mapping

| Domain | Skill File | Key Areas to Evolve |
|--------|-----------|-------------------|
| Data warehouse | jeeves-redshift | SQL patterns, date rules, table schemas |
| Analytics | jeeves-analytics | Query templates, metric definitions |
| Borrowing base | jeeves-borrowing-base | Pipeline steps, date conventions, Excel formatting |
| Contract review | jeeves-redline | Comparison rules, surgical edits, tracked changes |
| Relationships | jeeves-dossier | Synthesis prompts, data sources, scoring |
| Capital markets | jeeves-capital-markets | Workspace structure, report templates |
| SQL library | jeeves-sql-library | Reusable query patterns |
| Drive | google-drive | File naming, folder structure |
| Email | gmail | Search patterns, draft conventions |
| Calendar | google-calendar | Event handling, meeting prep timing |
| Slack search | slack-search | Search operators, user resolution |

## References

- [SimpleMem: Efficient Lifelong Memory for LLM Agents](https://arxiv.org/html/2601.02553v1)
- [A Survey on the Memory Mechanism of Large Language Model Agents](https://dl.acm.org/doi/10.1145/3748302)
- [Lifelong Learning of LLM based Agents](https://arxiv.org/html/2501.07278v1)
