from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from .delivery_queue import DeliveryFailure
from .locks import LockInfo
from .transcript_tail import TranscriptTail


class WorkState(str, Enum):
    WORKING = "WORKING"
    FINISHED = "FINISHED"
    INTERRUPTED = "INTERRUPTED"
    NO_MESSAGE = "NO_MESSAGE"


@dataclass(frozen=True)
class SessionComputed:
    state: WorkState
    no_feedback: bool
    long_run_seconds: Optional[int]
    delivery_failed: bool
    safety_alert: bool
    safeguard_alert: bool
    reason: str


def _now() -> datetime:
    return datetime.now(timezone.utc)


def compute_state(
    aborted_last_run: bool,
    tail: TranscriptTail,
    lock: Optional[LockInfo],
    delivery_failure: Optional[DeliveryFailure],
    *,
    safeguard_ok: bool = True,
    long_run_warn_seconds: int = 15 * 60,
    long_run_crit_seconds: int = 60 * 60,
) -> SessionComputed:
    # Strict: only treat "Last User Send" as real inbound user text. Internal
    # control-plane injections are tracked separately as last_trigger.
    user_msg = tail.last_user_send
    last_user_ts = user_msg.ts if user_msg else None
    last_assistant_ts = tail.last_assistant.ts if tail.last_assistant else None

    no_user = user_msg is None
    delivery_failed = delivery_failure is not None

    long_run_seconds: Optional[int] = None
    if lock and lock.created_at:
        long_run_seconds = int((_now() - lock.created_at).total_seconds())

    # safety signals (heuristic)
    stop_reason = (tail.last_assistant.stop_reason if tail.last_assistant else None) or ""
    safety_alert = any(x in stop_reason.lower() for x in ["safety", "content_filter", "refusal"])
    safeguard_alert = not bool(safeguard_ok)

    if lock:
        reason = "lock present"
        if long_run_seconds is not None and long_run_seconds >= long_run_crit_seconds:
            reason = "long run (critical)"
        elif long_run_seconds is not None and long_run_seconds >= long_run_warn_seconds:
            reason = "long run (warn)"
        return SessionComputed(
            state=WorkState.WORKING,
            no_feedback=False,
            long_run_seconds=long_run_seconds,
            delivery_failed=delivery_failed,
            safety_alert=safety_alert,
            safeguard_alert=safeguard_alert,
            reason=reason,
        )

    if no_user:
        return SessionComputed(
            state=WorkState.NO_MESSAGE,
            no_feedback=False,
            long_run_seconds=None,
            delivery_failed=delivery_failed,
            safety_alert=safety_alert,
            safeguard_alert=safeguard_alert,
            reason="no user message found",
        )

    no_feedback = False
    if last_user_ts and (last_assistant_ts is None or last_user_ts > last_assistant_ts):
        no_feedback = True

    if aborted_last_run and no_feedback:
        return SessionComputed(
            state=WorkState.INTERRUPTED,
            no_feedback=True,
            long_run_seconds=None,
            delivery_failed=delivery_failed,
            safety_alert=safety_alert,
            safeguard_alert=safeguard_alert,
            reason="abortedLastRun + pending reply",
        )

    return SessionComputed(
        state=WorkState.FINISHED,
        no_feedback=no_feedback,
        long_run_seconds=None,
        delivery_failed=delivery_failed,
        safety_alert=safety_alert,
        safeguard_alert=safeguard_alert,
        reason="no lock and assistant not behind user",
    )
