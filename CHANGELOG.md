# Changelog

## 0.2.1

- Model monitor: resolve provider auth/header secrets from `${VAR}` syntax and fall back to `~/.openclaw/.env` when process env vars are missing.
- Model monitor: prefer model-level `models[].api` declarations over provider/base URL heuristics so mixed transports probe with the correct API kind.
- Tests: add regression coverage for dotenv-backed auth resolution and model-level OpenAI Responses targets.

## 0.2.0

- Product scope: ClawMonitor is now a broader OpenClaw operations monitor, covering Session, Model, Token, and System views in one keyboard-first TUI.
- Session history: add on-demand cached history loading with `todo / doing / done` style task trajectory for the selected session.
- Token visibility: add current token snapshot in session detail plus Gateway-backed `1d / 7d / 30d` usage windows for selected sessions.
- System view: add a dedicated `System` surface for `openclaw-gateway.service` health, cgroup process inspection, zombie/orphan detection, reclaimable RSS estimates, and operator-facing risk summaries.
- Operator note: add an English runbook-style operator note overlay for the current system snapshot, with paging support and cleanup/restart guidance.
- TUI ergonomics: add clearer per-view `WAITING / RUNNING / READY / ERROR` states, pane-width cycling, fullscreen detail, better `Esc` reset behavior, per-view help, paging, agent jump shortcuts, and stronger status coloring.
- Models: keep model probing manual/on-demand, show more visible running state, and support parallel probing workers for larger model chains.
- Events/docs/tests: add monitor event log helpers, system-monitor and event-log tests, system/token research docs, release/demo docs, and automated asciinema demo tooling.

## 0.1.9

- Model monitor: add `clawmonitor models` to probe configured models directly and/or through OpenClaw.
- TUI: add separate `Models` view (`v` toggle) with manual refresh for per-model health checks.
- TUI: model view now shows a prominent `WAITING` / `RUNNING` / `DONE` / `ERROR` banner during probe runs.
- TUI: add `PgUp` / `PgDn` and `g` / `G` for page navigation / jump-to-edge.
- Direct probes: auto-detect supported transports (`openai-completions`, `openai-responses`, `anthropic-messages`), resolve agent auth profiles, measure latency, and classify failures (`timeout`, `network`, `auth`, `billing`, `rate_limit`, `overloaded`, `unsupported`, `error`).
- OpenClaw probes: run temporary probe sessions via `sessions.patch` + `agent` + `agent.wait`, then clean them up.
- Tests/docs: add model monitor tests and a dedicated `docs/model-monitor.md`.

## 0.1.8

- Silent-gap metrics: show `outAgeHuman` (channel lastOutboundAt) vs `thinkAge` (internal transcript activity) to spot “working silently”.
- TUI: `d` now runs on-demand diagnosis for the selected session (includes silent-gap hints) instead of forcing a full refresh.
- Docs: explain the two ages and add Git/PyPI/ClawHub install section in the 0.1.6 WeChat post.

## 0.1.7

- TUI: fix occasional “stale characters” artifacts on dynamic lines by padding redraws (prevents leftover fragments on screen).

## 0.1.6

- TUI: Focus filter (`x`) to hide stale/noise sessions; footer shows `sessions=shown/total`.
- TUI: rename/label editor (`R`) writes `[labels]` back to your config for opaque ids (e.g. Feishu `ou_...`, Telegram chat ids).
- Cron: `clawmonitor cron` + cron jobs in tree view (toggle `c`).
- Display: show agent identity name from `IDENTITY.md` as `name(agentId)`; compact list columns; clearer related logs header.
- UX: default NODE label mode off (`n` toggle); expanded `?` help for states/flags.

## 0.1.5

- TUI: startup loading splash (customizable via `docs/loadingart.txt` or `$CLAWMONITOR_LOADING_ART`).
- TUI: async refresh + cached related logs to reduce UI stalls when Gateway is slow.
- ACP: enrich ACP sessions via `~/.acpx/sessions/<acpxSessionId>.json` when available; better WORKING detection for ACP runs.
- Transcript tail: improved wrapper extraction and preview cleanup (gateway time prefixes, `[[...]]` markers).

## 0.1.4

- TUI: tree view in the left list (group sessions by agent; highlight ACP/subagent via indentation).
- TUI: highlight `Task:` / `Thinking:` / `Trigger:` lines in the right status panel (magenta).
- TUI: better narrow-terminal behavior by showing sessionKey “tail” in the list (reduces truncation pain).

## 0.1.3

- PyPI: trusted publishing workflow uses GitHub Environment `pypi` and supports manual `workflow_dispatch`.

## 0.1.2

- Packaging: use SPDX `license = "MIT"` metadata (avoid setuptools deprecation warnings).
- Docs: clarify pip-only installation wording.

## 0.1.1

- Telegram: detect “thread binding” routing (chat → different sessionKey), flag it (`BOUND_OTHER` / `BIND`), and show the binding in TUI details.
- Telegram: improve `Last User Send` extraction from wrapper-style transcript messages.
- TUI: prevent right-pane long lines from visually spilling into the left list pane.
- Status output: include `UPD` (session updated age) and show `TRXM` when transcripts are missing.
- TUI details: show channel last inbound/outbound timestamps (Gateway online).

## 0.1.0

- Initial alpha release: TUI + status/snapshot/report/nudge commands.
