from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional
import json
import threading

from .openclaw_cli import gateway_call
from .redact import redact_text


@dataclass(frozen=True)
class GatewayLogLine:
    ts: Optional[datetime]
    subsystem: Optional[str]
    level: Optional[str]
    text: str
    raw: str


def _parse_iso(ts: Any) -> Optional[datetime]:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except Exception:
        return None


def _extract_message(d: Dict[str, Any]) -> str:
    # Many OpenClaw file logs are JSON objects with numeric string keys.
    numeric_keys = [k for k in d.keys() if isinstance(k, str) and k.isdigit()]
    numeric_keys.sort(key=lambda x: int(x))
    parts: List[str] = []
    for k in numeric_keys:
        v = d.get(k)
        if isinstance(v, str) and v.strip():
            parts.append(v.strip())
    if parts:
        # Often k=0 is a JSON-encoded subsystem tag; keep it but it will be parsed elsewhere too.
        return " ".join(parts)
    msg = d.get("msg") or d.get("message")
    if isinstance(msg, str):
        return msg
    return ""


def _extract_subsystem(d: Dict[str, Any]) -> Optional[str]:
    meta = d.get("_meta")
    if isinstance(meta, dict):
        name = meta.get("name")
        if isinstance(name, str) and "subsystem" in name:
            try:
                inner = json.loads(name)
                if isinstance(inner, dict) and isinstance(inner.get("subsystem"), str):
                    return inner["subsystem"]
            except Exception:
                pass
    v0 = d.get("0")
    if isinstance(v0, str) and v0.strip().startswith("{") and "subsystem" in v0:
        try:
            inner = json.loads(v0)
            if isinstance(inner, dict) and isinstance(inner.get("subsystem"), str):
                return inner["subsystem"]
        except Exception:
            return None
    return None


def _extract_level(d: Dict[str, Any]) -> Optional[str]:
    meta = d.get("_meta")
    if isinstance(meta, dict):
        lvl = meta.get("logLevelName")
        if isinstance(lvl, str):
            return lvl
    return None


def _extract_ts(d: Dict[str, Any]) -> Optional[datetime]:
    if "time" in d:
        return _parse_iso(d.get("time"))
    meta = d.get("_meta")
    if isinstance(meta, dict):
        return _parse_iso(meta.get("date"))
    return None


class GatewayLogTailer:
    def __init__(self, openclaw_bin: str, ring_lines: int = 5000) -> None:
        self._openclaw_bin = openclaw_bin
        self._cursor: Optional[int] = None
        self._ring_lines = ring_lines
        self._lines: List[GatewayLogLine] = []
        self._available = True
        self._last_error: Optional[str] = None
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        with self._lock:
            return self._available

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    @property
    def lines(self) -> List[GatewayLogLine]:
        with self._lock:
            return list(self._lines)

    @property
    def line_count(self) -> int:
        with self._lock:
            return len(self._lines)

    def poll(self, limit: int = 200) -> None:
        with self._lock:
            if not self._available:
                return
            cursor = self._cursor

        params: Dict[str, Any] = {"limit": limit, "maxBytes": 1_000_000}
        if cursor is not None:
            params["cursor"] = cursor

        res = gateway_call(self._openclaw_bin, "logs.tail", params=params, timeout_ms=10000)
        if not res.ok or not res.data:
            with self._lock:
                self._available = False
                self._last_error = f"logs.tail unavailable (rc={res.returncode})"
            return
        data = res.data
        cursor = data.get("cursor")
        lines = data.get("lines")
        if not isinstance(lines, list):
            return
        parsed_lines: List[GatewayLogLine] = []
        for raw_line in lines:
            if not isinstance(raw_line, str):
                continue
            parsed: Optional[Dict[str, Any]] = None
            try:
                parsed = json.loads(raw_line)
            except Exception:
                parsed = None
            if isinstance(parsed, dict):
                msg = _extract_message(parsed)
                entry = GatewayLogLine(
                    ts=_extract_ts(parsed),
                    subsystem=_extract_subsystem(parsed),
                    level=_extract_level(parsed),
                    text=redact_text(msg) or redact_text(raw_line),
                    raw=redact_text(raw_line),
                )
            else:
                entry = GatewayLogLine(ts=None, subsystem=None, level=None, text=redact_text(raw_line), raw=redact_text(raw_line))
            parsed_lines.append(entry)

        with self._lock:
            if isinstance(cursor, int):
                self._cursor = cursor
            self._lines.extend(parsed_lines)
            if len(self._lines) > self._ring_lines:
                self._lines = self._lines[-self._ring_lines :]
