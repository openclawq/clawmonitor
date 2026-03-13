# Changelog

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
