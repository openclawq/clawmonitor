from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import state_dir
from .redact import redact_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    event: str
    ts: str
    data: Dict[str, Any]


def read_recent_events(path: Path, *, limit: int = 20) -> List[Event]:
    if limit <= 0 or not path.exists():
        return []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return []
    out: List[Event] = []
    for raw in reversed(lines):
        line = (raw or "").strip()
        if not line:
            continue
        try:
            doc = json.loads(line)
        except Exception:
            continue
        if not isinstance(doc, dict):
            continue
        event = str(doc.get("event") or "").strip()
        ts = str(doc.get("ts") or "").strip()
        data = doc.get("data")
        if not event or not ts or not isinstance(data, dict):
            continue
        out.append(Event(event=event, ts=ts, data=data))
        if len(out) >= limit:
            break
    return out


class EventLog:
    def __init__(self, path: Optional[Path] = None) -> None:
        base = state_dir()
        base.mkdir(parents=True, exist_ok=True)
        self._path = path or (base / "events.jsonl")

    @property
    def path(self) -> Path:
        return self._path

    def write(self, event: str, **data: Any) -> None:
        safe: Dict[str, Any] = {}
        for k, v in data.items():
            if isinstance(v, str):
                safe[k] = redact_text(v)
            else:
                safe[k] = v
        line = json.dumps(asdict(Event(event=event, ts=_now_iso(), data=safe)), ensure_ascii=False)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
