from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class PushResult:
    ok: bool
    returncode: int
    stdout: str
    stderr: str
    error: Optional[str] = None


def _strip_prefix(target: str) -> str:
    # openclaw message send accepts either "telegram:123" or "123", and for feishu
    # it will normalize "user:ou_xxx" -> "ou_xxx" and "chat:oc_xxx" -> "oc_xxx".
    t = (target or "").strip()
    if ":" in t:
        prefix, rest = t.split(":", 1)
        if prefix in ("telegram", "user", "chat"):
            return rest.strip()
    return t


def push_message(
    *,
    openclaw_bin: str,
    channel: str,
    account_id: Optional[str],
    target: str,
    message: str,
    dry_run: bool = False,
    silent: bool = False,
) -> PushResult:
    args = [
        openclaw_bin,
        "--log-level",
        "silent",
        "message",
        "send",
        "--channel",
        channel,
        "--target",
        _strip_prefix(target),
        "--message",
        message,
    ]
    if account_id:
        args += ["--account", account_id]
    if dry_run:
        args.append("--dry-run")
    if silent:
        args.append("--silent")
    args.append("--json")
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ok = p.returncode == 0
    return PushResult(ok=ok, returncode=p.returncode, stdout=p.stdout, stderr=p.stderr, error=None if ok else "message.send failed")

