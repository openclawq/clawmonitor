# Troubleshooting

## “Queue is idle but I’m not sure the task finished”

In ClawMonitor, look for:

- `NO_FEEDBACK` (user message newer than assistant message)
- `DELIVERY_FAILED` (failed delivery queue entry)
- `WORKING` with long run time (lock exists)

If Gateway logs are enabled, open **Diagnosis** to see evidence-driven findings and next steps.

## Common next-step commands

```bash
openclaw gateway call channels.status --json
openclaw gateway call status --json
openclaw logs --follow --json
```

