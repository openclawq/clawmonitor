# ClawMonitor

Real-time **OpenClaw** session monitor with:

- Per-session last inbound **user** message + last outbound **assistant** message (preview + timestamp)
- Work state: WORKING / FINISHED / INTERRUPTED / NO_MESSAGE (+ NO_FEEDBACK alert)
- Long-run visibility via `*.jsonl.lock` (works even if Gateway is down)
- Optional Gateway log tail + channel runtime snapshot correlation (Feishu/Telegram-focused rules)
- Full-screen TUI with manual “nudge” (send a progress request via `chat.send`)

## Install (editable)

```bash
cd ~/program/clawmonitor
python3 -m pip install -e .
```

## Run

```bash
clawmonitor tui
```

Other commands:

```bash
clawmonitor snapshot --format json
clawmonitor snapshot --format md
clawmonitor nudge --session-key 'agent:main:main' --template progress
clawmonitor status
clawmonitor status --format json
clawmonitor status --format md
clawmonitor report --session-key 'agent:main:main' --format both
clawmonitor watch --interval 1
```

## Configuration

Default config path:

- `~/.config/clawmonitor/config.toml`

Example config is in `config.example.toml`.

Runtime data (NOT stored in this repo):

- Logs: `~/.local/state/clawmonitor/events.jsonl`
- Reports: `~/.local/state/clawmonitor/reports/`
- Cache: `~/.cache/clawmonitor/`

## Keys (TUI)

- `↑/↓`: move selection
- `Enter`: nudge selected session (choose template)
- `l`: toggle related logs panel
- `d`: re-run diagnosis for selected session
- `e`: export a redacted report for selected session
- `r`: force refresh
- `f`: cycle refresh interval
- `q`: quit

Rows are color-coded when your terminal supports colors (`OK` green, `RUN` cyan, `IDLE` yellow, `ALERT` red).

## Notes

- ClawMonitor never prints or writes OpenClaw secrets. It avoids dumping `openclaw.json` and redacts suspicious token-like strings in logs/reports.
- If Gateway is unreachable, ClawMonitor still works in offline mode (sessions/transcripts/locks/delivery-queue) but disables log tail + nudge.
- If your terminal window is narrow, `clawmonitor tui` may hide the details panel; use `clawmonitor status` as a stable fallback.
