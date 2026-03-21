# ClawMonitor v0.2.0

ClawMonitor is a keyboard-first OpenClaw monitor for sessions, models, token usage, and gateway service health.

## Highlights

- Session history: load cached `todo / doing / done` task history for the selected session on demand.
- Token visibility: inspect current token pressure and Gateway-backed `1d / 7d / 30d` usage windows inside the TUI.
- System view: inspect `openclaw-gateway.service`, cgroup processes, zombie/orphan helpers, reclaimable RSS estimates, and service risk in a dedicated top-level view.
- Operator note: open an English runbook-style note from the System view with cleanup/restart guidance based on the current snapshot.
- TUI flow: clearer `WAITING / RUNNING / READY / ERROR` banners, pane-width cycling, fullscreen detail, reset/help/paging improvements, and stronger small-terminal behavior.
- Model monitoring: retain direct provider probes and OpenClaw-path probes, with clearer running state and parallel probing workers.

## Install / upgrade

```bash
pip install -U clawmonitor
```

## Docs

- WeChat article draft: `docs/wechat-update-0.2.0.md`
- Tweet draft: `docs/twitter-140-0.2.0.txt`
