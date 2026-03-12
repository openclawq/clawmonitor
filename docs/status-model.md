# Status model

Per session, ClawMonitor tracks:

- **last_user**: newest transcript message with `role=user` (may include internal wrapper/control-plane injections)
- **last_user_send**: best-effort “real inbound” user message extracted from common wrapper formats (preview + timestamp)
- **last_trigger**: newest internal/control-plane trigger message (if present)
- **last_assistant**: last transcript message with `role=assistant` (preview + timestamp, stopReason)
- **lock**: `sessionFile + ".lock"` (pid + createdAt) → indicates active run and run duration
- **abortedLastRun**: from `sessions.json`
- **delivery failure**: from `delivery-queue/failed` entries keyed by `mirror.sessionKey`
- **channel IO** (optional): `channels.status` lastInboundAt/lastOutboundAt for the channel account (Gateway online)

## Primary states

- `WORKING`: lock file exists
- `FINISHED`: no lock; last_assistant timestamp is >= last_user timestamp (when last_user exists)
- `INTERRUPTED`: no lock; `abortedLastRun=true` and last_user is newer than last_assistant
- `NO_MESSAGE`: no user message exists in transcript

## Alerts (orthogonal)

- `NO_FEEDBACK`: no lock but last_user is newer than last_assistant (the “queue empty but no reply” problem)
- `LONG_RUN`: lock exists and duration exceeds thresholds (default warn 15m, critical 60m)
- `DELIVERY_FAILED`: there is a failed delivery record for the session key
- `SAFETY`: last assistant stopReason hints safety/refusal/content_filter (heuristic)
- `SAFEGUARD_OFF`: agent compaction mode is not `safeguard` (best-effort snapshot from `openclaw.json`)
- `TRXM`: transcript missing (sessionFile exists but referenced `*.jsonl` is missing)
- `BOUND_OTHER` / `BIND`: Telegram chat is routed to another session key via thread bindings
