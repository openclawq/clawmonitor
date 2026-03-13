from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
import re


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        # Python 3.10: fromisoformat supports offsets.
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_text(content: Any, max_chars: int = 400) -> str:
    if isinstance(content, str):
        s = content.strip()
        return s[:max_chars]
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        t = item.get("type")
        if t in ("text", "output_text"):
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(text.strip())
    joined = " ".join(parts).strip()
    if not joined:
        return "<empty>"
    return joined[:max_chars]


_INTERNAL_USER_PATTERNS = [
    re.compile(r"^Skills store policy\s*\(operator configured\):", re.IGNORECASE),
    re.compile(r"^Conversation info \(untrusted metadata\):", re.IGNORECASE),
    re.compile(r"^Sender \(untrusted metadata\):", re.IGNORECASE),
    re.compile(r"^\[Queued messages while agent was busy\]", re.IGNORECASE),
    re.compile(r"^Queued messages while agent was busy", re.IGNORECASE),
    re.compile(r"^\[ClawMonitor nudge\]", re.IGNORECASE),
    re.compile(r"^Current time:\s*", re.IGNORECASE),
    re.compile(
        r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+[A-Za-z]+\s+\d{1,2}(st|nd|rd|th),\s+\d{4}\s+—\s+",
        re.IGNORECASE,
    ),
    re.compile(r"^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(:\d{2})?\s+\([A-Za-z_]+/[A-Za-z_]+\)", re.IGNORECASE),
]


def _is_internal_user_text(text: str) -> bool:
    s = (text or "").strip()
    if not s:
        return False
    return any(p.search(s) for p in _INTERNAL_USER_PATTERNS)


_META_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.IGNORECASE | re.DOTALL)
_SENDER_LINE_RE = re.compile(r"^([^:\n]{1,200}):\s*(.+)$")


def _extract_inbound_from_internal_wrapper(text: str) -> Optional[str]:
    """
    Best-effort: internal wrappers often include a JSON metadata block and then a final line like:
      <sender_id>: <actual user message>
    We treat that suffix message as the real user inbound when sender_id is not 'cli'.
    """
    s = (text or "").strip()
    if not s:
        return None

    sender_id: Optional[str] = None
    m = _META_BLOCK_RE.search(s)
    if m:
        try:
            meta = json.loads(m.group(1))
            if isinstance(meta, dict):
                sid = meta.get("sender_id") or meta.get("senderId") or meta.get("sender")
                if isinstance(sid, str):
                    sender_id = sid.strip()
        except Exception:
            sender_id = None

    if sender_id and sender_id.lower() in ("cli", "system", "gateway", "openclaw"):
        return None

    lines = [ln.strip() for ln in s.splitlines() if ln.strip()]
    for ln in reversed(lines[-20:]):
        mm = _SENDER_LINE_RE.match(ln)
        if not mm:
            continue
        sender = (mm.group(1) or "").strip()
        msg = (mm.group(2) or "").strip()
        if not msg:
            continue
        if sender.lower() in ("message_id", "sender_id", "sender", "timestamp"):
            continue
        # If we have a sender_id, prefer matching it, otherwise accept any.
        if sender_id and sender != sender_id:
            continue
        return msg

    # Fallback: some wrappers append the real user message as a plain trailing
    # line/paragraph (no "sender_id: ..." prefix). Example:
    #   ... Sender (untrusted metadata): ```json {...} ```
    #   <actual user text>
    # We treat the post-metadata tail as the real inbound.
    tail = ""
    last_meta = None
    for m2 in _META_BLOCK_RE.finditer(s):
        last_meta = m2
    if last_meta:
        tail = s[last_meta.end() :].strip()
    else:
        tail = s.strip()

    # Drop stray code fences/braces in case the tail begins with them.
    tail_lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
    tail_lines = [ln for ln in tail_lines if not ln.startswith("```") and ln not in ("{", "}", "[", "]")]
    if tail_lines:
        candidate = "\n".join(tail_lines).strip()
        if candidate and not _is_internal_user_text(candidate):
            return candidate

    # Last resort: pick the last non-internal non-metadata line.
    for ln in reversed(lines[-40:]):
        if ln.startswith("```"):
            continue
        if ln in ("{", "}", "[", "]"):
            continue
        if ln.lower().startswith(("conversation info", "sender (untrusted metadata)", "sender:", "skills store policy")):
            continue
        if not _is_internal_user_text(ln):
            return ln.strip()
    return None


@dataclass(frozen=True)
class TailMessage:
    role: str
    ts: Optional[datetime]
    preview: str
    stop_reason: Optional[str] = None


@dataclass(frozen=True)
class TranscriptTail:
    # last_user: the newest role=user message (may include internal triggers).
    last_user: Optional[TailMessage]
    # last_user_send: best-effort "real inbound" user message, skipping common internal
    # control-plane injections used by CLI/harness.
    last_user_send: Optional[TailMessage]
    # last_trigger: newest internal/control-plane trigger (if present).
    last_trigger: Optional[TailMessage]
    last_assistant: Optional[TailMessage]
    last_tool_error: Optional[Tuple[Optional[datetime], str]]
    last_compaction_at: Optional[datetime]


def tail_transcript(path: Path, max_bytes: int = 65536) -> TranscriptTail:
    if not path.exists():
        return TranscriptTail(None, None, None, None, None, None)
    # Read backwards in growing chunks until we find both last user and last assistant
    # (or we hit an upper bound).
    max_total = max(256 * 1024, min(2 * 1024 * 1024, max_bytes * 16))
    size: int
    try:
        size = path.stat().st_size
    except Exception:
        return TranscriptTail(None, None, None, None, None, None)

    gathered_text = ""
    read_total = 0
    chunk = max_bytes
    while True:
        start = max(0, size - read_total - chunk)
        to_read = min(chunk, size - start)
        if to_read <= 0:
            break
        try:
            with path.open("rb") as f:
                f.seek(start)
                buf = f.read(to_read)
        except Exception:
            break
        piece = buf.decode("utf-8", errors="replace")
        gathered_text = piece + gathered_text
        read_total = size - start
        if read_total >= max_total or start == 0:
            break
        chunk = min(chunk * 2, max_total - read_total)

    lines = [ln for ln in gathered_text.splitlines() if ln.strip().startswith("{")]

    last_user: Optional[TailMessage] = None
    last_user_send: Optional[TailMessage] = None
    last_trigger: Optional[TailMessage] = None
    last_assistant: Optional[TailMessage] = None
    last_tool_error: Optional[Tuple[Optional[datetime], str]] = None
    last_compaction_at: Optional[datetime] = None

    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue

        if isinstance(obj, dict) and obj.get("type") == "compaction" and last_compaction_at is None:
            last_compaction_at = _parse_iso(obj.get("timestamp"))

        if not (isinstance(obj, dict) and obj.get("type") == "message"):
            continue

        ts = _parse_iso(obj.get("timestamp"))
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role == "toolResult":
            is_error = bool(msg.get("isError", False))
            if is_error and last_tool_error is None:
                # Keep a short summary; details can be inspected via logs.
                tool_name = msg.get("toolName")
                content = msg.get("content")
                preview = ""
                if isinstance(content, list) and content:
                    first = content[0]
                    if isinstance(first, dict):
                        preview = first.get("text") or ""
                summary = f"{tool_name or 'tool'} error: {str(preview)[:160]}".strip()
                last_tool_error = (ts, summary)
            continue

        if role == "user":
            raw = _extract_text(msg.get("content"), max_chars=8000)
            preview = raw[:400] if raw else ""
            if last_user is None:
                last_user = TailMessage(role="user", ts=ts, preview=preview)
            if last_trigger is None and _is_internal_user_text(raw):
                last_trigger = TailMessage(role="user", ts=ts, preview=preview)
            if last_user_send is None:
                if _is_internal_user_text(raw):
                    extracted = _extract_inbound_from_internal_wrapper(raw)
                    if extracted:
                        last_user_send = TailMessage(role="user", ts=ts, preview=extracted[:400])
                else:
                    last_user_send = TailMessage(role="user", ts=ts, preview=preview)
            continue

        if role == "assistant" and last_assistant is None:
            stop_reason = msg.get("stopReason")
            last_assistant = TailMessage(
                role="assistant",
                ts=ts,
                preview=_extract_text(msg.get("content")),
                stop_reason=str(stop_reason) if stop_reason is not None else None,
            )
            continue

        # Stop early once we have enough for monitoring UI.
        if last_user and last_assistant and (last_user_send or last_trigger):
            break

    return TranscriptTail(
        last_user=last_user,
        last_user_send=last_user_send,
        last_trigger=last_trigger,
        last_assistant=last_assistant,
        last_tool_error=last_tool_error,
        last_compaction_at=last_compaction_at,
    )
