from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
import os


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


@dataclass(frozen=True)
class LockInfo:
    pid: Optional[int]
    created_at: Optional[datetime]
    pid_alive: Optional[bool]


def lock_path_for_session_file(session_file: Path) -> Path:
    return Path(str(session_file) + ".lock")


def read_lock(lock_path: Path) -> Optional[LockInfo]:
    if not lock_path.exists():
        return None
    try:
        doc = json.loads(lock_path.read_text(encoding="utf-8"))
    except Exception:
        return LockInfo(pid=None, created_at=None, pid_alive=None)
    pid_val = doc.get("pid")
    pid = int(pid_val) if isinstance(pid_val, int) else None
    created_at = _parse_iso(doc.get("createdAt"))
    pid_alive: Optional[bool] = None
    if pid is not None:
        try:
            os.kill(pid, 0)
            pid_alive = True
        except Exception:
            pid_alive = False
    return LockInfo(pid=pid, created_at=created_at, pid_alive=pid_alive)

