from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from .actions import TEMPLATES, send_nudge
from .config import load_config
from .delivery_queue import load_failed_delivery_map
from .diagnostics import diagnose, related_logs
from .eventlog import EventLog
from .gateway_logs import GatewayLogTailer
from .init_wizard import maybe_run_first_time_init, run_init
from .locks import lock_path_for_session_file, read_lock
from .model_monitor import ModelProbeOptions, collect_model_rows, format_model_json, format_model_markdown, format_model_table
from .openclaw_config import read_openclaw_config_snapshot
from .push_notify import push_message
from .redact import redact_text
from .reports import write_report_files
from .session_store import list_sessions
from .session_tail import tail_for_meta
from .acpx_sessions import acpx_is_working
from .state import WorkingSignal, compute_state
from .status_cli import collect_status, format_json as format_status_json, format_markdown as format_status_markdown, format_table, watch_loop
from .cron_cli import collect_cron as collect_cron_jobs, format_json as format_cron_json, format_markdown as format_cron_markdown, format_table as format_cron_table
from .tui import ClawMonitorTUI
from .tree_cli import format_tree


def _config_with_overrides(cfg_path: Optional[str], openclaw_root: Optional[str]) -> Any:
    cfg = load_config(Path(cfg_path) if cfg_path else None)
    if openclaw_root:
        return cfg.__class__(**{**cfg.__dict__, "openclaw_root": Path(openclaw_root).expanduser()})
    return cfg


def cmd_tui(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    from .config import default_config_path

    cfg_path = Path(args.config).expanduser() if args.config else default_config_path()
    ClawMonitorTUI(cfg, config_path=cfg_path).run()
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    cfg_path = Path(args.config).expanduser() if args.config else None
    oc_root = Path(args.openclaw_root).expanduser() if args.openclaw_root else None
    res = run_init(
        config_path=cfg_path,
        lang=args.lang,
        openclaw_root=oc_root,
        openclaw_bin=args.openclaw_bin,
        ui_seconds=args.ui_seconds,
        defaults=bool(args.defaults),
        force=bool(args.force),
    )
    if not res.ok:
        raise SystemExit(res.reason or "init failed")
    if res.path:
        print(str(res.path))
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    sessions = list_sessions(cfg.openclaw_root)
    out: Dict[str, Any] = {
        "openclaw_root": str(cfg.openclaw_root),
        "count": len(sessions),
        "sessions": [
            {
                "agentId": s.agent_id,
                "key": s.key,
                "sessionId": s.session_id,
                "updatedAtMs": s.updated_at_ms,
                "channel": s.channel,
                "accountId": s.account_id,
                "to": redact_text(s.to or ""),
                "sessionFile": str(s.session_file) if s.session_file else None,
                "abortedLastRun": s.aborted_last_run,
                "systemSent": s.system_sent,
                "acpState": s.acp_state,
                "acpxSessionId": s.acpx_session_id,
                "acpAgent": s.acp_agent,
            }
            for s in sessions
        ],
    }
    fmt = args.format
    if getattr(args, "json", False):
        fmt = "json"
    if fmt == "json":
        print(json.dumps(out, ensure_ascii=False, indent=2))
    elif fmt == "md":
        header = ["agentId", "channel", "accountId", "updatedAtMs", "sessionKey", "systemSent"]
        lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
        for s in sessions:
            lines.append(
                "| "
                + " | ".join(
                    [
                        s.agent_id or "-",
                        s.channel or "-",
                        s.account_id or "-",
                        str(s.updated_at_ms or "-"),
                        s.key or "-",
                        "true" if s.system_sent else "false",
                    ]
                )
                + " |"
            )
        print("\n".join(lines))
    else:
        for s in sessions:
            print(f"{s.agent_id} {s.channel or '-'} {s.key} updatedAt={s.updated_at_ms}")
    return 0


def cmd_nudge(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    elog = EventLog()
    template_id = args.template
    if template_id not in TEMPLATES:
        raise SystemExit(f"Unknown template: {template_id}. Choose from: {', '.join(TEMPLATES.keys())}")
    elog.write("nudge.sent", sessionKey=args.session_key, template=template_id)
    res = send_nudge(cfg.openclaw_bin, args.session_key, template_id, deliver=not args.no_deliver)
    elog.write("nudge.result", sessionKey=args.session_key, ok=res.ok, runId=res.run_id or "", status=res.status or "", error=res.error or "")
    if args.json:
        print(json.dumps({"ok": res.ok, "runId": res.run_id, "status": res.status, "error": res.error}, ensure_ascii=False, indent=2))
    else:
        print(f"ok={res.ok} runId={res.run_id} status={res.status} error={res.error}")
    return 0 if res.ok else 2


def cmd_status(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    rows = collect_status(
        openclaw_root=cfg.openclaw_root,
        openclaw_bin=cfg.openclaw_bin,
        transcript_tail_bytes=cfg.transcript_tail_bytes,
        hide_system_sessions=args.hide_system if args.hide_system is not None else cfg.hide_system_sessions,
        include_gateway_channels=not args.no_gateway,
        label_map=cfg.labels,
    )
    if args.format == "json":
        print(format_status_json(rows, cfg.openclaw_root))
    elif args.format == "md":
        print(format_status_markdown(rows, limit=args.limit, detail=bool(args.detail)))
    else:
        print(format_table(rows, limit=args.limit, detail=bool(args.detail)))
    return 0


def cmd_cron(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    rows = collect_cron_jobs(cfg.openclaw_root)
    if args.format == "json":
        print(format_cron_json(rows, cfg.openclaw_root))
    elif args.format == "md":
        print(format_cron_markdown(rows))
    else:
        print(format_cron_table(rows))
    return 0


def cmd_models(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    include_direct = args.mode in ("both", "direct")
    include_openclaw = args.mode in ("both", "openclaw")
    options = ModelProbeOptions(
        prompt=args.prompt,
        timeout_seconds=int(args.timeout),
        include_direct=include_direct,
        include_openclaw=include_openclaw,
        max_workers=int(args.max_workers),
    )
    rows = collect_model_rows(
        openclaw_root=cfg.openclaw_root,
        openclaw_bin=cfg.openclaw_bin,
        options=options,
    )
    if args.format == "json":
        print(format_model_json(rows, openclaw_root=cfg.openclaw_root, options=options))
    elif args.format == "md":
        print(format_model_markdown(rows))
    else:
        print(format_model_table(rows))
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    metas = list_sessions(cfg.openclaw_root)
    meta = next((m for m in metas if m.key == args.session_key), None)
    if not meta:
        raise SystemExit(f"Unknown sessionKey: {args.session_key}")

    tail, acpx = tail_for_meta(meta, transcript_tail_bytes=cfg.transcript_tail_bytes)
    user_msg = tail.last_user_send or tail.last_user
    lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
    delivery_map = load_failed_delivery_map(cfg.openclaw_root)
    df = delivery_map.get(meta.key)

    cfg_snapshot = read_openclaw_config_snapshot(cfg.openclaw_root)
    compaction_cfg = cfg_snapshot.compaction_by_agent.get(meta.agent_id) or cfg_snapshot.compaction_by_agent.get("main")
    safeguard_ok = (compaction_cfg.mode == "safeguard") if compaction_cfg and compaction_cfg.mode else False
    working: Optional[WorkingSignal] = None
    if lock is None and acpx and meta.acp_state in ("running", "pending") and acpx_is_working(acpx):
        created_at = acpx.last_prompt_at or acpx.last_used_at or acpx.updated_at
        working = WorkingSignal(kind="acp", created_at=created_at, pid=acpx.pid, pid_alive=None)
    computed = compute_state(meta.aborted_last_run, tail, lock, df, safeguard_ok=safeguard_ok, working=working)

    gtail = GatewayLogTailer(cfg.openclaw_bin, ring_lines=cfg.gateway_log_ring_lines)
    if not args.no_gateway:
        gtail.poll(limit=min(cfg.gateway_log_ring_lines, max(50, args.gateway_poll_limit)))

    findings = diagnose(
        session_key=meta.key,
        channel=meta.channel,
        account_id=meta.account_id,
        delivery_failed=df is not None,
        no_feedback=computed.no_feedback,
        is_working=computed.state.value == "WORKING",
        gateway_lines=gtail.lines,
    )
    rel = related_logs(gtail.lines, meta.key, meta.channel, meta.account_id, limit=cfg.report_max_log_lines)

    summary: Dict[str, Any] = {
        "agent_id": meta.agent_id,
        "channel": meta.channel,
        "account_id": meta.account_id,
        "state": computed.state.value,
        "reason": computed.reason,
        "no_feedback": computed.no_feedback,
        "delivery_failed": df is not None,
        "safety_alert": computed.safety_alert,
        "safeguard_alert": computed.safeguard_alert,
        "aborted_last_run": meta.aborted_last_run,
        "system_sent": meta.system_sent,
        "acp_state": meta.acp_state,
        "acpx_session_id": meta.acpx_session_id,
        "acp_agent": meta.acp_agent,
        "last_user_at": user_msg.ts.isoformat() if user_msg and user_msg.ts else None,
        "last_assistant_at": tail.last_assistant.ts.isoformat() if tail.last_assistant and tail.last_assistant.ts else None,
        "last_user_preview": redact_text(user_msg.preview) if user_msg else None,
        "last_assistant_preview": redact_text(tail.last_assistant.preview) if tail.last_assistant else None,
        "last_entry_type": tail.last_entry_type,
        "last_entry_at": tail.last_entry_ts.isoformat() if tail.last_entry_ts else None,
    }

    formats: List[str]
    if args.format == "both":
        formats = ["json", "md"]
    else:
        formats = [args.format]
    out_dir = Path(args.out_dir).expanduser() if args.out_dir else None
    paths = write_report_files(
        session_key=meta.key,
        summary=summary,
        findings=findings,
        related_logs=rel,
        max_log_lines=cfg.report_max_log_lines,
        formats=formats,
        out_dir=out_dir,
    )
    elog = EventLog()
    for k, p in paths.items():
        elog.write("report.written", sessionKey=meta.key, format=k, path=str(p))
    if args.json:
        print(json.dumps({k: str(p) for k, p in paths.items()}, ensure_ascii=False, indent=2))
    else:
        for k, p in paths.items():
            print(f"{k}: {p}")
    return 0


def cmd_tree(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    rows = collect_status(
        openclaw_root=cfg.openclaw_root,
        openclaw_bin=cfg.openclaw_bin,
        transcript_tail_bytes=cfg.transcript_tail_bytes,
        hide_system_sessions=args.hide_system if args.hide_system is not None else cfg.hide_system_sessions,
        include_gateway_channels=not args.no_gateway,
    )
    print(format_tree(rows, include_task=not bool(args.no_task)))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    watch_loop(
        openclaw_root=cfg.openclaw_root,
        openclaw_bin=cfg.openclaw_bin,
        transcript_tail_bytes=cfg.transcript_tail_bytes,
        hide_system_sessions=args.hide_system if args.hide_system is not None else cfg.hide_system_sessions,
        interval_seconds=float(args.interval),
        limit=args.limit,
        label_map=cfg.labels,
    )
    return 0


def cmd_push(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    metas = list_sessions(cfg.openclaw_root)
    meta = next((m for m in metas if m.key == args.session_key), None)
    if not meta:
        raise SystemExit(f"Unknown sessionKey: {args.session_key}")
    if not meta.channel or not meta.to:
        raise SystemExit("Session is missing delivery context (channel/to); cannot push.")

    # Compose a minimal monitor-generated status message (does NOT affect the session).
    rows = collect_status(
        openclaw_root=cfg.openclaw_root,
        openclaw_bin=cfg.openclaw_bin,
        transcript_tail_bytes=cfg.transcript_tail_bytes,
        hide_system_sessions=False,
        include_gateway_channels=not args.no_gateway,
    )
    row = next((r for r in rows if r.key == meta.key), None)
    if not row:
        raise SystemExit("Session not found in status collection.")

    flags = ",".join(row.flags) if row.flags else "-"
    msg = (
        f"ClawMonitor: {row.state} flags={flags}\n"
        f"userAge={row.user_age} asstAge={row.assistant_age} runFor={row.run_for}\n"
        f"reason={row.reason}\n"
        f"sessionKey={row.key}"
    )
    if args.message:
        msg = args.message.strip() + "\n\n" + msg

    res = push_message(
        openclaw_bin=cfg.openclaw_bin,
        channel=meta.channel,
        account_id=meta.account_id,
        target=meta.to,
        message=msg,
        dry_run=bool(args.dry_run),
        silent=bool(args.silent),
    )
    elog = EventLog()
    elog.write(
        "push.sent",
        sessionKey=meta.key,
        channel=meta.channel,
        accountId=meta.account_id or "",
        to=meta.to,
        ok=res.ok,
        rc=res.returncode,
    )
    if args.json:
        print(json.dumps({"ok": res.ok, "rc": res.returncode, "stdout": res.stdout, "stderr": res.stderr}, ensure_ascii=False, indent=2))
    else:
        print(f"ok={res.ok} rc={res.returncode}")
        if res.stderr.strip():
            print(res.stderr.strip())
    return 0 if res.ok else 2


def main() -> None:
    p = argparse.ArgumentParser(prog="clawmonitor")
    p.add_argument("--config", help="Path to config.toml (default: ~/.config/clawmonitor/config.toml)")
    p.add_argument("--openclaw-root", help="Override OpenClaw state dir (default: ~/.openclaw)")

    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="Interactive setup wizard (writes config.toml)")
    init.add_argument("--lang", choices=["en", "zh"], help="Language for prompts (default: ask)")
    init.add_argument("--openclaw-bin", help="OpenClaw CLI binary (default: openclaw)")
    init.add_argument("--ui-seconds", type=float, help="TUI refresh interval seconds (default: 5.0)")
    init.add_argument("--defaults", action="store_true", help="Non-interactive; write config with defaults")
    init.add_argument("--force", action="store_true", help="Overwrite existing config")
    init.set_defaults(func=cmd_init)

    tui = sub.add_parser("tui", help="Run full-screen TUI monitor")
    tui.set_defaults(func=cmd_tui)

    snap = sub.add_parser("snapshot", help="Print a snapshot of known sessions")
    snap.add_argument("--format", choices=["text", "json", "md"], default="text")
    snap.add_argument("--json", action="store_true", help="Alias for --format json")
    snap.set_defaults(func=cmd_snapshot)

    nudge = sub.add_parser("nudge", help="Send a progress nudge via chat.send")
    nudge.add_argument("--session-key", required=True)
    nudge.add_argument("--template", required=True, choices=sorted(TEMPLATES.keys()))
    nudge.add_argument("--no-deliver", action="store_true", help="Do not deliver to channel (still runs in session)")
    nudge.add_argument("--json", action="store_true")
    nudge.set_defaults(func=cmd_nudge)

    status = sub.add_parser("status", help="Print computed per-session core status (no curses)")
    status.add_argument("--format", choices=["text", "json", "md"], default="text")
    status.add_argument("--limit", type=int, help="Max sessions to print (text only)")
    status.add_argument("--detail", action="store_true", help="Include task and message previews (text/md only)")
    status.add_argument("--hide-system", action="store_true", help="Hide systemSent sessions")
    status.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment (channels/logs)")
    status.set_defaults(func=cmd_status)

    cron = sub.add_parser("cron", help="List configured cron jobs and last run status")
    cron.add_argument("--format", choices=["text", "json", "md"], default="text")
    cron.set_defaults(func=cmd_cron)

    models = sub.add_parser("models", help="Probe configured models directly and/or through OpenClaw")
    models.add_argument("--format", choices=["text", "json", "md"], default="text")
    models.add_argument("--mode", choices=["both", "direct", "openclaw"], default="both")
    models.add_argument("--timeout", type=int, default=20, help="Per-probe timeout seconds")
    models.add_argument("--prompt", default="Reply with exactly OK.", help="Short probe prompt")
    models.add_argument("--max-workers", type=int, default=4, help="Parallel probe workers")
    models.set_defaults(func=cmd_models)

    tree = sub.add_parser("tree", help="Print a tree-ish view grouped by agent")
    tree.add_argument("--hide-system", action="store_true", help="Hide systemSent sessions")
    tree.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment (channels/logs)")
    tree.add_argument("--no-task", action="store_true", help="Do not include task previews")
    tree.set_defaults(func=cmd_tree)

    rep = sub.add_parser("report", help="Export a single-session report (JSON/MD)")
    rep.add_argument("--session-key", required=True)
    rep.add_argument("--format", choices=["json", "md", "both"], default="both")
    rep.add_argument("--out-dir", help="Override output directory (default: XDG_STATE_HOME/clawmonitor/reports)")
    rep.add_argument("--no-gateway", action="store_true", help="Disable Gateway logs tail (offline report)")
    rep.add_argument("--gateway-poll-limit", type=int, default=200, help="Max lines to poll from logs.tail")
    rep.add_argument("--json", action="store_true", help="Print paths as JSON")
    rep.set_defaults(func=cmd_report)

    watch = sub.add_parser("watch", help="Continuously print status table (no curses)")
    watch.add_argument("--interval", type=float, default=1.0, help="Refresh interval seconds")
    watch.add_argument("--limit", type=int, help="Max sessions to print")
    watch.add_argument("--hide-system", action="store_true", help="Hide systemSent sessions")
    watch.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment (channels/logs)")
    watch.set_defaults(func=cmd_watch)

    push = sub.add_parser("push", help="Send a monitor-generated status message to the session's IM target (does not affect the session)")
    push.add_argument("--session-key", required=True)
    push.add_argument("--message", help="Optional prefix message (prepended)")
    push.add_argument("--silent", action="store_true", help="Send without notification when supported (Telegram/Discord)")
    push.add_argument("--dry-run", action="store_true", help="Do not send; print payload via openclaw")
    push.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment while computing status")
    push.add_argument("--json", action="store_true")
    push.set_defaults(func=cmd_push)

    args = p.parse_args()
    if args.cmd != "init":
        maybe_run_first_time_init(config_flag=args.config, openclaw_root_flag=args.openclaw_root)
    raise SystemExit(args.func(args))
