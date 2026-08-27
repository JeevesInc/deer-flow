---
name: jeeves-eval
description: Use this skill to measure whether the agent is any GOOD, as opposed to whether it is up — scoring the inbound-email classifier against a golden set of real labeled history, judging proposed-action quality, and trending the result. Use it BEFORE and AFTER changing a prompt, a model id, or the mem0 pattern namespace, and whenever asked "did that change help", "is the classifier getting worse", "how accurate are the proposals", or "do we have an eval".
allowed-tools:
  - bash
  - read_file
  - write_file
---

# Jeeves Evaluation Harness

Everything else in this stack measures liveness — is LangGraph up, did the cron
fire, is the gateway answering /health. None of it would notice the agent
quietly getting worse. This skill is the part that notices.

## The one rule

**Never change a classifier prompt, model id, or the `proposal-patterns` mem0
namespace without running this before and after.** A prompt edit that reads like
an improvement can cost ten points of recall, and nothing else in the system
will tell you.

```bash
cd /mnt/skills/custom/jeeves-eval
python run_eval.py --label "before-<change>"
# ... make the change ...
python run_eval.py --label "after-<change>"     # prints the delta vs the previous run
```

## What it measures

| Metric | Meaning |
|---|---|
| `precision` | Of the emails it surfaced, how many Brian actually acted on. Low = noisy proposals. |
| `recall` | Of the emails Brian personally replied to, how many it would have surfaced. Low = **silently missing real work**. |
| `accuracy`, `f1` | The usual combinations. |
| `action_score` | LLM-judge score 0–2 on `proposed_action`, graded against what Brian actually did — not against the judge's taste. |

**Recall is directional, not absolute.** The negative half of the golden set is
mail Brian never replied to, which is the best available proxy for "correctly
ignored" but is not a true labeled negative. Read a recall *move* as real; read
a recall *level* with caution.

## Files

| File | Role |
|---|---|
| `build_golden_set.py` | Builds `cases/golden_set.json` from real labeled history. |
| `run_eval.py` | Replays the **live** classifier over the set and scores it. |
| `judge.py` | LLM-as-judge for `proposed_action` quality. |
| `eval_cron.py` | Weekly run (Sun 04:00) → `_eval_state.json` → Grafana. Silent unless a metric drops ≥0.05. |
| `cases/golden_set.json` | The set itself. Committed, so scores are comparable across time. |

`run_eval.py` imports `classify_with_llm` from `webhook_receiver.py` rather than
copying the prompt. An eval that scores a copy drifts from production the first
time someone edits the real one, then reports green through a regression.

## Label policy — why cases are included or dropped

Ground truth comes from `proposal_outcomes.jsonl`, joined to `proposal_log.jsonl`.

**Positive** (`expected_actionable: true`)
- `approved` / `redirected` — Brian engaged in the Slack thread.
- `handled_elsewhere` **only when `gmail_disposition` is `owner_replied` or
  `owner_replied_pre`** — Brian himself answered the email.

**Negative** (`expected_actionable: false`)
- `rejected` / `dismissed` — surfaced, and he declined or archived it.
- `no_reply` observations never proposed and never replied to.

**Excluded — deliberately**
- `ignored`, `unknown`: no reply anywhere and still in the inbox. That is not
  evidence either way; it may mean noise or a busy week.
- `handled_elsewhere` with `thread_progressed`: a *colleague* replied. That is
  the normal shape of internal mail Brian is CC'd on and says nothing about
  whether he needed to act. The first eval run scored these as false negatives
  while the classifier's reasoning ("Brian merely CC'd") was correct — the
  label was wrong, not the model.

A golden set is only worth as much as its weakest label. When in doubt, drop
the case.

## Rebuilding the set

```bash
python build_golden_set.py --size 60          # default; --seed for a fresh draw
```

Rebuild when the classifier's job changes, or every few months so the set does
not drift away from current inbox reality. **Rebuilding resets comparability** —
scores before and after a rebuild are not directly comparable, so label the
first run after a rebuild clearly.

Before rebuilding, make sure outcome labels are current:

```bash
cd ../gmail && python proposal_learner.py --relabel-ignored --dry-run
```

Before 2026-08-26 every proposal without a Slack reply was labeled `ignored`
unconditionally — 445 of 470 rows were wrong, and the pattern synthesizer was
mining them as classifier failures. If that regresses, every number here becomes
meaningless.

## Reading a result

- **Precision drops** → the classifier got noisier. Suspect a newly written mem0
  pattern that is too broad, or a loosened prompt rule.
- **Recall drops** → it went quiet. Suspect an over-broad *suppression* pattern
  in `proposal-patterns`, or a tightened addressee rule.
- **`action_score` drops while precision holds** → it surfaces the right mail
  and proposes the wrong work. Look at the prompt's action guidance, not the
  actionable rules.
- **Any metric moves without a deploy** → look at mem0. `proposal-patterns` is
  the only input that changes on its own.

Check `jeeves_eval_action_judged_cases` before trusting an `action_score` move.
A mean over five cases moves on noise.

## Dashboard

`http://localhost:3001/d/jeeves-agent-quality` — "Jeeves Agent Quality".

Panels reading **"eval never run"** mean exactly that. `jeeves_eval_days_since_run`
is the gauge to alert on; it reports `9999` when no eval has ever run, so
"never" trips the same alert as "months stale".
