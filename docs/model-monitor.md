# Model Monitor

ClawMonitor now has a model-level monitor in addition to the original session monitor.

## Why

OpenClaw session status alone cannot tell you whether a model is healthy:

- the provider API may already be down
- auth or billing may have broken
- the direct API path may work, but the OpenClaw execution path may still hang
- one slow primary model can block the user-visible fallback chain

The model monitor is meant to answer that gap quickly.

## Probe Modes

`clawmonitor models` supports two probe paths:

- `direct`
  - calls the provider endpoint directly
  - measures latency and a simple efficiency metric
  - classifies failures into `timeout`, `network`, `auth`, `billing`, `rate_limit`, `overloaded`, `unsupported`, or `error`
- `openclaw`
  - creates a temporary session with `sessions.patch`
  - runs `agent`
  - waits with `agent.wait`
  - deletes the temporary session in `finally`
  - catches cases where the API itself is healthy but OpenClaw still stalls

`both` is the default and is usually what you want.

## Transport Detection

For direct probes, ClawMonitor inspects provider config and currently supports:

- `openai-completions`
- `openai-responses`
- `anthropic-messages`

If the provider explicitly declares `api`, that wins.
If not, ClawMonitor falls back to simple heuristics from `baseUrl` and headers.

Unsupported transports are reported as `UNSUPPORTED` instead of being treated as a timeout.

## Auth Resolution

Direct probes try to use the same credentials OpenClaw would normally use:

1. agent-local `auth-profiles.json`
2. `lastGood` profile for that provider
3. first matching provider profile
4. provider-level `apiKey` / `token` / `key`

Environment indirections like `env:NAME` and `secretref-env:NAME` are resolved automatically.

## TUI Design

The TUI keeps session monitoring as the default view and adds a separate model view:

- `v`
  - switch between `Sessions` and `Models`
- `r`
  - refresh the current view
  - session view keeps its existing auto-refresh
  - model view is manual-refresh only
- top banner
  - `WAITING`: idle, waiting for you to press `r`
  - `RUNNING`: probe batch is in progress, with step/message/elapsed time
  - `DONE`: last probe batch finished successfully
  - `ERROR`: last batch ended with an execution error

This avoids mixing session rows and model rows into one overloaded screen.

If the list gets long:

- `PgUp` / `PgDn` for page navigation
- `g` / `G` to jump to the top or bottom

## Current Limits

- direct probing is only implemented for the transport types listed above
- model view does not auto-refresh
- a model row is keyed by effective `agent + modelRef`, because auth and OpenClaw behavior can differ per agent

## CLI Examples

```bash
clawmonitor models
clawmonitor models --mode direct --format json
clawmonitor models --mode openclaw --timeout 15
```
