from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

from .transcript_tail import TailMessage, ToolCallEvent, ToolResultEvent, TranscriptTail


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def acpx_session_path(acpx_session_id: str) -> Path:
    return Path.home() / ".acpx" / "sessions" / f"{acpx_session_id}.json"


@dataclass(frozen=True)
class AcpxSnapshot:
    acpx_session_id: str
    pid: Optional[int]
    closed: bool
    last_used_at: Optional[datetime]
    last_prompt_at: Optional[datetime]
    last_agent_exit_at: Optional[datetime]
    updated_at: Optional[datetime]
    last_is_error: bool


def load_acpx_snapshot(acpx_session_id: str) -> Tuple[Optional[AcpxSnapshot], Optional[Dict[str, Any]]]:
    """
    Load an ACPX session json. Returns (snapshot, raw_doc) where raw_doc may be
    needed for message tailing.
    """
    path = acpx_session_path(acpx_session_id)
    if not path.exists():
        return None, None
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None, None
    if not isinstance(doc, dict):
        return None, None
    pid_val = doc.get("pid")
    pid = int(pid_val) if isinstance(pid_val, int) else None
    closed = bool(doc.get("closed", False))
    last_used_at = _parse_iso(doc.get("last_used_at"))
    last_prompt_at = _parse_iso(doc.get("last_prompt_at"))
    last_agent_exit_at = _parse_iso(doc.get("last_agent_exit_at"))
    updated_at = _parse_iso(doc.get("updated_at"))

    # Determine whether the last Agent content contains an error tool result.
    last_is_error = False
    msgs = doc.get("messages", [])
    if isinstance(msgs, list):
        for msg in reversed(msgs):
            if not isinstance(msg, dict) or "Agent" not in msg:
                continue
            agent = msg.get("Agent")
            if not isinstance(agent, dict):
                break
            content = agent.get("content", [])
            if not isinstance(content, list):
                break
            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                tr = block.get("ToolResult")
                if isinstance(tr, dict):
                    last_is_error = bool(tr.get("is_error", False))
                    break
            break

    snap = AcpxSnapshot(
        acpx_session_id=acpx_session_id,
        pid=pid,
        closed=closed,
        last_used_at=last_used_at,
        last_prompt_at=last_prompt_at,
        last_agent_exit_at=last_agent_exit_at,
        updated_at=updated_at,
        last_is_error=last_is_error,
    )
    return snap, doc


def acpx_is_working(snap: Optional[AcpxSnapshot]) -> bool:
    if not snap:
        return False
    if snap.closed:
        return False
    if snap.last_agent_exit_at is not None:
        return False
    # If there's no explicit exit marker, treat as working even if pid is
    # missing (some backends may not expose it reliably).
    return True


def _iter_acpx_messages(doc: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
    msgs = doc.get("messages", [])
    if not isinstance(msgs, list):
        return []
    for ent in msgs:
        if isinstance(ent, dict):
            yield ent


def tail_acpx_messages(doc: Dict[str, Any], *, max_preview_chars: int = 400) -> TranscriptTail:
    """
    Best-effort: map ACPX messages to the same tail fields we use for JSONL transcripts.
    """
    last_user: Optional[TailMessage] = None
    last_user_send: Optional[TailMessage] = None
    last_assistant: Optional[TailMessage] = None
    last_assistant_thinking: Optional[str] = None
    last_tool_error: Optional[Tuple[Optional[datetime], str]] = None
    last_tool_result: Optional[ToolResultEvent] = None
    last_tool_call: Optional[ToolCallEvent] = None

    # ACPX messages usually do not include per-message timestamps; keep ts=None.
    for msg in reversed(list(_iter_acpx_messages(doc))[-400:]):
        if "User" in msg and last_user_send is None:
            user = msg.get("User")
            if isinstance(user, dict):
                content = user.get("content", [])
                text = ""
                if isinstance(content, list):
                    for item in content:
                        if isinstance(item, dict) and "Text" in item and isinstance(item["Text"], str):
                            text = item["Text"].strip()
                            break
                if text:
                    last_user = TailMessage(role="user", ts=None, preview=text[:max_preview_chars])
                    last_user_send = last_user
            continue

        if "Agent" in msg and last_assistant is None:
            agent = msg.get("Agent")
            if not isinstance(agent, dict):
                continue
            content = agent.get("content", [])
            if not isinstance(content, list):
                continue
            text_parts: list[str] = []
            think_parts: list[str] = []
            tool_calls: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                if "Thinking" in item:
                    th = item["Thinking"]
                    if isinstance(th, dict) and isinstance(th.get("text"), str) and th.get("text", "").strip():
                        think_parts.append(th.get("text", "").strip())
                if "ToolCall" in item:
                    tc = item.get("ToolCall")
                    if isinstance(tc, dict):
                        nm = tc.get("name")
                        if isinstance(nm, str) and nm.strip() and nm.strip() not in tool_calls:
                            tool_calls.append(nm.strip())
                if "Text" in item and isinstance(item["Text"], str) and item["Text"].strip():
                    text_parts.append(item["Text"].strip())
                if "ToolResult" in item:
                    tr = item.get("ToolResult")
                    if isinstance(tr, dict):
                        is_error = bool(tr.get("is_error", False))
                        nm = tr.get("tool_name") or tr.get("name") or tr.get("toolName")
                        tool_name = str(nm).strip() if isinstance(nm, str) and nm.strip() else "tool"
                        content2 = tr.get("content")
                        preview = ""
                        if isinstance(content2, dict) and isinstance(content2.get("Text"), str):
                            preview = content2.get("Text", "")
                        elif isinstance(content2, str):
                            preview = content2
                        if last_tool_result is None:
                            last_tool_result = ToolResultEvent(
                                ts=None,
                                tool_name=tool_name,
                                is_error=is_error,
                                preview=(preview or "")[:240],
                                tool_call_id=None,
                            )
                        if is_error and last_tool_error is None:
                            last_tool_error = (None, f"{tool_name} error: {(preview or '')[:160]}".strip())

            preview = " ".join(text_parts).strip() or "<no text>"
            last_assistant = TailMessage(role="assistant", ts=None, preview=preview[:max_preview_chars], stop_reason=None)
            joined_think = " ".join(think_parts).strip()
            last_assistant_thinking = joined_think[:max_preview_chars] if joined_think else None
            if tool_calls and last_tool_call is None:
                last_tool_call = ToolCallEvent(ts=None, tool_names=tool_calls[:6])
            continue

        if last_user_send and last_assistant:
            break

    return TranscriptTail(
        last_user=last_user,
        last_user_send=last_user_send,
        last_trigger=None,
        last_assistant=last_assistant,
        last_assistant_thinking=last_assistant_thinking,
        last_tool_error=last_tool_error,
        last_tool_result=last_tool_result,
        last_tool_call=last_tool_call,
        last_compaction_at=None,
    )
