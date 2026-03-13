from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


@dataclass(frozen=True)
class CronJob:
    id: str
    name: Optional[str]
    agent_id: Optional[str]
    enabled: Optional[bool]
    schedule_kind: Optional[str]
    schedule_expr: Optional[str]
    schedule_tz: Optional[str]


@dataclass(frozen=True)
class CronSnapshot:
    jobs_by_id: Dict[str, CronJob]
    jobs_by_prefix: Dict[str, CronJob]


@dataclass(frozen=True)
class CronRunStatus:
    job_id: str
    ts_ms: Optional[int]
    status: Optional[str]


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def read_cron_snapshot(openclaw_root: Path) -> CronSnapshot:
    """
    Read cron job metadata from ~/.openclaw/cron/jobs.json.

    Cron sessions typically use session keys like:
      - agent:<agentId>:cron:<jobId>
      - agent:<agentId>:cron:<jobId>:run:<uuid>
    """
    path = openclaw_root / "cron" / "jobs.json"
    doc = _safe_load_json(path) or {}
    jobs = doc.get("jobs") if isinstance(doc, dict) else None

    by_id: Dict[str, CronJob] = {}
    by_prefix: Dict[str, CronJob] = {}
    if isinstance(jobs, list):
        for ent in jobs:
            if not isinstance(ent, dict):
                continue
            jid = ent.get("id")
            if not isinstance(jid, str) or not jid.strip():
                continue
            jid = jid.strip()
            name = ent.get("name")
            agent_id = ent.get("agentId")
            enabled = ent.get("enabled")
            schedule = ent.get("schedule") if isinstance(ent.get("schedule"), dict) else {}
            kind = schedule.get("kind")
            expr = schedule.get("expr")
            tz = schedule.get("tz")
            job = CronJob(
                id=jid,
                name=name.strip() if isinstance(name, str) and name.strip() else None,
                agent_id=agent_id.strip() if isinstance(agent_id, str) and agent_id.strip() else None,
                enabled=bool(enabled) if isinstance(enabled, bool) else None,
                schedule_kind=str(kind) if isinstance(kind, str) and kind else None,
                schedule_expr=str(expr) if isinstance(expr, str) and expr else None,
                schedule_tz=str(tz) if isinstance(tz, str) and tz else None,
            )
            by_id[jid] = job
            # Convenience: allow matching by UUID-ish prefix (8 chars) like
            # "b2a7c9d1" from "87747b96-e09f-..."
            if len(jid) >= 8:
                pref = jid[:8]
                # Only store if unambiguous; otherwise keep first and let callers
                # fall back to exact id matching.
                by_prefix.setdefault(pref, job)
            by_prefix.setdefault(jid, job)

    return CronSnapshot(jobs_by_id=by_id, jobs_by_prefix=by_prefix)


def _tail_last_jsonl_obj(path: Path, *, max_bytes: int = 8192) -> Optional[Dict[str, Any]]:
    try:
        data = path.read_bytes()
    except Exception:
        return None
    if not data:
        return None
    if len(data) > max_bytes:
        data = data[-max_bytes:]
        # Drop partial first line if we cut mid-line.
        nl = data.find(b"\n")
        if nl != -1:
            data = data[nl + 1 :]
    try:
        text = data.decode("utf-8", errors="ignore")
    except Exception:
        return None
    for line in reversed([ln.strip() for ln in text.splitlines()]):
        if not line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def read_cron_last_runs(openclaw_root: Path) -> Dict[str, CronRunStatus]:
    """
    Read last run status for each job from ~/.openclaw/cron/runs/<jobId>.jsonl
    (best-effort).
    """
    runs_dir = openclaw_root / "cron" / "runs"
    out: Dict[str, CronRunStatus] = {}
    if not runs_dir.exists():
        return out
    for path in sorted(runs_dir.glob("*.jsonl")):
        job_id = path.stem
        obj = _tail_last_jsonl_obj(path)
        ts = obj.get("ts") if isinstance(obj, dict) else None
        status = obj.get("status") if isinstance(obj, dict) else None
        ts_ms: Optional[int] = None
        if isinstance(ts, int):
            ts_ms = ts
        elif isinstance(ts, float):
            ts_ms = int(ts)
        out[job_id] = CronRunStatus(
            job_id=job_id,
            ts_ms=ts_ms,
            status=str(status) if isinstance(status, str) and status else None,
        )
    return out


def cron_job_id_from_session_key(session_key: str) -> Optional[str]:
    key = (session_key or "").strip()
    if not key:
        return None
    if key.startswith("agent:"):
        parts = key.split(":")
        # agent:<aid>:cron:<jobId>[:run:<uuid>]
        if len(parts) >= 4 and parts[2] == "cron":
            jid = parts[3].strip()
            return jid or None
        return None
    if key.startswith("cron:"):
        # Legacy/wake-reason style: cron:<jobId>
        rest = key.split("cron:", 1)[1]
        jid = rest.split(":", 1)[0].strip()
        return jid or None
    return None


def match_cron_job(snap: Optional[CronSnapshot], session_key: str) -> Optional[CronJob]:
    if not snap:
        return None
    jid = cron_job_id_from_session_key(session_key)
    if not jid:
        return None
    # Exact id first, then prefix.
    return snap.jobs_by_id.get(jid) or snap.jobs_by_prefix.get(jid)
