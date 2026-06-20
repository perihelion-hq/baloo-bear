# Thread Conversation Agent

When a developer replies to a Rocky inline review comment, a lightweight agent reads the conversation and responds appropriately — explaining, suggesting fixes, or conceding when the developer's reasoning is valid.

## How It Works

1. Developer replies to a Rocky inline comment on a PR
2. Rocky classifies the response: acknowledged, disagreed, question, or unclear
3. Based on classification, Rocky may reply once (explain, concede, or answer)
4. If Rocky concedes, a **feedback signal** is saved for the repo

## Feedback Signals

When Rocky concedes that a flagged pattern is intentional, it stores this as a feedback signal. In future reviews, these signals are injected into the review prompt so Rocky avoids re-flagging the same patterns.

Signals are:
- **Per-repo** — one developer's feedback benefits all future reviews
- **Category-scoped** — e.g., "Silent Failures in retry code"
- **Optionally file-scoped** — can target specific directories
- **Time-limited** — signals expire after 6 months without use

## Escalation Cap

Rocky replies at most twice per thread (original finding + 2 replies = 3 total Rocky messages). After that, the thread is left for human review.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `THREAD_AGENT_ENABLED` | `false` | Enable the thread conversation agent |
| `THREAD_AGENT_MODEL` | `haiku` | Model for thread replies |
| `THREAD_AGENT_MAX_REPLIES` | `3` | Max Rocky messages per thread before escalation |
| `THREAD_AGENT_MAX_CONCURRENT` | `3` | Max parallel thread agent calls |
| `FEEDBACK_SIGNALS_ENABLED` | `true` | Write and read feedback signals (requires DATABASE_ENABLED) |
| `FEEDBACK_SIGNALS_TTL_DAYS` | `180` | Days before unmatched signals expire |

## Cost

Thread agent uses a cheap model (Haiku/Flash). Typical cost: ~$0.001 per thread reply.
