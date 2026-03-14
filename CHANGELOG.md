# Changelog

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
