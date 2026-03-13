---
name: claw-monitor
description: Use the `clawmonitor` CLI to monitor OpenClaw sessions (last user/assistant messages, run state via locks, delivery failures, Telegram thread-binding routing).
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

## Preconditions

- This skill runs on a machine that has OpenClaw state at `~/.openclaw/`.
- `clawmonitor` is installed on that same machine.

Install:

```bash
pip install clawmonitor
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

### 2) Drill down on one session

Export a redacted report for a single session key:

```bash
clawmonitor report --session-key 'agent:main:main' --format md
```

### 3) Nudge (ask the session to report progress)

Send a progress request into the session (this is a trigger message; the agent may reply to IM depending on routing/delivery):

```bash
clawmonitor nudge --session-key 'agent:main:main' --template progress
```

## Reply guidelines

- Prefer `--format md` outputs for IM replies.
- If status shows `DELIVERY_FAILED` or `NO_FEEDBACK`, include the relevant sessionKey and recommend a `report` export next.
- Avoid pasting raw gateway logs unless the user asks; use `clawmonitor report` which redacts common secrets.

