from __future__ import annotations

from pathlib import Path

from clawmonitor.system_monitor import _parse_ps_output, collect_system_snapshot


def _write_cgroup(proc_root: Path, pid: int, cgroup_path: str) -> None:
    path = proc_root / str(pid)
    path.mkdir(parents=True, exist_ok=True)
    (path / "cgroup").write_text(f"0::{cgroup_path}\n", encoding="utf-8")


def test_parse_ps_output_keeps_args_column() -> None:
    rows = _parse_ps_output("100 1 100 Sl 12.5 1.2 123456 90 node node /srv/openclaw/server.js --flag value\n")
    assert len(rows) == 1
    row = rows[0]
    assert row["pid"] == 100
    assert row["comm"] == "node"
    assert row["args"] == "node /srv/openclaw/server.js --flag value"


def test_collect_system_snapshot_filters_to_service_cgroup_and_flags_risk(tmp_path: Path) -> None:
    proc_root = tmp_path / "proc"
    service_cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice/openclaw-gateway.service"
    other_cgroup = "/user.slice/user-1000.slice/user@1000.service/app.slice/other.service"

    _write_cgroup(proc_root, 100, service_cgroup)
    _write_cgroup(proc_root, 101, service_cgroup)
    _write_cgroup(proc_root, 102, service_cgroup)
    _write_cgroup(proc_root, 103, service_cgroup)
    _write_cgroup(proc_root, 200, other_cgroup)

    systemctl_text = "\n".join(
        [
            "Id=openclaw-gateway.service",
            "MainPID=100",
            "ActiveState=active",
            "SubState=running",
            "TasksCurrent=12",
            "MemoryCurrent=1073741824",
            "CPUUsageNSec=9000000000",
            "KillMode=process",
            f"ControlGroup={service_cgroup}",
        ]
    )
    ps_text = "\n".join(
        [
            "100 1 100 Sl 4.0 1.0 200000 300 node node /srv/openclaw/server.js",
            "101 100 101 Sl 33.0 3.0 350000 250 chrome chrome --type=renderer --enable-automation",
            "102 1 102 Sl 0.0 0.0 4000 120 ssh-agent ssh-agent -s",
            "103 1 103 Z 0.0 0.0 0 600 chrome chrome_crashpad_handler --monitor-self",
            "200 1 200 Sl 0.0 0.0 9000 999 ssh-agent ssh-agent -s",
        ]
    )

    def runner(args: list[str]) -> str:
        if args and args[0] == "systemctl":
            return systemctl_text
        if args and args[0] == "ps":
            return ps_text
        raise AssertionError(args)

    snapshot = collect_system_snapshot(command_runner=runner, proc_root=proc_root)

    assert snapshot.service.main_pid == 100
    assert snapshot.cgroup_process_count == 4
    assert {proc.pid for proc in snapshot.processes} == {100, 101, 102, 103}
    assert snapshot.service_risk == "alert"
    assert snapshot.orphan_count == 2
    assert snapshot.zombie_count == 1
    assert snapshot.problematic_count == 3
    assert snapshot.reclaimable_kib == 354000
    assert any(issue == "KillMode=process" for issue in snapshot.issues)
    assert any(issue == "orphan helpers=2" for issue in snapshot.issues)

    family_map = {row.family: row for row in snapshot.families}
    assert family_map["chrome/playwright"].risk == "alert"
    assert family_map["chrome/playwright"].zombie_count == 1
    assert family_map["chrome/playwright"].reclaimable_kib == 350000
    assert family_map["ssh-agent"].orphan_count == 1
    assert family_map["ssh-agent"].reclaimable_kib == 4000
