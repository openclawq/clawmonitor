# Architecture

ClawMonitor reads **local OpenClaw state** for real-time session observability, and optionally enriches it with **Gateway runtime** (channels snapshot) + **Gateway logs** via RPC.

## Data sources

Offline (no Gateway required):

- Session index: `~/.openclaw/agents/*/sessions/sessions.json`
- Transcripts: `*.jsonl` referenced by session entries
- In-flight locks: `*.jsonl.lock` (pid + createdAt)
- Delivery failures: `~/.openclaw/delivery-queue/failed/**/*.json`

Online (Gateway reachable):

- Gateway logs tail: `openclaw gateway call logs.tail --json` (incremental cursor)
- Channel runtime snapshot: `openclaw gateway call channels.status --json`

Telegram routing (local state):

- Thread bindings (conversation → sessionKey): `~/.openclaw/telegram/thread-bindings-<accountId>.json`

ACP sessions (local state, when enabled):

- ACPX sessions (ACP backend runtime): `~/.acpx/sessions/<acpxSessionId>.json`
  - Used to enrich ACP sessions that may not have a JSONL transcript file.

## Outputs

- TUI: `clawmonitor tui` (tree view grouped by agent, color-coded rows, footer hotkeys, manual refresh and interval cycling)
- CLI status: `clawmonitor status --format text|json|md`
- Export single-session report: `clawmonitor report --session-key ... --format json|md|both`
  - Written under `~/.local/state/clawmonitor/reports/` by default (XDG state dir)

## Security posture

- Never dumps `openclaw.json` to stdout or logs.
- Redacts token-like substrings in Gateway log lines and exported reports.
- Writes runtime logs and reports under XDG state dirs (`~/.local/state/clawmonitor/`), not inside the repo.
