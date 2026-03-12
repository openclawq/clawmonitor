# Troubleshooting

## “Queue is idle but I’m not sure the task finished”

In ClawMonitor, look for:

- `NO_FEEDBACK` (user message newer than assistant message)
- `DELIVERY_FAILED` (failed delivery queue entry)
- `WORKING` with long run time (lock exists)
- `ALERT` row class in TUI (red), which summarizes abnormal states

If Gateway logs are enabled, open **Diagnosis** to see evidence-driven findings and next steps.

## Telegram: “I see replies in Telegram, but ClawMonitor shows nothing”

First, check whether OpenClaw is writing the session transcript you’re looking at:

- `TRXM` means the `sessionFile` exists in `sessions.json` but the referenced `*.jsonl` transcript is missing.

Second, check Telegram routing:

- `BOUND_OTHER` / `BIND` means this Telegram chat (`telegram:<chatId>`) is bound to a *different* session key via thread bindings.
  In that case, your “main” session will not receive new inbound for that chat.

Where to look:

- Thread bindings file: `~/.openclaw/telegram/thread-bindings-default.json`
- OpenClaw config toggle: `~/.openclaw/openclaw.json` → `channels.telegram.threadBindings.spawnAcpSessions`

Fix pattern (safe, reversible):

1) Back up and clear the binding for the chat id from `thread-bindings-default.json`
2) Optionally disable ACP spawning: set `spawnAcpSessions=false`
3) Restart Gateway: `openclaw gateway restart`

## Useful ClawMonitor commands

```bash
clawmonitor status
clawmonitor status --format json
clawmonitor status --format md
clawmonitor report --session-key 'agent:main:main' --format both
```

## Common next-step commands

```bash
openclaw gateway call channels.status --json
openclaw gateway call status --json
openclaw logs --follow --json
```
