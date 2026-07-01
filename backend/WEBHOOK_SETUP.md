# Webhook Receiver — Setup Guide

Replaces the cron-based keyword classifier with:
**Gmail/Slack push → LLM classification (full analyst context) → DeerFlow agent run**

---

## Architecture

```
Gmail (Pub/Sub push) ──┐
                        ├──► webhook_receiver.py ──► Claude classifier ──► DeerFlow run
Slack (Event API)   ──┘              │
                                     └──► Slack DM alert (always)
```

---

## 1. Install dependencies (one time)

```bash
pip install fastapi uvicorn anthropic httpx slack-sdk \
            google-api-python-client google-auth python-dotenv

# Tunnel — pick one:
brew install cloudflared          # recommended (free, no expiry)
# or: brew install ngrok/ngrok/ngrok
```

---

## 2. Start the receiver

```bash
cd /path/to/webhook-files
chmod +x start_webhook.sh
./start_webhook.sh
```

The script launches uvicorn + cloudflared and prints your public URL:
```
════════════════════════════════════════
  Public URL: https://abc123.trycloudflare.com
  Gmail endpoint: https://abc123.trycloudflare.com/webhook/gmail
  Slack endpoint: https://abc123.trycloudflare.com/webhook/slack
════════════════════════════════════════
```

---

## 3. Wire up Gmail (Pub/Sub push)

### 3a. Create Pub/Sub topic (GCP — one time)
```bash
gcloud pubsub topics create gmail-push

# Grant Gmail permission to publish
gcloud pubsub topics add-iam-policy-binding gmail-push \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

### 3b. Create push subscription (update URL when tunnel restarts)
```bash
gcloud pubsub subscriptions create gmail-push-sub \
  --topic gmail-push \
  --push-endpoint https://YOUR-TUNNEL-URL/webhook/gmail \
  --ack-deadline 30
```

### 3c. Activate Gmail watch (expires every 7 days — renew via cron)
```bash
python setup_gmail_watch.py --topic projects/YOUR-GCP-PROJECT/topics/gmail-push
```

**Renew automatically** (add to crontab):
```
0 9 */6 * * cd /path/to/webhook && python setup_gmail_watch.py --topic projects/YOUR-GCP-PROJECT/topics/gmail-push
```

> **Tip:** If you use a static cloudflared tunnel (requires Cloudflare account), the URL never changes and you don't need to update the subscription on restart.

---

## 4. Wire up Slack (Event API)

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → your app → **Event Subscriptions**
2. Enable Events → paste `https://YOUR-TUNNEL/webhook/slack` as the Request URL
3. Slack will send a `url_verification` challenge — the receiver handles it automatically
4. Subscribe to bot events:
   - `message.im` — DMs to the bot
   - `app_mention` — when someone @mentions the bot in a channel
5. Set `SLACK_SIGNING_SECRET` in your `.env` (from app's Basic Information page)

---

## 5. Environment variables

Add to your `.env` (same file the rest of the stack uses):

```bash
# Required for classifier
ANTHROPIC_API_KEY=sk-ant-...

# Required for notifications  
SLACK_BOT_TOKEN=xoxb-...
SLACK_OWNER_USER_ID=U09PQTZ5DHC

# Optional security
SLACK_SIGNING_SECRET=...        # from Slack app settings
PUBSUB_VERIFICATION_TOKEN=...   # set in GCP push subscription config

# DeerFlow
LANGGRAPH_URL=http://localhost:2024   # or your DeerFlow URL

# Gmail
GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REFRESH_TOKEN=...
GOOGLE_CALENDAR_EMAIL=brian.mauck@tryjeeves.com

# Optional
WEBHOOK_PORT=8080   # default
```

---

## 6. Permanent hosting (static tunnel URL)

If you want to set this up once and never update the Pub/Sub subscription URL:

```bash
# Authenticate cloudflared with your Cloudflare account
cloudflared tunnel login

# Create a named tunnel (one time)
cloudflared tunnel create deerflow-webhook

# Route a subdomain
cloudflared tunnel route dns deerflow-webhook webhook.yourdomain.com

# Run with static URL
cloudflared tunnel run deerflow-webhook
```

Then set the Pub/Sub endpoint to `https://webhook.yourdomain.com/webhook/gmail` — permanent.

---

## How classification works

Every inbound message goes through a single Claude call (`claude-3-5-haiku` — fast + cheap) with:
- Brian's full role and context
- All active deals (BBVA, NB, CIM, Gramercy, etc.)
- Known counterparty domains
- Clear rules for what's actionable vs. FYI

**Returns:**
```json
{
  "actionable": true,
  "action_type": "diligence",
  "priority": "high",
  "task_description": "NB requesting updated MX portfolio tape for May month-end",
  "reasoning": "Email from nb.com explicitly requests data tape by EOW"
}
```

If `actionable: true` → DeerFlow thread created + Slack notification sent.  
If `actionable: false` → Slack alert only (no agent run consumed).

---

## Disabling the old cron

Once webhooks are live and working, stop the email monitor cron:

```bash
# Remove from crontab or systemd
# The keyword-based classifier in email_monitor_cron.py is fully replaced
```

---

## Cost estimate

- ~500 emails/month × ~$0.0003/call (Haiku) = **~$0.15/month** for classification
- Only actionable emails (~10-20%) trigger DeerFlow runs
