# ClawHub import (claw-monitor skill)

This repository includes an OpenClaw skill folder:

- `skills/claw-monitor/SKILL.md`

ClawHub can import skills directly from **public GitHub repos** and auto-detects `SKILL.md` files inside folders.

## Import steps (web)

1) Open ClawHub and choose **Import from GitHub**.
2) Paste the repo URL: `https://github.com/openclawq/clawmonitor`
3) Select the skill folder: `skills/claw-monitor`
4) Confirm the imported `SKILL.md` preview.
5) Install / enable it in your OpenClaw workspace and start a new session (skills snapshot refresh happens on new sessions by default).

## What the skill does

The `claw-monitor` skill is a wrapper that tells the agent how to use the `clawmonitor` CLI to:

- render `clawmonitor status --format md` for IM-friendly updates
- export per-session diagnostics via `clawmonitor report`
- optionally send progress nudges via `clawmonitor nudge`

## Notes

- The skill itself is just text instructions; it does not bundle your OpenClaw state.
- Make sure the host has `clawmonitor` installed:

```bash
pip install clawmonitor
```

