---
name: jeeves-sql-library
description: "DEPRECATED — use the `cfo-org-kb` skill instead. This skill redirects to the local CFO knowledge base which contains all SQL templates and Python analytics scripts."
allowed-tools:
  - bash
  - read_file
---

# Jeeves SQL Library — REDIRECTED

**This skill has been replaced by `cfo-org-kb`.**

All SQL templates and Python analytics scripts now live locally in the `cfo-org-kb` skill directory. Load the `cfo-org-kb` skill instead.

**Local path:** `/mnt/skills/custom/cfo-org-kb/`

Quick access:
```bash
# List available SQL files
ls /mnt/skills/custom/cfo-org-kb/sql/

# List available Python scripts
ls /mnt/skills/custom/cfo-org-kb/scripts/

# Read a specific SQL template
cat /mnt/skills/custom/cfo-org-kb/sql/data_tape.sql
```
