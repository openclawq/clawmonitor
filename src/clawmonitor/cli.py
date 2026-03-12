from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, Optional

from .actions import TEMPLATES, send_nudge
from .config import load_config
from .eventlog import EventLog
from .redact import redact_text
from .session_store import list_sessions
from .status_cli import collect_status, format_json as format_status_json, format_table, watch_loop
from .tui import ClawMonitorTUI


def _config_with_overrides(cfg_path: Optional[str], openclaw_root: Optional[str]) -> Any:
    cfg = load_config(Path(cfg_path) if cfg_path else None)
    if openclaw_root:
        return cfg.__class__(**{**cfg.__dict__, "openclaw_root": Path(openclaw_root).expanduser()})
    return cfg


def cmd_tui(args: argparse.Namespace) -> int:
    cfg = _config_with_overrides(args.config, args.openclaw_root)
    ClawMonitorTUI(cfg).run()
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
            }
            for s in sessions
        ],
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
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
    )
    if args.format == "json":
        print(format_status_json(rows, cfg.openclaw_root))
    else:
        print(format_table(rows, limit=args.limit))
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
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(prog="clawmonitor")
    p.add_argument("--config", help="Path to config.toml (default: ~/.config/clawmonitor/config.toml)")
    p.add_argument("--openclaw-root", help="Override OpenClaw state dir (default: ~/.openclaw)")

    sub = p.add_subparsers(dest="cmd", required=True)

    tui = sub.add_parser("tui", help="Run full-screen TUI monitor")
    tui.set_defaults(func=cmd_tui)

    snap = sub.add_parser("snapshot", help="Print a snapshot of known sessions")
    snap.add_argument("--json", action="store_true", help="Output JSON")
    snap.set_defaults(func=cmd_snapshot, json=True)

    nudge = sub.add_parser("nudge", help="Send a progress nudge via chat.send")
    nudge.add_argument("--session-key", required=True)
    nudge.add_argument("--template", required=True, choices=sorted(TEMPLATES.keys()))
    nudge.add_argument("--no-deliver", action="store_true", help="Do not deliver to channel (still runs in session)")
    nudge.add_argument("--json", action="store_true")
    nudge.set_defaults(func=cmd_nudge)

    status = sub.add_parser("status", help="Print computed per-session core status (no curses)")
    status.add_argument("--format", choices=["text", "json"], default="text")
    status.add_argument("--limit", type=int, help="Max sessions to print (text only)")
    status.add_argument("--hide-system", action="store_true", help="Hide systemSent sessions")
    status.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment (channels/logs)")
    status.set_defaults(func=cmd_status)

    watch = sub.add_parser("watch", help="Continuously print status table (no curses)")
    watch.add_argument("--interval", type=float, default=1.0, help="Refresh interval seconds")
    watch.add_argument("--limit", type=int, help="Max sessions to print")
    watch.add_argument("--hide-system", action="store_true", help="Hide systemSent sessions")
    watch.add_argument("--no-gateway", action="store_true", help="Disable Gateway enrichment (channels/logs)")
    watch.set_defaults(func=cmd_watch)

    args = p.parse_args()
    raise SystemExit(args.func(args))
