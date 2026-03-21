# Reference Repo Comparison

Checked on: 2026-03-20

Compared local reference repositories:

- `/home/qagent/program/clawmonitor/prgram/temp/claw-monitor`
- `/home/qagent/program/clawmonitor/prgram/temp/openclaw-sessionwatcher`
- `/home/qagent/program/clawmonitor/prgram/temp/openclaw-watchdog`

This note answers 3 questions:

1. What useful data do those projects expose that ClawMonitor does not yet expose?
2. What display patterns are stronger than the current ClawMonitor TUI?
3. Which ideas are worth borrowing now vs later vs not at all?

## Short answer

There are still useful ideas to borrow, but not many missing fundamentals.

The biggest remaining gaps are:

- a clearer top-level host summary panel
- optional coding-agent process visibility
- optional host resource gauges
- better “operator history” / event stream for actions and recoveries

The strongest reference for session-detail rendering is still `openclaw-sessionwatcher`.
The strongest reference for policy / incident-state handling is still `openclaw-watchdog`.
The strongest reference for “single-screen operations dashboard” is `claw-monitor`.

For ClawMonitor specifically, the best near-term borrowings are:

1. lightweight host resource bars in `System`
2. optional coding-agent process list
3. a compact operator-event log / action history panel

The ideas that are interesting but should probably stay out for now:

- full browser-style session transcript UI
- Docker / k8s dashboard
- automatic recovery actions directly from the TUI

## Repo 1: `claw-monitor`

Repo:

- `/home/qagent/program/clawmonitor/prgram/temp/claw-monitor`

### What it is good at

This project is strongest as a terminal operations dashboard.

It combines:

- sub-agent activity
- coding-agent process detection
- cron dashboards
- host CPU / memory / disk / GPU
- Docker / k8s / systemd summaries

The best idea is not any single metric.
It is the “one screen, many operator surfaces” layout.

### Useful data it has that ClawMonitor does not yet show

1. Host resource gauges

- CPU
- memory
- disk
- optional GPU

This is useful because when OpenClaw is slow, the first triage question is often:

- is the gateway broken?
- or is the machine itself saturated?

Current ClawMonitor `System` shows cgroup/service memory and process RSS, but not the machine-level capacity picture.

2. Coding-agent process detection

- detects Codex / Claude Code / GitHub Copilot CLI processes via `ps`
- shows pid and runtime
- provides attach commands

This is useful because users often mentally connect “OpenClaw is busy” with “which coding agent is currently alive”.

We currently show session history and status, but not an explicit process-level coding-agent layer.

3. Cron and system cron on the same screen

This project merges OpenClaw cron and system cron.
That is broader than ClawMonitor today.

### Display ideas worth borrowing

1. Gauge-style summary blocks

The bar chart presentation is good for:

- CPU
- RAM
- disk
- maybe reclaimable memory

This would work well in the top of `System`, especially when the right pane is wide.

2. Dashboard sections with clear separators

`claw-monitor` is visually easier to scan because each section is clearly chunked:

- sub-agents
- cron
- system stats

This supports the direction we already started in the new `System` detail pane.

3. Graceful narrow-terminal behavior

It explicitly thinks about:

- auto sizing
- internal scroll
- alternate screen

The layout discipline is worth learning from, even if we do not copy the visual style.

### What is less relevant for ClawMonitor right now

- Docker containers
- k8s pods
- system-wide systemd services

These are useful for a host dashboard, but they are a step away from the OpenClaw-centric problem we are solving.
If we add them, they should be optional and clearly secondary.

### Recommendation

Borrow now:

- host resource gauges in `System`
- optional coding-agent process list

Maybe later:

- generic host services / Docker / k8s

Do not copy directly:

- full “everything on one screen” layout

Why:

- ClawMonitor already has 3 top-level views
- too much unrelated host data will dilute the OpenClaw focus

## Repo 2: `openclaw-sessionwatcher`

Repo:

- `/home/qagent/program/clawmonitor/prgram/temp/openclaw-sessionwatcher`

### What it is good at

This project is strongest at deep session inspection.

Its main strengths are:

- very rich message rendering
- SSE live push
- ACP / cron special handling
- per-message structure awareness
- local-web security considerations

### Useful data it has that ClawMonitor does not yet show

1. Richer ACP details

It surfaces ACP-specific info such as:

- ACP status
- last activity
- cumulative token usage
- exit state checks from `.acpx`

ClawMonitor already supports ACP session reading, but not a dedicated ACP stats panel.

2. Better gateway/web runtime features

It has:

- SSE push updates
- gateway chat-send path
- cookie/token protection for non-loopback bind

This is not directly relevant to the TUI, but it is useful if ClawMonitor ever gets a local web companion.

3. Fine-grained message-structure rendering

It distinguishes:

- gateway-injected traffic
- inter-session payloads
- thinking blocks
- tool calls
- tool results
- cron metadata

ClawMonitor is intentionally more compact, but for drill-down screens this is still the best reference.

### Display ideas worth borrowing

1. Stronger status indicators for “currently processing”

It uses pulsing/typing indicators and ACP-specific activity badges.

For the TUI, the direct equivalent is:

- more visible live badges in detail panes
- stronger highlighting of currently active sessions / families

2. Special rendering for structured subtypes

It does not flatten everything into plain text.

This is useful for:

- ACP sessions
- cron sessions
- maybe future system issue rows

3. “Chat-only” simplification

The idea matters more than the exact UI:

- let the user hide complexity temporarily

In ClawMonitor terms, similar ideas could be:

- hide healthy processes in `System`
- show only problematic families
- show only tool/error events in history

### What is less relevant for ClawMonitor right now

- full browser chat UI
- raw JSON modal
- copy buttons everywhere
- light/dark theming complexity

These are strong for a web UI, but do not translate cleanly into a compact TUI.

### Recommendation

Borrow now:

- ACP summary panel ideas
- stronger active-processing cues
- maybe a “problematic only” filter in `System`

Maybe later:

- a small local web companion for deep inspection

Do not copy directly:

- full message-browser interaction model

## Repo 3: `openclaw-watchdog`

Repo:

- `/home/qagent/program/clawmonitor/prgram/temp/openclaw-watchdog`

### What it is good at

This project is strongest at policy and recovery state.

It is less about visualization, more about:

- evaluating interrupted sessions
- deciding whether action is allowed
- recording watchdog state
- avoiding repeated handling

### Useful data it has that ClawMonitor does not yet show

1. Persistent action state

It stores per-session watch state such as:

- auto resume count
- last handled abort time
- last resume time
- last action
- status

ClawMonitor currently has rich observation, but much less persistent action-state tracking.

2. Policy gating

It models:

- enabled/disabled
- max auto resume
- cooldown
- per-agent policy

This is useful even if ClawMonitor never becomes an auto-recovery tool.
Why:

- the monitor can still display whether a session is “actionable”, “cooling down”, or “escalated”

3. Event-log mentality

The watchdog is structured around:

- repeated checks
- decisions
- actions
- escalation

This maps well onto the user’s request for history / todo / doing / done style views.

### Display ideas worth borrowing

1. Operator action history

Not full auto-resume, but a readable history of:

- detected interruption
- nudge sent
- reload requested
- token load requested
- system refresh completed

This would make ClawMonitor feel more like an operations console.

2. Explicit policy labels

Examples:

- `eligible`
- `cooldown active`
- `max auto resume reached`

Even in read-only mode, these labels reduce ambiguity.

3. Per-agent policy summary

This could become a low-cost right-pane block later:

- default policy
- agent override
- whether auto action is enabled elsewhere

### What is less relevant for ClawMonitor right now

- direct recovery actions from the TUI
- automatic nudging / escalation logic inside this repo

Why:

- ClawMonitor has intentionally stayed observational first
- mixing heavy action policy into the main monitor increases risk fast

### Recommendation

Borrow now:

- event/action history concept
- policy/readiness labels

Maybe later:

- read-only watchdog-state integration

Do not copy directly:

- auto-recovery logic into the main TUI

## What ClawMonitor still does not have

After comparing the 3 repos, these are the most meaningful missing pieces.

### Tier 1: worth adding

1. Host-level resource summary

Suggested:

- top-right `HOST` block in `System`
- CPU / RAM / disk
- optional GPU if available

Why:

- explains whether the machine is saturated even when OpenClaw-specific processes are fine

2. Coding-agent process visibility

Suggested:

- optional `System` subsection or separate top-level table later
- detect `codex`, `claude`, `gh copilot`
- pid / age / CPU / RSS

Why:

- matches how users actually reason about long-running work

3. Operator event history

Suggested:

- compact event log pane or overlay
- not full transcript history
- monitor-origin events only

Examples:

- model probe started / finished
- token usage loaded
- session history loaded
- system snapshot refreshed
- nudge sent

Why:

- gives users confidence about what they already did

### Tier 2: useful but not urgent

1. ACP-specific detail mini-panel
2. “problematic only” filter for `System`
3. richer cron metadata in the main TUI

### Tier 3: probably out of scope unless product direction changes

1. Docker / k8s observability
2. full browser transcript UI
3. automatic recovery engine inside ClawMonitor

## Best display ideas to borrow

If we only borrow 3 presentation ideas, they should be:

1. `claw-monitor`: compact resource gauges
2. `openclaw-sessionwatcher`: subtype-specific detail rendering
3. `openclaw-watchdog`: operator event / policy state language

That combination fits ClawMonitor much better than copying any one project wholesale.

## Recommended next steps

1. Add a `HOST` mini-summary in `System`

- CPU
- RAM
- disk
- optional GPU

2. Add an internal monitor event log

- store monitor-origin actions in local JSONL
- show recent events in a pane or overlay

3. Add a lightweight coding-agent subsection

- `codex`
- `claude`
- `copilot`

4. Keep the TUI OpenClaw-centric

- do not turn it into a generic host dashboard too early

That keeps the scope under control while still learning the best lessons from the reference repos.
