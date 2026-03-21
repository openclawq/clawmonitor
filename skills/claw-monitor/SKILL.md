---
name: claw-monitor
description: Use the `clawmonitor` CLI/TUI to inspect OpenClaw sessions, model health, token usage, and gateway service health.
homepage: https://github.com/openclawq/clawmonitor
metadata:
  {
    "openclaw":
      {
        "emoji": "🦞",
        "requires": { "bins": ["clawmonitor"] }
      },
  }
---

# ClawMonitor (OpenClaw monitoring)

Use this skill when the user asks questions like:

- “Did my agent finish? Why no feedback?”
- “Which session/thread received the last message, and when?”
- “Is the agent working, interrupted, or stuck? Any delivery failures?”
- “Which model is failing, and is the problem the provider or OpenClaw itself?”
- “Which session burned the most tokens recently?”
- “Do we have zombie/helper processes or a dirty gateway cgroup?”

## Preconditions

- This skill runs on a machine that has OpenClaw state at `~/.openclaw/`.
- `clawmonitor` is installed on that same machine.

Install:

```bash
pip install -U clawmonitor
```

## Core commands

### 1) Status (Markdown)

Show the core status table (good default for IM replies):

```bash
clawmonitor status --format md
```

For a more verbose table including task/message previews:

```bash
clawmonitor status --format md --detail
```

### 2) Model health

Probe configured models directly and/or through OpenClaw:

```bash
clawmonitor models --format md
```

Useful variants:

```bash
clawmonitor models --mode direct --format json
clawmonitor models --mode openclaw --timeout 20
```

### 3) Drill down on one session

Export a redacted report for a single session key:

```bash
clawmonitor report --session-key 'agent:main:main' --format md
```

### 4) Nudge (ask the session to report progress)

Send a progress request into the session (this is a trigger message; the agent may reply to IM depending on routing/delivery):

```bash
clawmonitor nudge --session-key 'agent:main:main' --template progress
```

### 5) Full TUI for interactive inspection

Run the TUI when you need interactive triage:

```bash
clawmonitor tui
```

Important TUI keys:

- `v`: cycle Sessions / Models / System
- `s`: jump directly to System
- `h`: toggle right-side Status / History
- `u`: cycle token windows (`now` / `1d` / `7d` / `30d`)
- `r`: refresh current active surface
- `z`: cycle pane widths
- `Z`: fullscreen detail
- `o`: open the English operator note in System view

## Reply guidelines

- Prefer `--format md` outputs for IM replies.
- If status shows `DELIVERY_FAILED` or `NO_FEEDBACK`, include the relevant sessionKey and recommend a `report` export next.
- If model checks disagree, explicitly separate `direct provider path` vs `OpenClaw path`.
- If token questions are time-windowed, mention whether the number is a current session snapshot or a `1d / 7d / 30d` Gateway usage range.
- If the issue is service-level, summarize `risk`, `reclaimable memory estimate`, and whether zombies/orphans were detected.
- Avoid pasting raw gateway logs unless the user asks; use `clawmonitor report` which redacts common secrets.
