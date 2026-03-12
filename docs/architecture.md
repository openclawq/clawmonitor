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

## Security posture

- Never dumps `openclaw.json` to stdout or logs.
- Redacts token-like substrings in Gateway log lines and exported reports.
- Writes runtime logs and reports under XDG state dirs (`~/.local/state/clawmonitor/`), not inside the repo.

