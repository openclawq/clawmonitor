# Publishing / Sharing

ClawMonitor is designed to be shareable:

- The repository contains **code + docs only**.
- Runtime data is stored under XDG state/cache:
  - `~/.local/state/clawmonitor/` (events + reports)
  - `~/.cache/clawmonitor/`
- Configuration is read from `~/.config/clawmonitor/config.toml` by default.

## Before publishing

1) Ensure you did not copy any of these into the repo:

- `~/.openclaw/openclaw.json`
- `~/.openclaw/agents/**/sessions/*.jsonl`
- `~/.openclaw/delivery-queue/**`

2) Keep diagnostics reports redacted (ClawMonitor does this by default).

3) If you paste logs into issues, still treat them as sensitive and remove:

- tokens / bot tokens
- phone numbers / user ids (when required)
- internal hostnames / paths (if relevant)

## Report sharing

- Prefer exporting a redacted report: `clawmonitor report --session-key ... --format md` (or `both`).
- Reports are written to `~/.local/state/clawmonitor/reports/` (XDG state dir), not into the repo.
