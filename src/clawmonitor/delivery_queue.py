from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional


@dataclass(frozen=True)
class DeliveryFailure:
    id: str
    session_key: str
    channel: Optional[str]
    to: Optional[str]
    account_id: Optional[str]
    retry_count: int
    last_error: Optional[str]
    enqueued_at_ms: Optional[int]


def load_failed_delivery_map(openclaw_root: Path) -> Dict[str, DeliveryFailure]:
    failed_dir = openclaw_root / "delivery-queue" / "failed"
    out: Dict[str, DeliveryFailure] = {}
    if not failed_dir.exists():
        return out
    for path in failed_dir.rglob("*.json"):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        mirror = doc.get("mirror") if isinstance(doc, dict) else None
        if not isinstance(mirror, dict):
            continue
        session_key = mirror.get("sessionKey")
        if not isinstance(session_key, str) or not session_key:
            continue
        df = DeliveryFailure(
            id=str(doc.get("id") or path.stem),
            session_key=session_key,
            channel=str(doc.get("channel")) if doc.get("channel") is not None else None,
            to=str(doc.get("to")) if doc.get("to") is not None else None,
            account_id=str(doc.get("accountId")) if doc.get("accountId") is not None else None,
            retry_count=int(doc.get("retryCount") or 0),
            last_error=str(doc.get("lastError")) if doc.get("lastError") is not None else None,
            enqueued_at_ms=int(doc.get("enqueuedAt")) if doc.get("enqueuedAt") is not None else None,
        )
        # Keep the latest (best-effort) by retry_count/enqueuedAt.
        prev = out.get(session_key)
        if prev is None:
            out[session_key] = df
        else:
            prev_score = (prev.enqueued_at_ms or 0, prev.retry_count)
            new_score = (df.enqueued_at_ms or 0, df.retry_count)
            if new_score >= prev_score:
                out[session_key] = df
    return out

