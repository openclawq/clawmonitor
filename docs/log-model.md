# Gateway log correlation

When Gateway is reachable, ClawMonitor tails logs via:

```bash
openclaw gateway call logs.tail --json
```

The returned `lines[]` are typically JSON-encoded log lines (often with numeric string keys like `"0"`, `"1"`). ClawMonitor extracts:

- timestamp (`time` / `_meta.date`)
- subsystem (`gateway/channels/feishu`, `gateway/channels/telegram`, …)
- message text (joined string fields)

These are used to:

- show **Related Logs** per session
- run heuristics (Findings) for “no reply / stuck” root causes

