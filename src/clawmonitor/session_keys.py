from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class SessionKeyInfo:
    kind: str
    channel: Optional[str]
    subagent_depth: int
    parent_key_hint: Optional[str]


def _count_segments(parts: list[str], seg: str) -> int:
    return sum(1 for p in parts if p == seg)


def _subagent_parent(session_key: str) -> Optional[str]:
    parts = session_key.split(":")
    # Find the last "subagent" segment and drop it + its following id.
    for i in range(len(parts) - 1, -1, -1):
        if parts[i] == "subagent":
            # Drop i and i+1.
            return ":".join(parts[:i])
    return None


def parse_session_key(session_key: str) -> SessionKeyInfo:
    """
    Parse OpenClaw session key shape.

    Common forms:
      - agent:<agentId>:main
      - agent:<agentId>:heartbeat
      - agent:<agentId>:cron:<jobId>
      - agent:<agentId>:cron:<jobId>:run:<uuid>
      - agent:<agentId>:<channel>:...
      - agent:<agentId>:subagent:<uuid>[:subagent:<uuid>...]
      - agent:<agentId>:acp:<uuid>
    """
    key = (session_key or "").strip()
    if key.startswith("cron:"):
        # Cron wake-reason style session key (used in logs/heartbeat reasons).
        return SessionKeyInfo(kind="cron", channel=None, subagent_depth=0, parent_key_hint=None)
    parts = key.split(":") if key else []
    if len(parts) < 3 or parts[0] != "agent":
        return SessionKeyInfo(kind="unknown", channel=None, subagent_depth=0, parent_key_hint=None)

    third = parts[2] if len(parts) >= 3 else ""
    if third == "main":
        return SessionKeyInfo(kind="main", channel=None, subagent_depth=0, parent_key_hint=None)
    if third == "heartbeat":
        return SessionKeyInfo(kind="heartbeat", channel=None, subagent_depth=0, parent_key_hint=None)
    if third == "acp":
        return SessionKeyInfo(kind="acp", channel=None, subagent_depth=0, parent_key_hint=None)
    if third == "cron":
        # Base: agent:<aid>:cron:<jobId>
        # Run record: agent:<aid>:cron:<jobId>:run:<uuid>
        if len(parts) >= 6 and parts[4] == "run":
            parent = ":".join(parts[:4])
            return SessionKeyInfo(kind="cron_run", channel=None, subagent_depth=0, parent_key_hint=parent)
        return SessionKeyInfo(kind="cron", channel=None, subagent_depth=0, parent_key_hint=None)
    if third == "subagent":
        depth = _count_segments(parts, "subagent")
        parent = _subagent_parent(key) if depth > 1 else None
        return SessionKeyInfo(kind="subagent", channel=None, subagent_depth=depth, parent_key_hint=parent)

    # Otherwise treat the third segment as a channel surface (telegram/feishu/discord/slack/...)
    channel = third or None
    return SessionKeyInfo(kind="channel", channel=channel, subagent_depth=0, parent_key_hint=None)


def is_modelprobe_session_key(session_key: str) -> bool:
    key = (session_key or "").strip()
    if not key:
        return False
    parts = key.split(":")
    return len(parts) >= 4 and parts[0] == "agent" and parts[2] == "modelprobe"
