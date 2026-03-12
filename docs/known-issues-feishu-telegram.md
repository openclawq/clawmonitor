# Feishu & Telegram: common “no response” classes

This doc encodes recurring patterns seen in OpenClaw deployments:

## Feishu

- Long tasks interrupted by channel health restarts (stale-socket / restart mid-run)
- Run ends without queued final reply (`queuedFinal=false, replies=0`) due to upstream rate limits/errors
- SIGTERM/restart interruptions (final not delivered)
- Policy gates: DM pairing / allowlist and group mention gating

## Telegram

- Polling stall: no `getUpdates` for a long time (often proxy/NO_PROXY/egress or multi-instance competition)
- Policy gates: dmPolicy/groupPolicy allowlists, mention gating in groups
- `BOT_COMMANDS_TOO_MUCH` is usually not the root cause (command sync limits)
- Thread bindings can route a DM to an ACP session key. If this surprises you, disable ACP spawning:
  - `~/.openclaw/openclaw.json` → `channels.telegram.threadBindings.spawnAcpSessions=false`
  - Clear `~/.openclaw/telegram/thread-bindings-default.json` for the affected chat id (backup first)
