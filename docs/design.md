# Design Notes (TUI + Refresh Model)

This document explains the “why” and “how” behind ClawMonitor’s responsiveness and correctness.

## Goals

- **Answer 2 questions fast**:
  1) What is the last inbound user message + timestamp?
  2) What is the current work state (working/finished/interrupted/no message), and why?
- **Long-task friendly**: a task can run for 1 hour; the monitor should show “still running” without waiting for a 1-hour heartbeat.
- **Expose “silent gaps”**: distinguish “agent is active” vs “human-visible output is being delivered”.
- **Gateway optional**: offline mode still works (sessions/transcripts/locks/delivery-queue).
- **Actionability**: every monitor conclusion should be explainable by local files or Gateway logs.
- **No secrets in repo**: config is local; runtime reports/logs go to XDG state dirs.

## Core data path

At each refresh, ClawMonitor builds a `SessionView` list from:

- `sessions.json` index per agent (`~/.openclaw/agents/*/sessions/sessions.json`)
- transcript tail for each session JSONL (bounded by `limits.transcript_tail_bytes`)
- lock file state (`*.jsonl.lock`)
- delivery failures (`~/.openclaw/delivery-queue/failed/**`)
- optional enrichments:
  - `channels.status` + `logs.tail` (Gateway)
  - Telegram thread bindings (local)
  - ACPX state (`~/.acpx/sessions/<id>.json`) for ACP sessions without JSONL
  - cron jobs/runs (`~/.openclaw/cron/*`) for cron sessions

The computed status is intentionally conservative:

- A “real user message” is only `last_user_send` (wrapper-stripped), not any `role=user` blob.
- If `last_user_send` is newer than `last_assistant` and there is no lock, we flag **NO_FEEDBACK**.

## Async refresh (avoid UI stalls)

Gateway calls (`logs.tail`, `channels.status`) and JSONL tailing can block for seconds.
To avoid a “frozen” TUI:

- Refresh runs in a background thread.
- The UI loop always keeps handling key input and rendering the latest snapshot.
- Footer shows refresh progress and errors.

Caching and cheap change detection:

- Transcript tail is cached by `(mtime, size)` where possible.
- For per-session tailing, `sessions.json updatedAt` acts as a cheap “changed” detector.
- When a session is working, a short TTL forces re-tailing so `Last ASST` can update.
- Gateway log tail uses a cursor and a ring buffer; related-log filtering is cached per selected session.

## Focus filter (reduce noise)

When users have dozens of sessions, the default list can hide the important ones.
Focus mode is a single toggle:

- Keeps sessions that are working, interrupted, pending reply, delivery-failed, safety/safeguard issues, stale locks, transcript missing, telegram-routed-elsewhere.
- Keeps explicitly labeled sessions.
- Keeps recently active sessions (configurable default window in code).

This intentionally avoids a “lots of tiny filter toggles” UX.

## Labels (human-friendly ids)

Channels often expose opaque ids:

- Feishu: `ou_...` (direct user), `oc_...` (group), etc.
- Telegram: numeric chat ids.

ClawMonitor supports a local label map (`[labels]` in `~/.config/clawmonitor/config.toml`).

TUI can edit labels in-place (key `R`) and writes only the `[labels]` section back to the config file, keeping user config readable and shareable.

## Agent naming

OpenClaw agents can have a user-facing identity name (WebUI shows it).
ClawMonitor prefers:

1) identity name from `IDENTITY.md`
2) configured agent name from `openclaw.json`

and displays it as `name(agentId)` when it differs from the id.

## “Silent gap” metrics (lightweight)

Some of the nastiest failures happen in the gap between “agent is doing something” and “a human sees output”.
ClawMonitor keeps this lightweight by tracking two *ages*:

- `age_output` (human-visible): time since the IM channel last successfully sent an outbound message.
  - Source: Gateway `channels.status` → `lastOutboundAt` (per channel account).
  - Why: this is the cleanest signal for “a real message was delivered to a human”, but it is channel-level.
- `age_think` (internal activity): time since the session last produced internal activity (even if nothing was delivered).
  - Source: transcript tail timestamps (assistant message / toolResult / non-message entries).
  - Why: if `age_think` is fresh but `age_output` is stale, the agent may be “working silently” (stuck behind delivery, policy gates, routing, or a loop).

These are intentionally **heuristics**:

- They do not require token accounting or deep parsing.
- They are most useful when shown side-by-side (and in `d` diagnosis), not as a single “truthy” status.
