from __future__ import annotations

from typing import Optional, Tuple

from .acpx_sessions import AcpxSnapshot, load_acpx_snapshot, tail_acpx_messages
from .session_store import SessionMeta
from .transcript_tail import TranscriptTail, tail_transcript


def empty_tail() -> TranscriptTail:
    return TranscriptTail(None, None, None, None, None, None, None)


def tail_for_meta(meta: SessionMeta, *, transcript_tail_bytes: int) -> Tuple[TranscriptTail, Optional[AcpxSnapshot]]:
    """
    Tail a session transcript for monitoring.

    Preference order:
      1) JSONL transcript if present
      2) ACPX session file (for ACP sessions without JSONL)
      3) empty
    """
    if meta.session_file and meta.session_file.exists():
        return tail_transcript(meta.session_file, max_bytes=transcript_tail_bytes), None
    if meta.acpx_session_id:
        snap, doc = load_acpx_snapshot(meta.acpx_session_id)
        if doc:
            return tail_acpx_messages(doc), snap
        return empty_tail(), snap
    return empty_tail(), None

