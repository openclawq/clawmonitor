from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .openclaw_cli import gateway_call


TEMPLATES: Dict[str, str] = {
    # English (default)
    "progress": "[ClawMonitor nudge] Reply in <=8 lines: progress, next step, ETA. If finished, write DONE. If blocked, write BLOCKED: <reason>.",
    "status": "[ClawMonitor nudge] Reply with one word: WORKING / DONE / BLOCKED, plus 1 short line with reason or next step.",
    "last_action": "[ClawMonitor nudge] List your most recent tool/command execution(s) with a short result summary (<=5 lines).",
    "continue": "[ClawMonitor nudge] Continue the task you were working on. If a long task is running, do not interrupt it; report DONE when finished, and every 10 minutes report progress in <=6 lines.",
    "finalize": "[ClawMonitor nudge] If you already completed the user task: output final summary (<=10 lines) + deliverables (paths/links) + one line DONE. If not done: in <=6 lines report progress/next/ETA and end with WORKING.",
    # Chinese (optional)
    "progress_zh": "【ClawMonitor 提示】请用不超过8行汇报：当前进度、下一步、预估完成时间；已完成请写 DONE；受阻请写 BLOCKED: 原因。",
    "status_zh": "【ClawMonitor 提示】只回复一个词：WORKING / DONE / BLOCKED，并补充1行原因或下一步。",
    "last_action_zh": "【ClawMonitor 提示】请列出你最近一次工具/命令执行的名称与结果摘要（最多5行）。",
    "continue_zh": "【ClawMonitor 提示】继续完成你正在做/上次未完成的任务；如果正在运行长任务也不要中断，完成后汇报 DONE，并在过程中每隔10分钟用不超过6行汇报一次进度。",
    "finalize_zh": "【ClawMonitor 提示】如果你已经完成了用户交代的任务：请输出最终总结（<=10行）+ 交付物/结果位置（路径/链接）+ 用一行写 DONE。若尚未完成：用 <=6 行汇报当前进度、下一步、预估完成时间，并用 WORKING 结尾。",
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
