from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import subprocess
from typing import Callable, Dict, List, Optional, Sequence, Tuple


SYSTEM_UNIT_NAME = "openclaw-gateway.service"
HELPER_FAMILIES = {"chrome/playwright", "ssh-agent", "qmd", "node", "other"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_int(value: object) -> Optional[int]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("[not set]", "n/a", "unknown"):
        return None
    try:
        return int(text)
    except Exception:
        return None


def _parse_float(value: object) -> Optional[float]:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text in ("[not set]", "n/a", "unknown"):
        return None
    try:
        return float(text)
    except Exception:
        return None


def _risk_rank(risk: str) -> int:
    return {"ok": 0, "warn": 1, "alert": 2}.get((risk or "").strip(), 0)


def _short_bytes_from_kib(kib: int) -> str:
    value = max(0, int(kib)) * 1024
    if value < 1024:
        return f"{value}B"
    if value < 1024 * 1024:
        return f"{value / 1024:.0f}K"
    if value < 1024 * 1024 * 1024:
        return f"{value / (1024 * 1024):.1f}M".replace(".0M", "M")
    return f"{value / (1024 * 1024 * 1024):.1f}G".replace(".0G", "G")


@dataclass(frozen=True)
class SystemServiceSnapshot:
    unit_name: str
    main_pid: Optional[int]
    active_state: str
    sub_state: str
    tasks_current: Optional[int]
    memory_current_bytes: Optional[int]
    cpu_usage_nsec: Optional[int]
    kill_mode: str
    control_group: Optional[str]


@dataclass(frozen=True)
class SystemProcessSnapshot:
    pid: int
    ppid: int
    pgid: int
    stat: str
    cpu_pct: float
    mem_pct: float
    rss_kib: int
    elapsed_seconds: int
    comm: str
    args: str
    cgroup_path: Optional[str]
    in_service_cgroup: bool
    family: str
    relation: str
    risk: str
    is_zombie: bool
    is_orphan: bool
    is_main: bool
    potentially_problematic: bool
    reclaimable_kib: int


@dataclass(frozen=True)
class SystemFamilySummary:
    family: str
    risk: str
    count: int
    live_count: int
    zombie_count: int
    orphan_count: int
    rss_kib: int
    cpu_pct: float
    reclaimable_kib: int
    notes: Tuple[str, ...]


@dataclass(frozen=True)
class SystemSnapshot:
    service: SystemServiceSnapshot
    sampled_at: datetime
    service_risk: str
    processes: Tuple[SystemProcessSnapshot, ...]
    families: Tuple[SystemFamilySummary, ...]
    cgroup_process_count: int
    helper_process_count: int
    zombie_count: int
    orphan_count: int
    problematic_count: int
    reclaimable_kib: int
    issues: Tuple[str, ...]


def _run_text(args: Sequence[str], *, timeout: int = 6) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(err or f"command failed: {' '.join(args)}")
    return proc.stdout


def _parse_systemctl_show(text: str, *, unit_name: str = SYSTEM_UNIT_NAME) -> SystemServiceSnapshot:
    values: Dict[str, str] = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return SystemServiceSnapshot(
        unit_name=values.get("Id") or unit_name,
        main_pid=_parse_int(values.get("MainPID")),
        active_state=values.get("ActiveState") or "-",
        sub_state=values.get("SubState") or "-",
        tasks_current=_parse_int(values.get("TasksCurrent")),
        memory_current_bytes=_parse_int(values.get("MemoryCurrent")),
        cpu_usage_nsec=_parse_int(values.get("CPUUsageNSec")),
        kill_mode=values.get("KillMode") or "-",
        control_group=values.get("ControlGroup") or None,
    )


def _parse_ps_output(text: str) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line:
            continue
        parts = line.split(None, 9)
        if len(parts) < 9:
            continue
        pid, ppid, pgid, stat, cpu, mem, rss, etimes, comm = parts[:9]
        args = parts[9] if len(parts) >= 10 else ""
        rows.append(
            {
                "pid": _parse_int(pid) or 0,
                "ppid": _parse_int(ppid) or 0,
                "pgid": _parse_int(pgid) or 0,
                "stat": stat,
                "cpu_pct": _parse_float(cpu) or 0.0,
                "mem_pct": _parse_float(mem) or 0.0,
                "rss_kib": _parse_int(rss) or 0,
                "elapsed_seconds": _parse_int(etimes) or 0,
                "comm": comm,
                "args": args,
            }
        )
    return rows


def _read_proc_cgroup(pid: int, *, proc_root: Path = Path("/proc")) -> Optional[str]:
    path = proc_root / str(pid) / "cgroup"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None
    paths: List[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = line.split(":", 2)
        if len(parts) != 3:
            continue
        cg = parts[2].strip()
        if cg:
            paths.append(cg)
    if not paths:
        return None
    for item in paths:
        if item.endswith(".service"):
            return item
    return paths[-1]


def _in_control_group(control_group: Optional[str], cgroup_path: Optional[str]) -> bool:
    if not control_group or not cgroup_path:
        return False
    if cgroup_path == control_group:
        return True
    return cgroup_path.startswith(control_group.rstrip("/") + "/")


def _classify_family(*, pid: int, main_pid: Optional[int], comm: str, args: str) -> str:
    comm_l = (comm or "").lower()
    args_l = (args or "").lower()
    if main_pid and pid == main_pid:
        return "openclaw-gateway"
    if "playwright" in args_l or comm_l.startswith("chrome") or comm_l.startswith("chromium"):
        return "chrome/playwright"
    if comm_l == "ssh-agent":
        return "ssh-agent"
    if comm_l == "qmd" or "/qmd" in args_l:
        return "qmd"
    if comm_l == "node":
        if "openclaw" in args_l:
            return "openclaw-gateway"
        return "node"
    if "openclaw" in args_l:
        return "openclaw-gateway"
    return "other"


def _classify_process_risk(
    *,
    service: SystemServiceSnapshot,
    family: str,
    is_main: bool,
    is_zombie: bool,
    is_orphan: bool,
    in_service_cgroup: bool,
    rss_kib: int,
) -> Tuple[str, bool, int]:
    if is_main:
        if (service.active_state or "").lower() != "active":
            return "alert", False, 0
        return "ok", False, 0
    if is_zombie:
        return "alert", True, 0
    if is_orphan and in_service_cgroup:
        return "alert", True, max(0, rss_kib)
    if family == "chrome/playwright" and rss_kib >= 128 * 1024:
        return "warn", True, max(0, rss_kib)
    if family == "qmd" and rss_kib >= 64 * 1024:
        return "warn", True, max(0, rss_kib)
    if family == "node" and rss_kib >= 128 * 1024:
        return "warn", True, max(0, rss_kib)
    if family == "ssh-agent" and rss_kib > 0:
        return "warn", True, max(0, rss_kib)
    if family == "other" and rss_kib >= 256 * 1024:
        return "warn", True, max(0, rss_kib)
    return "ok", False, 0


def _family_notes(
    family: str,
    *,
    processes: Sequence[SystemProcessSnapshot],
    service: SystemServiceSnapshot,
) -> Tuple[str, ...]:
    notes: List[str] = []
    zombies = sum(1 for proc in processes if proc.is_zombie)
    orphans = sum(1 for proc in processes if proc.is_orphan and proc.in_service_cgroup and not proc.is_main)
    reclaim_kib = sum(proc.reclaimable_kib for proc in processes)
    if family == "openclaw-gateway" and (service.active_state or "").lower() != "active":
        notes.append(f"service={service.active_state}/{service.sub_state}")
    if orphans:
        notes.append(f"orphans={orphans}")
    if zombies:
        notes.append(f"zombies={zombies}")
    if reclaim_kib:
        notes.append(f"reclaim~{_short_bytes_from_kib(reclaim_kib)}")
    return tuple(notes)


def _service_issues(
    *,
    service: SystemServiceSnapshot,
    orphan_count: int,
    zombie_count: int,
    helper_count: int,
    reclaimable_kib: int,
) -> Tuple[str, Tuple[str, ...]]:
    issues: List[str] = []
    risk = "ok"
    active_ok = (service.active_state or "").lower() == "active" and (service.sub_state or "").lower() == "running"
    if not active_ok:
        issues.append(f"service={service.active_state}/{service.sub_state}")
        risk = "alert"
    if (service.kill_mode or "").strip() != "control-group":
        issues.append(f"KillMode={service.kill_mode or '-'}")
        if risk != "alert":
            risk = "warn"
    if orphan_count > 0:
        issues.append(f"orphan helpers={orphan_count}")
        risk = "alert"
    if zombie_count > 0:
        issues.append(f"zombies={zombie_count}")
        if risk == "ok":
            risk = "warn"
    if reclaimable_kib >= 1024 * 1024:
        issues.append(f"reclaim~{_short_bytes_from_kib(reclaimable_kib)}")
        risk = "alert"
    elif reclaimable_kib >= 256 * 1024 and risk == "ok":
        issues.append(f"reclaim~{_short_bytes_from_kib(reclaimable_kib)}")
        risk = "warn"
    if helper_count >= 12 and risk == "ok":
        issues.append(f"helpers={helper_count}")
        risk = "warn"
    return risk, tuple(issues)


def _build_snapshot(
    *,
    service: SystemServiceSnapshot,
    ps_rows: Sequence[Dict[str, object]],
    proc_root: Path = Path("/proc"),
) -> SystemSnapshot:
    processes: List[SystemProcessSnapshot] = []
    for row in ps_rows:
        pid = int(row.get("pid") or 0)
        if pid <= 0:
            continue
        cgroup_path = _read_proc_cgroup(pid, proc_root=proc_root)
        in_service_cgroup = _in_control_group(service.control_group, cgroup_path)
        is_main = service.main_pid is not None and pid == service.main_pid
        if not is_main and not in_service_cgroup:
            continue
        ppid = int(row.get("ppid") or 0)
        family = _classify_family(pid=pid, main_pid=service.main_pid, comm=str(row.get("comm") or ""), args=str(row.get("args") or ""))
        is_zombie = "Z" in str(row.get("stat") or "")
        is_orphan = ppid == 1
        relation = "main" if is_main else ("orphan" if is_orphan else "service-child")
        risk, problematic, reclaimable_kib = _classify_process_risk(
            service=service,
            family=family,
            is_main=is_main,
            is_zombie=is_zombie,
            is_orphan=is_orphan,
            in_service_cgroup=in_service_cgroup,
            rss_kib=int(row.get("rss_kib") or 0),
        )
        processes.append(
            SystemProcessSnapshot(
                pid=pid,
                ppid=ppid,
                pgid=int(row.get("pgid") or 0),
                stat=str(row.get("stat") or ""),
                cpu_pct=float(row.get("cpu_pct") or 0.0),
                mem_pct=float(row.get("mem_pct") or 0.0),
                rss_kib=int(row.get("rss_kib") or 0),
                elapsed_seconds=int(row.get("elapsed_seconds") or 0),
                comm=str(row.get("comm") or ""),
                args=str(row.get("args") or ""),
                cgroup_path=cgroup_path,
                in_service_cgroup=in_service_cgroup,
                family=family,
                relation=relation,
                risk=risk,
                is_zombie=is_zombie,
                is_orphan=is_orphan,
                is_main=is_main,
                potentially_problematic=problematic,
                reclaimable_kib=reclaimable_kib,
            )
        )

    processes.sort(
        key=lambda proc: (
            -_risk_rank(proc.risk),
            -int(proc.potentially_problematic),
            -proc.reclaimable_kib,
            -proc.rss_kib,
            -proc.cpu_pct,
            proc.pid,
        )
    )

    family_map: Dict[str, List[SystemProcessSnapshot]] = {}
    for proc in processes:
        family_map.setdefault(proc.family, []).append(proc)
    families: List[SystemFamilySummary] = []
    for family, family_processes in family_map.items():
        risk = "ok"
        for proc in family_processes:
            if _risk_rank(proc.risk) > _risk_rank(risk):
                risk = proc.risk
        families.append(
            SystemFamilySummary(
                family=family,
                risk=risk,
                count=len(family_processes),
                live_count=sum(1 for proc in family_processes if not proc.is_zombie),
                zombie_count=sum(1 for proc in family_processes if proc.is_zombie),
                orphan_count=sum(1 for proc in family_processes if proc.is_orphan and not proc.is_main),
                rss_kib=sum(proc.rss_kib for proc in family_processes if not proc.is_zombie),
                cpu_pct=sum(proc.cpu_pct for proc in family_processes if not proc.is_zombie),
                reclaimable_kib=sum(proc.reclaimable_kib for proc in family_processes),
                notes=_family_notes(family, processes=family_processes, service=service),
            )
        )
    families.sort(key=lambda row: (-_risk_rank(row.risk), -row.reclaimable_kib, -row.rss_kib, row.family))

    orphan_count = sum(1 for proc in processes if proc.is_orphan and proc.in_service_cgroup and not proc.is_main)
    zombie_count = sum(1 for proc in processes if proc.is_zombie)
    helper_count = sum(1 for proc in processes if not proc.is_main and not proc.is_zombie)
    problematic_count = sum(1 for proc in processes if proc.potentially_problematic)
    reclaimable_kib = sum(proc.reclaimable_kib for proc in processes)
    service_risk, issues = _service_issues(
        service=service,
        orphan_count=orphan_count,
        zombie_count=zombie_count,
        helper_count=helper_count,
        reclaimable_kib=reclaimable_kib,
    )
    return SystemSnapshot(
        service=service,
        sampled_at=_utc_now(),
        service_risk=service_risk,
        processes=tuple(processes),
        families=tuple(families),
        cgroup_process_count=len(processes),
        helper_process_count=helper_count,
        zombie_count=zombie_count,
        orphan_count=orphan_count,
        problematic_count=problematic_count,
        reclaimable_kib=reclaimable_kib,
        issues=issues,
    )


def collect_system_snapshot(
    *,
    unit_name: str = SYSTEM_UNIT_NAME,
    command_runner: Optional[Callable[[Sequence[str]], str]] = None,
    proc_root: Path = Path("/proc"),
) -> SystemSnapshot:
    runner = command_runner or _run_text
    service = _parse_systemctl_show(
        runner(
            [
                "systemctl",
                "--user",
                "show",
                unit_name,
                "-p",
                "Id",
                "-p",
                "MainPID",
                "-p",
                "ActiveState",
                "-p",
                "SubState",
                "-p",
                "TasksCurrent",
                "-p",
                "MemoryCurrent",
                "-p",
                "CPUUsageNSec",
                "-p",
                "KillMode",
                "-p",
                "ControlGroup",
            ]
        ),
        unit_name=unit_name,
    )
    ps_rows = _parse_ps_output(
        runner(
            [
                "ps",
                "-eo",
                "pid=,ppid=,pgid=,stat=,%cpu=,%mem=,rss=,etimes=,comm=,args=",
            ]
        )
    )
    return _build_snapshot(service=service, ps_rows=ps_rows, proc_root=proc_root)
