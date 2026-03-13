from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .session_store import SessionMeta


def _id_from_key_tail(session_key: str) -> Optional[str]:
    key = (session_key or "").strip()
    if not key:
        return None
    parts = key.split(":")
    if parts:
        last = parts[-1].strip()
        return last or None
    return None


def session_display_label(label_map: dict[str, str], meta: SessionMeta) -> Optional[str]:
    """
    Resolve a human-friendly label for a session.

    Label lookup precedence:
      1) labels[\"sessionKey:<full sessionKey>\"] — exact mapping
      2) labels[\"target:<channel>:<to>\"] — delivery target mapping (meta.to)
      3) labels[\"id:<channel>:<id>\"] — id tail mapping (last key segment)
      4) meta.origin_label — if available (best-effort)
    """
    key = (meta.key or "").strip()
    if key:
        v = label_map.get(f"sessionKey:{key}")
        if v:
            return v

    chan = (meta.channel or "").strip() or None
    if chan and meta.to:
        v = label_map.get(f"target:{chan}:{meta.to}")
        if v:
            return v

    if chan and key:
        tail = _id_from_key_tail(key)
        if tail:
            v = label_map.get(f"id:{chan}:{tail}")
            if v:
                return v

    if meta.origin_label:
        return meta.origin_label
    return None
