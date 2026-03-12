from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import time

from .channels_status import fetch_channels_status
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .openclaw_config import read_openclaw_config_snapshot
from .redact import redact_text
from .session_keys import parse_session_key
from .session_store import SessionMeta, list_sessions
from .state import WorkState, compute_state
from .thread_bindings import load_telegram_thread_bindings
from .transcript_tail import TranscriptTail, tail_transcript


def _fmt_dt(dt: Optional[datetime]) -> str:
    if not dt:
        return "-"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _dt_from_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


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
class StatusRow:
    agent_id: str
    channel: Optional[str]
    account_id: Optional[str]
    key: str
    state: str
    flags: List[str]
    updated_at: str
    updated_age: str
    transcript_missing: bool
    last_user_at: str
    last_assistant_at: str
    user_age: str
    assistant_age: str
    run_for: str
    reason: str


def collect_status(
    openclaw_root: Path,
    openclaw_bin: str,
    transcript_tail_bytes: int,
    hide_system_sessions: bool,
    include_gateway_channels: bool = True,
) -> List[StatusRow]:
    metas = list_sessions(openclaw_root)
    delivery_map = load_failed_delivery_map(openclaw_root)
    cfg_snapshot = read_openclaw_config_snapshot(openclaw_root)
    channels = fetch_channels_status(openclaw_bin) if include_gateway_channels else None
    telegram_bindings = load_telegram_thread_bindings(openclaw_root, account_id="default")

    rows: List[StatusRow] = []
    for meta in metas:
        if hide_system_sessions and meta.system_sent:
            continue
        tail = tail_transcript(meta.session_file, max_bytes=transcript_tail_bytes) if meta.session_file else TranscriptTail(None, None, None, None, None, None)
        user_msg = tail.last_user_send or tail.last_user
        lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
        df = delivery_map.get(meta.key)
        compaction_cfg = cfg_snapshot.compaction_by_agent.get(meta.agent_id) or cfg_snapshot.compaction_by_agent.get("main")
        safeguard_ok = (compaction_cfg.mode == "safeguard") if compaction_cfg and compaction_cfg.mode else False
        computed = compute_state(meta.aborted_last_run, tail, lock, df, safeguard_ok=safeguard_ok)

        flags: List[str] = []
        key_info = parse_session_key(meta.key)
        if key_info.kind == "subagent":
            flags.append("SUBAGENT")
        elif key_info.kind == "acp":
            flags.append("ACP")
        elif key_info.kind == "heartbeat":
            flags.append("HEARTBEAT")

        if not cfg_snapshot.configured_agent_ids.get(meta.agent_id, False):
            flags.append("IMPL_AGENT")
        if computed.no_feedback:
            flags.append("NO_FEEDBACK")
        if df:
            flags.append("DELIVERY_FAILED")
        if lock and lock.pid_alive is False:
            flags.append("ZOMBIE_LOCK")
        if computed.safety_alert:
            flags.append("SAFETY")
        if computed.safeguard_alert:
            flags.append("SAFEGUARD_OFF")

        # Telegram routing: if a conversation is bound to a different session key,
        # this session will never receive new inbound for that chat.
        if (meta.channel or "") == "telegram" and meta.to and meta.to.startswith("telegram:"):
            conv_id = meta.to.split("telegram:", 1)[1].strip()
            b = telegram_bindings.get(conv_id)
            if b and b.target_session_key and b.target_session_key != meta.key:
                flags.append("BOUND_OTHER")

        run_for = "-"
        if lock and lock.created_at:
            run_for = _fmt_age(int((datetime.now(timezone.utc) - lock.created_at).total_seconds()))

        updated_dt = _dt_from_ms(meta.updated_at_ms)
        transcript_missing = bool(meta.session_file) and not bool(meta.session_file.exists())

        rows.append(
            StatusRow(
                agent_id=meta.agent_id,
                channel=meta.channel,
                account_id=meta.account_id,
                key=meta.key,
                state=computed.state.value,
                flags=flags,
                updated_at=_fmt_dt(updated_dt),
                updated_age=_fmt_age(_age_seconds(updated_dt)),
                transcript_missing=transcript_missing,
                last_user_at=_fmt_dt(user_msg.ts if user_msg else None),
                last_assistant_at=_fmt_dt(tail.last_assistant.ts if tail.last_assistant else None),
                user_age=_fmt_age(_age_seconds(user_msg.ts if user_msg else None)),
                assistant_age=_fmt_age(_age_seconds(tail.last_assistant.ts if tail.last_assistant else None)),
                run_for=run_for,
                reason=computed.reason,
            )
        )
    return rows


def format_table(rows: List[StatusRow], limit: Optional[int] = None) -> str:
    shown = rows[:limit] if limit else rows
    def fit(text: str, width: int) -> str:
        s = text or "-"
        if len(s) <= width:
            return s.ljust(width)
        if width <= 1:
            return s[:width]
        return (s[: width - 1] + "…")[:width]

    agent_w = max(5, min(16, max((len(r.agent_id or "") for r in shown), default=5)))
    header = f"{fit('AGENT', agent_w)}  CHAN      STATE        UPD   U_AGE  A_AGE  RUN   FLAGS                SESSION"
    lines = [header]
    for r in shown:
        flags_list = list(r.flags)
        if r.transcript_missing and "TRXM" not in flags_list:
            flags_list.append("TRXM")
        flags = ",".join(flags_list)[:20]
        line = f"{fit(r.agent_id, agent_w)}  {(r.channel or '-')[:8]:<8}  {r.state:<11}  {r.updated_age:>4}  {r.user_age:>4}  {r.assistant_age:>4}  {r.run_for:>4}  {flags:<20}  {r.key}"
        lines.append(line)
    return "\n".join(lines)


def format_markdown(rows: List[StatusRow], limit: Optional[int] = None) -> str:
    shown = rows[:limit] if limit else rows
    header = [
        "agentId",
        "channel",
        "state",
        "updatedAge",
        "userAge",
        "assistantAge",
        "runFor",
        "flags",
        "sessionKey",
        "reason",
    ]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    def esc(v: str) -> str:
        return (v or "-").replace("|", "\\|").replace("\n", " ")

    for r in shown:
        lines.append(
            "| "
            + " | ".join(
                [
                    esc(r.agent_id),
                    esc(r.channel or "-"),
                    esc(r.state),
                    esc(r.updated_age),
                    esc(r.user_age),
                    esc(r.assistant_age),
                    esc(r.run_for),
                    esc(",".join(r.flags) if r.flags else "-"),
                    esc(r.key),
                    esc(r.reason),
                ]
            )
            + " |"
        )
    return "\n".join(lines)


def format_json(rows: List[StatusRow], openclaw_root: Path) -> str:
    doc: Dict[str, Any] = {
        "openclaw_root": str(openclaw_root),
        "ts": datetime.now(timezone.utc).isoformat(),
        "count": len(rows),
        "rows": [
            {
                "agentId": r.agent_id,
                "channel": r.channel,
                "accountId": r.account_id,
                "key": r.key,
                "state": r.state,
                "flags": r.flags,
                "updatedAt": r.updated_at,
                "updatedAge": r.updated_age,
                "transcriptMissing": r.transcript_missing,
                "lastUserAt": r.last_user_at,
                "lastAssistantAt": r.last_assistant_at,
                "userAge": r.user_age,
                "assistantAge": r.assistant_age,
                "runFor": r.run_for,
                "reason": r.reason,
            }
            for r in rows
        ],
    }
    import json

    return json.dumps(doc, ensure_ascii=False, indent=2)


def watch_loop(
    openclaw_root: Path,
    openclaw_bin: str,
    transcript_tail_bytes: int,
    hide_system_sessions: bool,
    interval_seconds: float,
    limit: Optional[int],
) -> None:
    try:
        import os

        while True:
            rows = collect_status(
                openclaw_root=openclaw_root,
                openclaw_bin=openclaw_bin,
                transcript_tail_bytes=transcript_tail_bytes,
                hide_system_sessions=hide_system_sessions,
                include_gateway_channels=True,
            )
            os.system("clear")
            print(format_table(rows, limit=limit))
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        return
