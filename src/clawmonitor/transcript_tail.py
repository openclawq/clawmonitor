from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
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


_MARKER_PATTERN = re.compile(r"\[\[[^\]]*\]\]")
_GATEWAY_TIME_PREFIX_PATTERN = re.compile(
    r"^\[[A-Za-z]{3}\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[^\]]*\]\s*"
)


def _strip_markers(text: str) -> str:
    return _MARKER_PATTERN.sub("", text or "").strip()


def _strip_gateway_time_prefix(text: str) -> str:
    if not text:
        return ""
    return _GATEWAY_TIME_PREFIX_PATTERN.sub("", str(text), count=1).strip()


def _clean_preview(text: str) -> str:
    # Keep previews readable: drop gateway envelope prefix and internal markers.
    return _strip_markers(_strip_gateway_time_prefix(text or ""))


_INTERNAL_USER_PATTERNS = [
    re.compile(r"^Skills store policy\s*\(operator configured\):", re.IGNORECASE),
    re.compile(r"^Conversation info \(untrusted metadata\):", re.IGNORECASE),
    re.compile(r"^Sender \(untrusted metadata\):", re.IGNORECASE),
    re.compile(r"^\[Queued messages while agent was busy\]", re.IGNORECASE),
    re.compile(r"^Queued messages while agent was busy", re.IGNORECASE),
    re.compile(r"^\[ClawMonitor nudge\]", re.IGNORECASE),
    re.compile(r"^Current time:\s*", re.IGNORECASE),
    re.compile(r"^System:\s*\[\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?\s+GMT[^\]]*\]\s*", re.IGNORECASE),
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

    # Be conservative: only attempt wrapper extraction when there are strong
    # signals that this "user" message is a channel wrapper containing real
    # inbound user content (e.g., Telegram wrapper with untrusted metadata).
    lower = s.lower()
    has_meta_hint = bool(_META_BLOCK_RE.search(s)) or ("untrusted metadata" in lower)
    if not has_meta_hint:
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
    # Only use "sender_id: message" extraction when we actually know the sender_id.
    # Otherwise, JSON metadata blocks may contain many ":" lines (e.g. `"label": ...`)
    # which are not real inbound user text.
    if sender_id:
        for ln in reversed(lines[-20:]):
            # Avoid mis-parsing gateway time envelopes like:
            #   [Wed 2026-03-11 12:02 GMT+8] hello
            # which contain ":" and would match sender-line regex.
            if ln.startswith("[") and "GMT" in ln and "]" in ln:
                continue
            mm = _SENDER_LINE_RE.match(ln)
            if not mm:
                continue
            sender = (mm.group(1) or "").strip()
            msg = (mm.group(2) or "").strip()
            if not msg:
                continue
            if sender.startswith("[") and "GMT" in sender:
                continue
            if sender.lower() in ("message_id", "sender_id", "sender", "timestamp", "current time", "system"):
                continue
            if sender != sender_id:
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
    model: Optional[str] = None
    provider: Optional[str] = None


@dataclass(frozen=True)
class ToolResultEvent:
    ts: Optional[datetime]
    tool_name: str
    is_error: bool
    preview: str
    tool_call_id: Optional[str] = None


@dataclass(frozen=True)
class ToolCallEvent:
    ts: Optional[datetime]
    tool_names: List[str]


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
    last_assistant_thinking: Optional[str]
    last_tool_error: Optional[Tuple[Optional[datetime], str]]
    last_compaction_at: Optional[datetime] = None
    last_entry_type: Optional[str] = None
    last_entry_ts: Optional[datetime] = None
    # last_tool_result: newest toolResult message (success or error).
    last_tool_result: Optional[ToolResultEvent] = None
    # last_tool_call: newest assistant message toolCall names (if any).
    last_tool_call: Optional[ToolCallEvent] = None


def _extract_thinking(content: Any, max_chars: int = 400) -> str:
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "thinking":
            continue
        thinking = item.get("thinking")
        if isinstance(thinking, str) and thinking.strip():
            parts.append(thinking.strip())
    joined = " ".join(parts).strip()
    return joined[:max_chars] if joined else ""


def _extract_tool_call_names(content: Any, *, max_calls: int = 6) -> List[str]:
    """
    Extract tool call names from an assistant message content list:
      { "type": "toolCall", "name": "...", ... }
    """
    if not isinstance(content, list):
        return []
    out: List[str] = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "toolCall":
            continue
        nm = item.get("name")
        if not isinstance(nm, str):
            continue
        nm = nm.strip()
        if not nm:
            continue
        if nm not in out:
            out.append(nm)
        if len(out) >= max_calls:
            break
    return out


def tail_transcript(path: Path, max_bytes: int = 65536) -> TranscriptTail:
    if not path.exists():
        return TranscriptTail(None, None, None, None, None, None, None)
    # Read backwards in growing chunks until we find both last user and last assistant
    # (or we hit an upper bound).
    max_total = max(256 * 1024, min(2 * 1024 * 1024, max_bytes * 16))
    size: int
    try:
        size = path.stat().st_size
    except Exception:
        return TranscriptTail(None, None, None, None, None, None, None)

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
    last_assistant_thinking: Optional[str] = None
    last_tool_result: Optional[ToolResultEvent] = None
    last_tool_call: Optional[ToolCallEvent] = None
    last_tool_error: Optional[Tuple[Optional[datetime], str]] = None
    last_compaction_at: Optional[datetime] = None
    last_entry_type: Optional[str] = None
    last_entry_ts: Optional[datetime] = None

    for ln in reversed(lines):
        try:
            obj = json.loads(ln)
        except Exception:
            continue

        if isinstance(obj, dict) and last_entry_type is None:
            last_entry_type = str(obj.get("type")) if obj.get("type") is not None else None
            last_entry_ts = _parse_iso(obj.get("timestamp"))

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
            tool_name = msg.get("toolName")
            tool_call_id = msg.get("toolCallId")
            nm = str(tool_name).strip() if isinstance(tool_name, str) else "tool"
            if not nm:
                nm = "tool"
            preview = _clean_preview(_extract_text(msg.get("content"), max_chars=400))
            if last_tool_result is None:
                last_tool_result = ToolResultEvent(
                    ts=ts,
                    tool_name=nm,
                    is_error=is_error,
                    preview=preview[:240] if preview else "",
                    tool_call_id=str(tool_call_id) if isinstance(tool_call_id, str) and tool_call_id else None,
                )
            if is_error and last_tool_error is None:
                summary = f"{nm} error: {str(preview)[:160]}".strip()
                last_tool_error = (ts, summary)
            continue

        if role == "user":
            raw = _extract_text(msg.get("content"), max_chars=8000)
            preview = _clean_preview(raw[:400] if raw else "")
            if last_user is None:
                last_user = TailMessage(role="user", ts=ts, preview=preview)
            if last_trigger is None and _is_internal_user_text(raw):
                last_trigger = TailMessage(role="user", ts=ts, preview=preview)
            if last_user_send is None:
                if _is_internal_user_text(raw):
                    extracted = _extract_inbound_from_internal_wrapper(raw)
                    if extracted:
                        last_user_send = TailMessage(role="user", ts=ts, preview=_clean_preview(extracted[:400]))
                else:
                    last_user_send = TailMessage(role="user", ts=ts, preview=preview)
            continue

        if role == "assistant" and last_assistant is None:
            stop_reason = msg.get("stopReason")
            thinking = _extract_thinking(msg.get("content"), max_chars=400)
            if last_tool_call is None:
                tool_calls = _extract_tool_call_names(msg.get("content"))
                if tool_calls:
                    last_tool_call = ToolCallEvent(ts=ts, tool_names=tool_calls)
            model = msg.get("model")
            provider = msg.get("provider")
            last_assistant = TailMessage(
                role="assistant",
                ts=ts,
                preview=_clean_preview(_extract_text(msg.get("content"))),
                stop_reason=str(stop_reason) if stop_reason is not None else None,
                model=str(model) if isinstance(model, str) and model.strip() else None,
                provider=str(provider) if isinstance(provider, str) and provider.strip() else None,
            )
            last_assistant_thinking = thinking or None
            continue

        # Stop early once we have enough for monitoring UI.
        # We prefer waiting until we have a *real* inbound user message.
        if last_user and last_assistant and last_user_send:
            break

    return TranscriptTail(
        last_user=last_user,
        last_user_send=last_user_send,
        last_trigger=last_trigger,
        last_assistant=last_assistant,
        last_assistant_thinking=last_assistant_thinking,
        last_tool_error=last_tool_error,
        last_tool_result=last_tool_result,
        last_tool_call=last_tool_call,
        last_compaction_at=last_compaction_at,
        last_entry_type=last_entry_type,
        last_entry_ts=last_entry_ts,
    )
