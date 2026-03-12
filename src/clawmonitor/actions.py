from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .openclaw_cli import gateway_call


TEMPLATES: Dict[str, str] = {
    "progress": "请用不超过8行汇报：当前进度、下一步、预估完成时间；已完成请写 DONE；受阻请写 BLOCKED: 原因。",
    "status": "只回复一个词：WORKING / DONE / BLOCKED，并补充1行原因或下一步。",
    "last_action": "请列出你最近一次工具/命令执行的名称与结果摘要（最多5行）。",
}


@dataclass(frozen=True)
class NudgeResult:
    ok: bool
    run_id: Optional[str]
    status: Optional[str]
    error: Optional[str]


def send_nudge(openclaw_bin: str, session_key: str, template_id: str, deliver: bool = True) -> NudgeResult:
    msg = TEMPLATES.get(template_id)
    if not msg:
        return NudgeResult(ok=False, run_id=None, status=None, error=f"unknown template: {template_id}")
    params: Dict[str, Any] = {
        "sessionKey": session_key,
        "message": msg,
        "deliver": bool(deliver),
        "timeoutMs": 0,
        "idempotencyKey": str(uuid.uuid4()),
    }
    res = gateway_call(openclaw_bin, "chat.send", params=params, timeout_ms=10000)
    if not res.ok or not res.data:
        return NudgeResult(ok=False, run_id=None, status=None, error=f"chat.send failed (rc={res.returncode})")
    run_id = res.data.get("runId") if isinstance(res.data.get("runId"), str) else None
    status = res.data.get("status") if isinstance(res.data.get("status"), str) else None
    return NudgeResult(ok=True, run_id=run_id, status=status, error=None)

