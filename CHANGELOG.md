# Changelog

## 0.1.1

- Telegram: detect “thread binding” routing (chat → different sessionKey), flag it (`BOUND_OTHER` / `BIND`), and show the binding in TUI details.
- Telegram: improve `Last User Send` extraction from wrapper-style transcript messages.
- TUI: prevent right-pane long lines from visually spilling into the left list pane.
- Status output: include `UPD` (session updated age) and show `TRXM` when transcripts are missing.
- TUI details: show channel last inbound/outbound timestamps (Gateway online).

## 0.1.0

- Initial alpha release: TUI + status/snapshot/report/nudge commands.

