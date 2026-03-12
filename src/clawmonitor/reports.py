from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import state_dir
from .diagnostics import Finding
from .gateway_logs import GatewayLogLine
from .redact import redact_lines, redact_text


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


@dataclass(frozen=True)
class Report:
    session_key: str
    created_at: str
    summary: Dict[str, Any]
    findings: List[Dict[str, Any]]
    related_logs: List[str]


def write_report(
    session_key: str,
    summary: Dict[str, Any],
    findings: List[Finding],
    related_logs: List[GatewayLogLine],
    max_log_lines: int,
) -> Path:
    base = state_dir() / "reports"
    base.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    safe_logs = [ln.text or ln.raw for ln in related_logs][-max_log_lines:]
    safe_logs = redact_lines(safe_logs)
    rep = Report(
        session_key=session_key,
        created_at=datetime.now(timezone.utc).isoformat(),
        summary={k: (redact_text(v) if isinstance(v, str) else v) for k, v in summary.items()},
        findings=[asdict(f) for f in findings],
        related_logs=safe_logs,
    )
    out_path = base / f"clawmonitor_report_{stamp}.json"
    out_path.write_text(json.dumps(asdict(rep), ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path

