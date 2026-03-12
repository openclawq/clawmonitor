# Troubleshooting

## “Queue is idle but I’m not sure the task finished”

In ClawMonitor, look for:

- `NO_FEEDBACK` (user message newer than assistant message)
- `DELIVERY_FAILED` (failed delivery queue entry)
- `WORKING` with long run time (lock exists)
- `ALERT` row class in TUI (red), which summarizes abnormal states

If Gateway logs are enabled, open **Diagnosis** to see evidence-driven findings and next steps.

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
