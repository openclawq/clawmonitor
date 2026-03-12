from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

from .openclaw_cli import gateway_call


@dataclass(frozen=True)
class ChannelsSnapshot:
    ts: Optional[int]
    raw: Dict[str, Any]


def fetch_channels_status(openclaw_bin: str, probe: bool = False, timeout_ms: int = 10000) -> Optional[ChannelsSnapshot]:
    params: Dict[str, Any] = {}
    if probe:
        params["probe"] = True
        params["timeoutMs"] = timeout_ms
    res = gateway_call(openclaw_bin, "channels.status", params=params, timeout_ms=timeout_ms)
    if not res.ok or not res.data:
        return None
    data = res.data
    ts = data.get("ts")
    return ChannelsSnapshot(ts=int(ts) if isinstance(ts, int) else None, raw=data)

