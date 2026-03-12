from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class OpenClawResult:
    ok: bool
    data: Optional[Dict[str, Any]]
    raw_stdout: str
    raw_stderr: str
    returncode: int


def _extract_json(stdout: str) -> Optional[Dict[str, Any]]:
    if not stdout:
        return None
    idx_obj = stdout.find("{")
    idx_arr = stdout.find("[")
    idxs = [i for i in [idx_obj, idx_arr] if i != -1]
    if not idxs:
        return None
    idx = min(idxs)
    chunk = stdout[idx:].strip()
    try:
        return json.loads(chunk)
    except Exception:
        return None


def gateway_call(
    openclaw_bin: str,
    method: str,
    params: Optional[Dict[str, Any]] = None,
    timeout_ms: int = 10000,
) -> OpenClawResult:
    args = [
        openclaw_bin,
        "gateway",
        "call",
        method,
        "--json",
        "--timeout",
        str(timeout_ms),
        "--params",
        json.dumps(params or {}, ensure_ascii=False),
    ]
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    data = _extract_json(p.stdout)
    ok = p.returncode == 0 and data is not None
    return OpenClawResult(ok=ok, data=data, raw_stdout=p.stdout, raw_stderr=p.stderr, returncode=p.returncode)

