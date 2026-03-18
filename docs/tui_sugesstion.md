# Suggestions: Integrate ClawMonitor concepts into OpenClaw TUI / WebUI (OpenClaw v2026.3.13)

Scope: **design-only** suggestions (no code). This note is based on reviewing OpenClaw tag `v2026.3.13` in `~/projects/openclaw`.

## What OpenClaw already has (relevant building blocks)

### Built-in TUI (pi-tui)

OpenClaw already ships a first-class TUI built on `@mariozechner/pi-tui`:

- `src/tui/tui.ts` (main TUI wiring)
- `src/tui/tui-command-handlers.ts` (slash commands / overlays)
- `src/tui/tui-session-actions.ts` (session switching, refresh)
- `src/tui/components/*` (chat log, tool execution view, selectors)

Notable: the TUI currently focuses on **one active session at a time**, with a selector (`sessions.list`) mainly for navigation.

### Gateway session APIs

The gateway exposes:

- `sessions.list` with `includeDerivedTitles` + `includeLastMessage` (already used by TUI + WebUI)
- `sessions.preview` (multi-key preview items from transcripts)
- `channels.status` (channel account snapshots, including `lastInboundAt/lastOutboundAt`)
- `logs.tail` (for correlation)

These are the right primitives to build a monitor view without requiring direct FS reads from clients.

### Existing “stuck / safety / loop” signals inside OpenClaw

OpenClaw already has internal mechanisms that are highly relevant to a monitor:

- Session lock hygiene / inspection:
  - `src/agents/session-write-lock.ts`
  - `src/commands/doctor-session-locks.ts`
- Delivery queue:
  - `src/infra/outbound/delivery-queue.ts`
- Tool loop detection:
  - `src/agents/tool-loop-detection.ts`

Recommendation: if we integrate monitoring into OpenClaw UX, we should **surface these first-party signals** rather than re-inventing heuristics.

## ClawMonitor’s “core value” to preserve

ClawMonitor’s monitor framing works because it answers two questions quickly:

1) Last real inbound user message + timestamp (per session).
2) Current work state + why (working/finished/interrupted/no-message), including “silent” failure modes:
   - delivery failures
   - routed elsewhere (thread binding / ACP)
   - safeguards off / safety blocks
   - “silent gap”: internal activity vs human-visible outbound

Additionally, the newest lightweight improvements worth carrying into OpenClaw UX:

- `age_output` (human-visible): “time since channel lastOutboundAt”
- `age_think` (internal): “time since last internal activity (assistant/tool/non-message)”
- “LastTool” (tool result ok/err + name) and “LastToolCall” (names)

## Integration into OpenClaw TUI: recommended approach

### UX goal

Keep the current “single-session chat TUI” intact, but add a **monitor overlay/panel** that:

- lists *many sessions* with health badges and a few core columns
- lets you jump into a session
- supports manual refresh and Focus mode (hide noise)
- remains responsive (avoid blocking the UI loop)

### Proposed entrypoint

Add a slash command (or keybinding) in TUI:

- `/monitor` → opens a “Session Monitor” overlay component
- optional: `/monitor focus` or a toggle inside the overlay

Why overlay? It matches OpenClaw’s existing TUI patterns (`createSearchableSelectList`, `createFilterableSelectList`, overlays in `tui-command-handlers.ts`).

### Data strategy (keep it light)

**Do not** re-read full transcripts for every session every second.

Instead, use a 2-tier strategy:

1) **Fast snapshot** for all rows:
   - `sessions.list` (keys, updatedAt, derivedTitle, lastTo/account/channel hints)
   - `channels.status` (per channel account `lastOutboundAt/lastInboundAt`, running/busy signals)
   - optionally: a cheap “lock summary” endpoint (see next section)

2) **On-demand drilldown for the selected row**:
   - `sessions.preview` for the selected session (and/or last N items)
   - `logs.tail` filtered by session key + channel/account context

This mirrors ClawMonitor’s philosophy: “show the dashboard instantly, then make the details richer as needed.”

### Most important missing piece: per-session run state + delivery failures

ClawMonitor uses on-disk signals (`*.jsonl.lock`, delivery-queue failed entries). In OpenClaw, the same signals already exist in core modules.

Best long-term design:

- Add a **new gateway method** (name suggestion): `sessions.monitor` / `sessions.health`
  - Returns per-session computed fields:
    - `state`: working/finished/interrupted/no_message/no_feedback
    - `reason` (human-readable)
    - `lock`: pid/createdAt/isStale/isZombie (from `session-write-lock.ts`)
    - `deliveryFailed`: boolean + lastError summary (from delivery queue)
    - `lastUserAt/lastAssistantAt/lastToolAt/lastToolName/lastToolIsError` (tail-scan transcripts; similar to what ClawMonitor does)
    - `telegramBindingRoutedElsewhere` (if OpenClaw can expose it safely)
    - `safety/safeguard` signals (if available in session metadata or system events)
    - `loopDetected` (if `tool-loop-detection.ts` has a surfaced flag/event)
  - Supports options:
    - `limit`, `activeMinutes`, `includeDerivedTitles`, `includeLastMessage` (like `sessions.list`)
    - `detail=false|true` (avoid heavy fields unless requested)

Why a gateway method?

- Works for local + remote gateway mode
- Lets both **TUI and WebUI** share the same monitor logic
- Keeps monitor “truth” in one place (OpenClaw core), while ClawMonitor can stay as a community tool / alternate frontend

If adding a gateway method is too heavy right now:

- TUI overlay can approximate with:
  - `sessions.list` + `sessions.preview` for a small subset (e.g. “recent 20”)
  - `channels.status` for `age_output`
  - but you will miss robust lock/delivery/loop signals

### Suggested columns for a TUI monitor overlay

Keep it scannable:

- Health: `OK/RUN/IDLE/ALERT`
- State: WORKING/FINISHED/…
- `U-AGE` / `A-AGE` (from transcript tail)
- `OUT` (age_output; channel-level lastOutboundAt)
- `THINK` (age_think; internal activity)
- `TOOL` (last tool name + ok/err)
- Flags (delivery failed, routed elsewhere, safeguard off, loop detected, stale lock)
- Session label/title (derivedTitle + key tail)

### Interactions

- Enter: jump into selected session (switch current session key)
- `r`: refresh monitor snapshot
- `x`: Focus mode toggle
- `?`: help
- `d`: run deeper diagnosis for selected session (fetch logs.tail + preview)

## Integration into OpenClaw WebUI: recommended approach

OpenClaw WebUI already has:

- Sessions page (`ui/src/ui/views/sessions.ts`) driven by `sessions.list`
- Channels controller using `channels.status` (`ui/src/ui/controllers/channels.ts`)

### Where to put a monitor panel (pure UX discussion)

WebUI can host a richer monitor panel than terminal UIs (wide tables, sorting, multi-column filters, expandable evidence).
There are three reasonable homes:

Option A — **inside Sessions** (lowest friction):

- Add a “Monitor mode” toggle to the Sessions view.
- In monitor mode, show extra columns/badges (OUT/THINK/TOOL/FLAGS) and a details drawer per row.
- Keep default Sessions view as navigation-focused, so the UI doesn’t feel heavier for casual users.

Option B — **a dedicated Monitor tab** (clean separation):

- Add a top-level “Monitor” tab next to Sessions/Usage/Cron.
- Use the monitor table as the primary screen, with a right-side details panel.

Option C — **inside Agents** (agent-centric health overview):

- Add an “Agent health” panel that aggregates per-agent: WORKING/ALERT counts, last OUT/THINK, delivery failures.
- Clicking an agent drills into its sessions filtered view.

Recommended path (keep it lightweight but effective):

- Start with **Option A (Sessions + toggle)** and optionally add **Option C (Agent summary)** later.
- Option B is the best long-term UX once the monitor endpoint exists, but it’s more surface area.

### Minimal “monitor” in WebUI (low risk)

Option A (fastest):

- Add columns/badges to the existing Sessions view:
  - `outAgeHuman` (from `channels.status.lastOutboundAt`)
  - `thinkAge` (needs tail fields; see below)
  - `lastTool` (needs tail fields; see below)
  - delivery/lock/loop badges (best from a new `sessions.monitor` endpoint)

Option B (cleaner UX):

- Add a dedicated “Monitor” tab next to Sessions/Usage/Cron.
  - Table view (sort/filter/focus)
  - Row details panel (diagnosis, related logs, delivery failures)
  - Jump-to-session action

### Data source recommendation for WebUI

Like TUI, WebUI should not parse full transcripts client-side.

Preferred:

- `sessions.monitor` gateway endpoint provides the extra fields

Fallback (works now, but limited):

- Use `sessions.preview` to fetch small tails for selected sessions only (e.g. on row expand)

### Extra value if surfaced in WebUI

WebUI can visualize things TUI can’t comfortably:

- Sparkline timeline of `OUT` vs `THINK` ages (silent gap)
- “Tool loop suspicion” history if OpenClaw exposes loop-detection events
- Delivery queue detail viewer for failed items (redacted)

## How to “combine” ClawMonitor with OpenClaw without bloating OpenClaw core

If OpenClaw maintainers want to avoid adding new core endpoints:

- Treat ClawMonitor as the **reference implementation** and keep it external.
- Provide one small “interop hook” in OpenClaw:
  - a stable `sessions.preview` schema (already present)
  - optional: add minimal `sessions.locks` / `sessions.delivery` endpoints (small surface area)

Then:

- OpenClaw TUI/WebUI can show a “Launch ClawMonitor” link/button (docs only)
- ClawMonitor continues to be pip-installable and evolves independently

## Recommended next steps (non-coding)

1) Align on the canonical monitor API shape:
   - decide whether `sessions.monitor` is acceptable as a gateway method
2) Decide where “truth” should live:
   - OpenClaw core computes lock/delivery/loop → shared by TUI/WebUI/ClawMonitor
   - or clients compute heuristics (less reliable)
3) Define a minimal field set that is:
   - useful for debugging silent failures
   - safe to expose (redaction)
   - cheap to compute
