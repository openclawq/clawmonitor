from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


@dataclass(frozen=True)
class SessionMeta:
    agent_id: str
    key: str
    session_id: str
    updated_at_ms: Optional[int]
    session_file: Optional[Path]
    aborted_last_run: bool
    system_sent: bool
    chat_type: Optional[str]
    kind: Optional[str]
    channel: Optional[str]
    account_id: Optional[str]
    to: Optional[str]


def _safe_int(v: Any) -> Optional[int]:
    try:
        if v is None:
            return None
        return int(v)
    except Exception:
        return None


def _load_json(path: Path) -> Dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def iter_agent_ids(openclaw_root: Path) -> Iterable[str]:
    agents_dir = openclaw_root / "agents"
    if not agents_dir.exists():
        return []
    for child in sorted(agents_dir.iterdir()):
        if child.is_dir():
            yield child.name


def list_sessions(openclaw_root: Path) -> List[SessionMeta]:
    out: List[SessionMeta] = []
    agents_dir = openclaw_root / "agents"
    for agent_id in iter_agent_ids(openclaw_root):
        sessions_json = agents_dir / agent_id / "sessions" / "sessions.json"
        if not sessions_json.exists():
            continue
        try:
            doc = _load_json(sessions_json)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        for key, entry in doc.items():
            if not isinstance(entry, dict):
                continue
            session_id = str(entry.get("sessionId", ""))
            if not session_id:
                continue
            updated_at = _safe_int(entry.get("updatedAt"))
            session_file_str = entry.get("sessionFile")
            session_file = Path(session_file_str) if isinstance(session_file_str, str) else None
            aborted_last_run = bool(entry.get("abortedLastRun", False))
            system_sent = bool(entry.get("systemSent", False))
            chat_type = entry.get("chatType")
            kind = entry.get("kind") or entry.get("chatType")
            delivery_context = entry.get("deliveryContext") if isinstance(entry.get("deliveryContext"), dict) else {}
            channel = delivery_context.get("channel") or entry.get("lastChannel") or (entry.get("origin", {}) or {}).get("surface")
            account_id = delivery_context.get("accountId") or entry.get("lastAccountId") or (entry.get("origin", {}) or {}).get("accountId")
            to = delivery_context.get("to") or entry.get("lastTo")
            out.append(
                SessionMeta(
                    agent_id=agent_id,
                    key=str(key),
                    session_id=session_id,
                    updated_at_ms=updated_at,
                    session_file=session_file,
                    aborted_last_run=aborted_last_run,
                    system_sent=system_sent,
                    chat_type=str(chat_type) if chat_type is not None else None,
                    kind=str(kind) if kind is not None else None,
                    channel=str(channel) if channel is not None else None,
                    account_id=str(account_id) if account_id is not None else None,
                    to=str(to) if to is not None else None,
                )
            )
    out.sort(key=lambda s: (s.updated_at_ms or 0), reverse=True)
    return out

