---
name: jeeves-cico
description: Use this skill for the daily CICO balances email (cash in / cash out, "YYMMDD CICO - Daily balances") - triggers, sending, resends, missed-date backfills, and questions like "did the CICO go out", "is there a CICO for <date>", "CICO resend". NOT borrowing bases (jeeves-borrowing-base).
---

# Jeeves CICO Daily Balances Email

Scripts live in `custom/jeeves-borrowing-base/` (`cico_daily_email.py`, `cico_fetch_balances.py`); run them from there.

## Runbook (LIVE 2026-10-08, approved by Brian)
- Trigger: a Slack DM to the bot containing "CICO"/"balance"/"saldo" from Axel (U075NLRHYBX), William Filho (U0A5AT863BL) or Mariana (U09AGT85RSN). Hook: `backend/app/channels/slack.py` `_handle_cico_trigger` -> spawns `cico_daily_email.py --send --trigger-user <id>` (no agent turn; they are deliberately NOT on the DM allowlist). "CICO resend" adds `--force`. Same three IDs are in `slack_dm_monitor_cron.py` IGNORED_USERS (no holding note). slack.py edits need a GATEWAY restart.
- Script pulls the newest `YYMMDD Jeeves - CICO.xlsx` from Axel's OneDrive (FinOps V3/CICO, top level) via Edge+Okta; from session 0 it hops to the desktop session (`zscaler-reauth/session_launcher.py`). Render logic is byte-identical to the approved 10/7 test (table verified identical). Date = latest Bank-balances column with >30 filled rows; subject `{yymmdd} CICO - Daily balances`.
- Sends from Brian's Gmail to Mariana's 10/7 distribution list (12). One send per balance date (`_cico_email_state.json`); a repeat trigger for the same date tells the sender the latest date is unchanged. Confirmation (date, total cash, $1M+ moves) goes to the trigger user and Brian; failures too. Manual test: `cico_daily_email.py --send --no-fetch --to brian.mauck@tryjeeves.com`.
- **Missed date = backfill it yourself, no asking.** When a run failed or anyone asks "is there a CICO for <date>?" and it didn't go out: run the script for that date (that day's CICO file via `--file`; confirm the subject date in a `--to brian` dry send or the render before the real send), send oldest-first, then tell the trigger user / reply-all to the asker (gmail SKILL send rule) that you sent it and why it was late. 2026-11-01: Brian had to push twice ("Could you just check if you can run the 10/29?" -> "So why don't you do it").
