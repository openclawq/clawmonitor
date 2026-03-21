# System Monitor Design

Checked on: 2026-03-20

This note defines the first `System` view for ClawMonitor.

The goal is not to replace session monitoring.
It is to answer a different operator question:

- Is the `openclaw-gateway.service` itself healthy?
- Is the service cgroup accumulating residual helper processes?
- Are there true zombies?
- If the operator decides to restart or clean the service later, how much memory is likely to be recovered?

Phase 1 is intentionally read-only.
ClawMonitor should observe and summarize risk, not kill anything.

## Why this view is needed

The current Session and Model views answer:

- is a specific session blocked?
- is a specific model/provider path healthy?

They do not answer:

- whether the gateway service cgroup is dirty
- whether Playwright / Chrome / ssh-agent / qmd helpers are piling up
- whether `KillMode=process` or similar service behavior leaves residual processes behind
- whether the system has many zombies that indicate poor cleanup

In the recent production runbooks, the core issue was not only model timeout/failover.
It was also service-level process accumulation around the systemd cgroup.

That is why `System` should be a separate top-level view, not a sub-pane inside session status.

## Core principles

1. Cgroup-first, not process-name-first

- The service cgroup is the most important boundary.
- Process names alone can be misleading.
- A `chrome` process inside `openclaw-gateway.service` means something very different from a random browser outside it.

2. Separate true zombies from residual live helpers

- `Zombie`
  - `STAT` contains `Z`
  - usually consumes no RSS
  - still matters as a cleanup signal
- `Residual helper`
  - process is still alive
  - often consumes real RSS / CPU
  - may be inside the service cgroup or orphaned after a restart

3. Risk must be explicit

- use clear levels such as `OK`, `WARN`, `ALERT`
- use stronger colors for:
  - service down / bad kill mode
  - orphan residual helpers in cgroup
  - active zombies
  - large reclaimable memory estimates
- keep the semantic palette stable:
  - green = healthy / ready
  - cyan = loading / running / active
  - yellow = warn / waiting / stale
  - red = alert / error
  - magenta = monitor actions / task trajectory
  - blue = section labels / structure

4. Keep it observational

- no `kill`
- no `restart`
- no automatic remediation
- only summaries, evidence, and estimates

5. Show loading state clearly

- do not hide `LOADING` only in the footer
- banner must say `WAITING / LOADING / READY / ERROR`
- show elapsed time and sample age

## Data sources

The first version can be built entirely from local process/service inspection.

### Service summary

Use:

```bash
systemctl --user show openclaw-gateway.service \
  -p Id \
  -p MainPID \
  -p ActiveState \
  -p SubState \
  -p TasksCurrent \
  -p MemoryCurrent \
  -p CPUUsageNSec \
  -p KillMode \
  -p ControlGroup
```

Why:

- stable key-value output
- cheap to parse
- enough to determine service state, cgroup path, and top-level resource summary

### Process inventory

Use:

```bash
ps -eo pid=,ppid=,pgid=,stat=,%cpu=,%mem=,rss=,etimes=,comm=,args=
```

Why:

- gives the full candidate process set
- `etimes` is easier to compare than locale-dependent elapsed text
- `rss` provides the main memory estimate we need

### Cgroup membership

Use:

- `ControlGroup` from `systemctl show`
- `/proc/<pid>/cgroup` for each candidate process

Why this is preferred over parsing `systemd-cgls`:

- machine-readable
- easier to test
- robust for process-level classification

`systemd-cgls --user-unit openclaw-gateway.service` is still useful for operator runbooks, but not required as the collector's primary parser.

## Classification model

Each process should be classified along 4 axes.

### 1) Family

Initial families:

- `openclaw-gateway`
- `chrome/playwright`
- `ssh-agent`
- `qmd`
- `node`
- `other`

This is a best-effort grouping used for summaries, not a hard security boundary.

### 2) Relation

- `main`
  - service main PID
- `service-child`
  - inside service cgroup and not main
- `orphan`
  - `PPID=1`
- `external`
  - outside service cgroup and not obviously relevant

### 3) State kind

- `live`
- `zombie`

### 4) Risk

- `ok`
  - main process healthy
  - or child helper that appears normal
- `warn`
  - helper accumulation
  - `KillMode!=control-group`
  - moderate reclaimable memory
- `alert`
  - service not running
  - orphan residual helpers in cgroup
  - large reclaimable memory
  - zombie accumulation

## What counts as “potentially problematic”

ClawMonitor should not label every helper as a problem.

A process is “potentially problematic” when at least one of these is true:

- it is a zombie
- it is inside the service cgroup, not the main PID, and looks orphaned
- it is in a known helper family and has large RSS
- the service itself is not in a healthy state
- `KillMode` is not `control-group`

This gives a conservative, operator-friendly definition.

## Reclaimable memory estimate

The UI should show an estimate:

- `Potential reclaimable RSS`

Definition for Phase 1:

- sum of RSS for non-zombie processes that are classified as potentially problematic
- exclude main PID
- exclude clearly healthy service children where risk is only informational

This is only an estimate.

It answers:

- “If I later clean the dirty helpers, approximately how much RSS might I get back?”

It does not guarantee:

- that exactly this memory will be returned immediately
- that all of it is safe to reclaim without interrupting work

The UI must label this as `estimate`.

## How to read the current TUI

The current `System` view has 3 visible layers.

### Top area

Line 1:

- `SYSTEM SNAPSHOT`
- current load state: `WAITING / LOADING / READY / ERROR`
- sample age
- total reclaimable estimate
- zombie/helper counts

Line 2:

- compact service summary
- `Svc`
- `KillMode`
- `MainPID`
- `Tasks`
- `Mem`
- `Procs`
- `Prob`
- `Reclaim`

This line is meant to answer:

- “Is the gateway basically healthy right now?”
- “Is the cgroup obviously dirty?”

### Left list

The first row is `SERVICE`.
It is not a process family.
It is the overall service/cgroup summary row.

Rows below it are process families such as:

- `chrome/playwright`
- `ssh-agent`
- `qmd`
- `node`
- `other`

Current columns:

- `FAMILY`
  - service row or grouped helper family
- `RISK`
  - `OK / WARN / ALERT`
- `PROC`
  - number of processes in that row
- `RSS`
  - total live resident memory for that row
- `CPU`
  - summed CPU percent for that row when width allows
- `Z`
  - zombie count
- `ORPH`
  - orphan count (`PPID=1`) when width allows
- `RECL`
  - estimated reclaimable RSS for that row

### Right detail

The right pane is intentionally split into labeled sections:

- `SERVICE`
  - unit name, state, kill mode, risk, sample age
- `RESOURCES`
  - main pid, task count, memory, cpu ns, cgroup path
- `COUNTS`
  - procs, helpers, zombies, orphans, problematic, reclaim
- `ISSUES`
  - short reason list for why the service is `WARN/ALERT`
- `PROCESS DETAIL`
  - either top potentially problematic processes
  - or processes within the selected family

This avoids forcing the user to infer structure from plain text.

## Current shortcuts for System view

- `s`
  - jump directly to `System`
- `v`
  - cycle `Sessions -> Models -> System`
- `r`
  - refresh immediately
- `z`
  - cycle pane widths:
    - `10/90`
    - `50/50`
    - `left100`
    - `right100`
- `Esc`
  - reset back to the default surface

## Aggregations

The `System` view should provide 3 levels of summary.

### 1) Service summary

Single summary block:

- service state
- `MainPID`
- `KillMode`
- `TasksCurrent`
- `MemoryCurrent`
- `CPUUsageNSec`
- cgroup path
- sample age
- total process count
- in-cgroup count
- zombie count
- residual helper count
- potential reclaimable RSS estimate

### 2) Family summary table

Rows grouped by family, for example:

- `openclaw-gateway`
- `chrome/playwright`
- `ssh-agent`
- `qmd`
- `node`
- `other`

Suggested columns:

- `FAMILY`
- `RISK`
- `COUNT`
- `RSS`
- `CPU`
- `Z`
- `ORPH`
- `RECLAIM`

This is the primary left-side list.

### 3) Selected-family detail

The right pane should show:

- family summary
- why this family is `OK/WARN/ALERT`
- top processes in that family
- each process:
  - pid / ppid / stat
  - rss / cpu
  - age
  - relation
  - in-cgroup yes/no
  - short args preview

## TUI interaction

`System` should become the third top-level view.

### View switching

- `v`
  - cycle `sessions -> models -> system -> sessions`
- `s`
  - jump directly to `system`

Why:

- `v` keeps consistency with the existing top-level view switch
- `s` gives a direct operator shortcut for service health

### Refresh model

For `System` view:

- auto-refresh while the view is active
- `r` forces immediate refresh
- banner shows:
  - `WAITING`
  - `LOADING`
  - `READY`
  - `ERROR`

This is acceptable because the collector is local and relatively cheap compared with Gateway calls.

### Layout

Use the existing two-pane pattern:

- left: family summary list
- right: service summary + selected family detail

If the terminal is too narrow:

- show the family list
- replace the detail pane with an explicit hint

### Colors

Suggested mapping:

- green: healthy service / healthy families
- yellow: warning / stale / non-ideal kill mode
- red: service down, high residual RSS, zombies, orphan helpers
- cyan: loading / active refresh

### Banner text examples

- `SYSTEM SNAPSHOT: WAITING  Press [r] to inspect service and cgroup state.`
- `SYSTEM SNAPSHOT: LOADING elapsed=2s  reading systemctl + ps + /proc cgroups`
- `SYSTEM SNAPSHOT: READY  sampleAge=1s  reclaimable~3.4G  zombies=18  residual=42`
- `SYSTEM SNAPSHOT: ERROR  failed to read systemctl output`

## Recommended operator wording

Avoid overly certain wording such as:

- “bad process”
- “must kill”

Prefer:

- `potentially problematic`
- `residual helper`
- `zombie`
- `reclaimable estimate`
- `restart may recover`

This matches the current runbook style and avoids accidental destructive interpretation.

## Performance expectations

The collector should stay lightweight:

- one `systemctl show`
- one `ps`
- `/proc/<pid>/cgroup` only for candidate processes

No transcript scanning, no Gateway calls, no large file reads.

This should normally be fast enough for active-view auto-refresh.

## Known limits

Phase 1 will not try to do true per-agent resource attribution.

Why:

- helper processes are not always cleanly tagged back to a single session or agent
- doing this reliably would require deeper OpenClaw/runtime instrumentation

Phase 1 should instead be honest:

- cgroup-level summary first
- family-level grouping second
- per-process evidence third

## Implementation order

1. Add a local collector module for service + process snapshots
2. Add risk classification and family aggregation
3. Add `System` view to the TUI
4. Add help/footer/banner updates
5. Add unit tests for parsing/classification

## Not in scope for Phase 1

- killing processes
- restarting `openclaw-gateway.service`
- agent/session-level CPU attribution
- historical charts
- exporting Prometheus / OTel metrics

These can come later if the first observational view proves useful.
