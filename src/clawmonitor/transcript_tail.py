from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


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
        elif t == "thinking":
            # Avoid filling previews with long internal thoughts.
            thinking = item.get("thinking")
            if isinstance(thinking, str) and thinking.strip():
                parts.append(f"[thinking] {thinking.strip()}")
    joined = " ".join(parts).strip()
    if not joined:
        return "<empty>"
    return joined[:max_chars]


@dataclass(frozen=True)
class TailMessage:
    role: str
    ts: Optional[datetime]
    preview: str
    stop_reason: Optional[str] = None


@dataclass(frozen=True)
class TranscriptTail:
    last_user: Optional[TailMessage]
    last_assistant: Optional[TailMessage]
    last_tool_error: Optional[Tuple[Optional[datetime], str]]
    last_compaction_at: Optional[datetime]


def tail_transcript(path: Path, max_bytes: int = 65536) -> TranscriptTail:
    if not path.exists():
        return TranscriptTail(None, None, None, None)
    # Read backwards in growing chunks until we find both last user and last assistant
    # (or we hit an upper bound).
    max_total = max(256 * 1024, min(2 * 1024 * 1024, max_bytes * 16))
    size: int
    try:
        size = path.stat().st_size
    except Exception:
        return TranscriptTail(None, None, None, None)

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

        if role == "user" and last_user is None:
            last_user = TailMessage(role="user", ts=ts, preview=_extract_text(msg.get("content")))
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

        if last_user and last_assistant and last_tool_error and last_compaction_at:
            break

    return TranscriptTail(last_user=last_user, last_assistant=last_assistant, last_tool_error=last_tool_error, last_compaction_at=last_compaction_at)
