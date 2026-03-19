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
    origin_label: Optional[str]
    parent_session_key: Optional[str]
    acp_state: Optional[str]
    acpx_session_id: Optional[str]
    acp_agent: Optional[str]
    acp_identity_state: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    total_tokens: Optional[int]
    context_tokens: Optional[int]
    total_tokens_fresh: Optional[bool]
    cache_read_tokens: Optional[int]
    cache_write_tokens: Optional[int]
    model_provider: Optional[str]
    model_name: Optional[str]


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
            origin = entry.get("origin") if isinstance(entry.get("origin"), dict) else {}
            origin_label = origin.get("label")
            parent_session_key = entry.get("parentSessionKey")

            acp_state: Optional[str] = None
            acpx_session_id: Optional[str] = None
            acp_agent: Optional[str] = None
            acp_identity_state: Optional[str] = None
            acp = entry.get("acp")
            if isinstance(acp, dict):
                st = acp.get("state")
                acp_state = str(st) if isinstance(st, str) and st else None
                ag = acp.get("agent")
                acp_agent = str(ag) if isinstance(ag, str) and ag else None
                ident = acp.get("identity")
                if isinstance(ident, dict):
                    sid = ident.get("acpxSessionId")
                    acpx_session_id = str(sid) if isinstance(sid, str) and sid else None
                    ist = ident.get("state")
                    acp_identity_state = str(ist) if isinstance(ist, str) and ist else None

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
                    origin_label=str(origin_label) if isinstance(origin_label, str) and origin_label else None,
                    parent_session_key=str(parent_session_key) if isinstance(parent_session_key, str) and parent_session_key else None,
                    acp_state=acp_state,
                    acpx_session_id=acpx_session_id,
                    acp_agent=acp_agent,
                    acp_identity_state=acp_identity_state,
                    input_tokens=_safe_int(entry.get("inputTokens")),
                    output_tokens=_safe_int(entry.get("outputTokens")),
                    total_tokens=_safe_int(entry.get("totalTokens")),
                    context_tokens=_safe_int(entry.get("contextTokens")),
                    total_tokens_fresh=bool(entry.get("totalTokensFresh")) if "totalTokensFresh" in entry else None,
                    cache_read_tokens=_safe_int(entry.get("cacheRead")),
                    cache_write_tokens=_safe_int(entry.get("cacheWrite")),
                    model_provider=str(entry.get("modelProvider")) if isinstance(entry.get("modelProvider"), str) and entry.get("modelProvider") else None,
                    model_name=str(entry.get("model")) if isinstance(entry.get("model"), str) and entry.get("model") else None,
                )
            )
    out.sort(key=lambda s: (s.updated_at_ms or 0), reverse=True)
    return out
