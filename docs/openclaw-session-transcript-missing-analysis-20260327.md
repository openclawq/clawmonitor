# OpenClaw Session Shell Rollovers And Missing Transcripts

Date: 2026-03-27

## Summary

We investigated cases where a session key in `sessions.json` points to a new `sessionId`, but the corresponding transcript file does not exist.

This is not the same as a Linux zombie process issue.

It is a session persistence consistency gap:

- `sessions.json` is the route/index layer
- `*.jsonl` is the actual transcript/history layer
- OpenClaw can rotate a session key to a new session shell before the new transcript file is physically created
- the previous transcript may already have been archived as `.reset.<timestamp>` or `.deleted.<timestamp>`

Result: a monitor or UI can see `transcript missing` even though the route itself has already moved to a fresh session shell.

## What We Confirmed In OpenClaw

Relevant code paths in the local OpenClaw checkout:

- Session rollover logic:
  - `src/auto-reply/reply/session.ts`
- Session file persistence into `sessions.json`:
  - `src/config/sessions/session-file.ts`
- Transcript header creation and append flow:
  - `src/config/sessions/transcript.ts`
- Existing fallback behavior for rotated transcripts:
  - `src/hooks/bundled/session-memory/handler.ts`
- Existing doctor warning for recent sessions missing transcripts:
  - `src/commands/doctor-state-integrity.ts`

Key observations from the code:

1. A reset or stale-session rollover creates a fresh `sessionId` for the same `sessionKey`.
2. The new `sessionId` and resolved `sessionFile` are written into `sessions.json` before the new transcript file is guaranteed to exist.
3. The old transcript is archived after rollover, usually to `.reset.<timestamp>`.
4. The transcript file itself is created later by code paths that append transcript content or explicitly ensure a header.

This means there is a real timing and failure window where:

- the old transcript has already been archived
- the new store entry already points to the new shell
- the new `*.jsonl` file has not yet been created

## Why This Happens

The current OpenClaw behavior is effectively:

1. Evaluate session freshness or detect `/new` or `/reset`
2. Mint a new `sessionId`
3. Persist the new `sessionId` and derived `sessionFile` into `sessions.json`
4. Archive the previous transcript
5. Only later, when transcript persistence happens, create the new `*.jsonl`

If anything interrupts or delays step 5, the store points at a non-existent transcript.

Likely triggers:

- normal rollover timing gap
- run startup failure before first transcript write
- process interruption or crash after store update
- reset path completes but no successful append occurs afterward

## Is This A Bug?

The rollover itself is expected behavior.

The long-lived state where an active session points to a missing transcript should be treated as an OpenClaw robustness bug, or at minimum a consistency bug.

Reasons:

- OpenClaw's own doctor command warns when recent sessions are missing transcripts
- OpenClaw's own session-memory hook already contains fallback logic to read the latest `.reset.*` file when the current transcript cannot be used
- a route/index layer that permanently points to a non-existent primary history file is not a healthy steady state

## Does This Affect Real Usage?

Yes, potentially.

It does not always mean the chat route is completely dead, but it can affect behavior in several ways.

### Cases That May Still Work

- The next successful turn may create the new transcript file and recover normal behavior.
- A live model call can still run if the runtime already has enough routing context and later persists successfully.

### Cases That Break Or Degrade

- History panels may look empty or appear reset.
- Token continuity and transcript-derived previews may be wrong.
- Session-memory or export logic may need to fall back to archived transcripts.
- Monitoring tools may report missing transcript, no recent user/assistant messages, or missing details.
- If the underlying run also fails before transcript creation, the session can appear to reset without usable history.

Practical conclusion:

- `transcript missing` is not just a cosmetic issue.
- It is often recoverable.
- If it persists on an active human chat, it can materially degrade OpenClaw usability.

## What We Observed Locally

In the local state on this machine, we found Feishu session keys whose current `sessionId` pointed to transcript files that do not exist.

At the same time, older archived transcripts for the same route still existed as `.jsonl.reset.<timestamp>` files and contained real conversation history.

That confirms the pattern above:

- the route was rolled over
- the current shell exists in `sessions.json`
- the actual usable history remained in older archived transcript files

## Current ClawMonitor Handling

ClawMonitor now treats this state more defensively:

- it first tries the current `sessionFile`
- then the derived `<sessionId>.jsonl`
- then `.reset.*` and `.deleted.*` variants
- then related transcripts for the same route or target

This lets the TUI recover useful history even when OpenClaw's current shell transcript is missing.

## Recommended Upstream Fix

The safest upstream fix is:

- when a new session shell is created for an active chat route, create the new transcript file header immediately after resolving the new `sessionFile`
- do this before or at least atomically with the rollover completion, so `sessions.json` does not point to a missing primary transcript in steady state

Expected effect:

- current shell always has a real `*.jsonl`
- history UIs and monitors stop seeing false-empty active sessions
- old transcript archival still works as before

Potential tradeoff:

- some sessions will now have header-only transcript files if no real message append happens afterward

That tradeoff is acceptable. A header-only transcript is much safer than a store entry that points to a non-existent file.

## Operator Guidance

When you see `transcript missing`:

1. Check whether the session was just reset or rolled over recently.
2. Look for `.reset.*` or `.deleted.*` siblings in the same sessions directory.
3. Treat a recent one-off missing transcript as recoverable.
4. Treat repeated or persistent missing transcripts on active sessions as an OpenClaw issue worth fixing upstream.

For cleanup:

- do not assume these are process zombies
- do not delete active session entries blindly
- prefer route-aware inspection and archive fallback first

## Upstream Status

Planned follow-up from this analysis:

- upstream PR opened: `openclaw/openclaw#55817`
  - https://github.com/openclaw/openclaw/pull/55817
- the PR makes active session rollovers create a transcript header immediately
- the PR includes regression coverage for stale-session rollover and explicit `/reset`
