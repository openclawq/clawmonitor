from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .config import state_dir
from .redact import redact_text


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Event:
    event: str
    ts: str
    data: Dict[str, Any]


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

