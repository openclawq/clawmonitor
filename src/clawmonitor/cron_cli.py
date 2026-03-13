from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .openclaw_cron import CronJob, read_cron_last_runs, read_cron_snapshot


def _dt_from_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _age_seconds(dt: Optional[datetime]) -> Optional[int]:
    if not dt:
        return None
    now = datetime.now(timezone.utc)
    try:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int((now - dt.astimezone(timezone.utc)).total_seconds())
    except Exception:
        return None


def _fmt_age(age: Optional[int]) -> str:
    if age is None:
        return "-"
    if age < 60:
        return f"{age}s"
    if age < 3600:
        return f"{age//60}m"
    return f"{age//3600}h"


@dataclass(frozen=True)
class CronRow:
    job_id: str
    name: str
    agent_id: str
    enabled: str
    schedule: str
    last_run_at: str
    last_run_age: str
    last_status: str


def _job_schedule(job: CronJob) -> str:
    if job.schedule_kind == "cron" and job.schedule_expr:
        tz = f" {job.schedule_tz}" if job.schedule_tz else ""
        return f"cron {job.schedule_expr}{tz}".strip()
    if job.schedule_kind and job.schedule_expr:
        return f"{job.schedule_kind} {job.schedule_expr}".strip()
    return job.schedule_kind or "-"


def collect_cron(openclaw_root: Path) -> List[CronRow]:
    snap = read_cron_snapshot(openclaw_root)
    runs = read_cron_last_runs(openclaw_root)

    rows: List[CronRow] = []
    for job_id, job in sorted(snap.jobs_by_id.items(), key=lambda kv: (kv[1].agent_id or "", kv[1].name or "", kv[0])):
        run = runs.get(job_id)
        dt = _dt_from_ms(run.ts_ms) if run else None
        rows.append(
            CronRow(
                job_id=job_id,
                name=job.name or "-",
                agent_id=job.agent_id or "main",
                enabled="-" if job.enabled is None else ("Y" if job.enabled else "N"),
                schedule=_job_schedule(job),
                last_run_at=_fmt_dt(dt),
                last_run_age=_fmt_age(_age_seconds(dt)),
                last_status=(run.status if run and run.status else "-"),
            )
        )
    return rows


def format_table(rows: List[CronRow]) -> str:
    def fit(text: str, width: int) -> str:
        s = text or "-"
        if len(s) <= width:
            return s.ljust(width)
        if width <= 1:
            return s[:width]
        return (s[: width - 1] + "…")[:width]

    job_w = max(8, min(12, max((len(r.job_id) for r in rows), default=8)))
    agent_w = max(8, min(20, max((len(r.agent_id) for r in rows), default=8)))
    header = f"{fit('JOB', job_w)}  {fit('AGENT', agent_w)}  EN  {fit('SCHEDULE', 22)}  LAST     STAT  NAME"
    lines = [header]
    for r in rows:
        lines.append(
            f"{fit(r.job_id, job_w)}  {fit(r.agent_id, agent_w)}  {fit(r.enabled, 2)}  "
            f"{fit(r.schedule, 22)}  {fit(r.last_run_age, 4)}  {fit(r.last_status, 4)}  {r.name}"
        )
    return "\n".join(lines) + "\n"


def format_markdown(rows: List[CronRow]) -> str:
    header = ["jobId", "agentId", "enabled", "schedule", "lastRunAt", "lastRunAge", "lastStatus", "name"]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    def esc(v: str) -> str:
        return (v or "-").replace("|", "\\|").replace("\n", " ")

    for r in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    esc(r.job_id),
                    esc(r.agent_id),
                    esc(r.enabled),
                    esc(r.schedule),
                    esc(r.last_run_at),
                    esc(r.last_run_age),
                    esc(r.last_status),
                    esc(r.name),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def format_json(rows: List[CronRow], openclaw_root: Path) -> str:
    doc: Dict[str, Any] = {
        "openclaw_root": str(openclaw_root),
        "ts": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "rows": [
            {
                "jobId": r.job_id,
                "agentId": r.agent_id,
                "enabled": r.enabled,
                "schedule": r.schedule,
                "lastRunAt": r.last_run_at,
                "lastRunAge": r.last_run_age,
                "lastStatus": r.last_status,
                "name": r.name,
            }
            for r in rows
        ],
    }
    import json

    return json.dumps(doc, ensure_ascii=False, indent=2)
