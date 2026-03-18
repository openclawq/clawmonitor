from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import time

from .channels_status import fetch_channels_status
from .channels_status import ChannelsSnapshot
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .openclaw_config import read_openclaw_config_snapshot
from .openclaw_cron import match_cron_job, read_cron_snapshot
from .redact import redact_text
from .session_keys import parse_session_key
from .session_tail import tail_for_meta
from .session_store import SessionMeta, list_sessions
from .state import WorkState, WorkingSignal, compute_state
from .thread_bindings import load_telegram_thread_bindings
from .acpx_sessions import acpx_is_working


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


def _channel_account_info(
    channels: Optional[ChannelsSnapshot],
    *,
    channel: Optional[str],
    account_id: Optional[str],
) -> Optional[Dict[str, object]]:
    if not channels or not channel:
        return None
    chan = (channel or "").strip()
    if not chan:
        return None
    acct = (account_id or "").strip() or (channels.raw.get("channelDefaultAccountId", {}) or {}).get(chan) or "default"
    try:
        accounts = (channels.raw.get("channelAccounts", {}) or {}).get(chan)
        if isinstance(accounts, list):
            for ent in accounts:
                if not isinstance(ent, dict):
                    continue
                if str(ent.get("accountId") or "") == str(acct):
                    return ent
    except Exception:
        return None
    return None


def _internal_activity_at(tail: Any) -> Optional[datetime]:
    candidates: List[datetime] = []
    try:
        if tail.last_assistant and tail.last_assistant.ts:
            candidates.append(tail.last_assistant.ts)
    except Exception:
        pass
    try:
        if tail.last_tool_error and tail.last_tool_error[0]:
            candidates.append(tail.last_tool_error[0])
    except Exception:
        pass
    try:
        if tail.last_tool_result and tail.last_tool_result.ts:
            candidates.append(tail.last_tool_result.ts)
    except Exception:
        pass
    try:
        if tail.last_entry_type and tail.last_entry_type != "message" and tail.last_entry_ts:
            candidates.append(tail.last_entry_ts)
    except Exception:
        pass
    if not candidates:
        return None
    return max(candidates)


@dataclass(frozen=True)
class StatusRow:
    agent_id: str
    agent_label: str
    channel: Optional[str]
    account_id: Optional[str]
    key: str
    session_label: str
    session_kind: str
    agent_kind: str
    state: str
    flags: List[str]
    updated_at: str
    updated_age: str
    transcript_missing: bool
    last_user_at: str
    last_assistant_at: str
    user_age: str
    assistant_age: str
    human_out_at: str
    human_out_age: str
    internal_at: str
    internal_age: str
    last_tool_at: str
    last_tool_name: str
    last_tool_ok: str
    run_for: str
    task_preview: str
    last_user_preview: str
    last_assistant_preview: str
    reason: str
    last_entry_type: str
    assistant_model: str
    assistant_provider: str
    acp_state: str
    acpx_session_id: str


def collect_status(
    openclaw_root: Path,
    openclaw_bin: str,
    transcript_tail_bytes: int,
    hide_system_sessions: bool,
    include_gateway_channels: bool = True,
    label_map: Optional[Dict[str, str]] = None,
) -> List[StatusRow]:
    metas = list_sessions(openclaw_root)
    delivery_map = load_failed_delivery_map(openclaw_root)
    cfg_snapshot = read_openclaw_config_snapshot(openclaw_root)
    cron_snapshot = read_cron_snapshot(openclaw_root)
    channels = fetch_channels_status(openclaw_bin) if include_gateway_channels else None
    telegram_bindings = load_telegram_thread_bindings(openclaw_root, account_id="default")

    rows: List[StatusRow] = []
    for meta in metas:
        if hide_system_sessions and meta.system_sent:
            continue
        tail, acpx = tail_for_meta(meta, transcript_tail_bytes=transcript_tail_bytes)
        user_msg = tail.last_user_send
        user_preview = redact_text(user_msg.preview) if user_msg else "-"
        assistant_preview = redact_text(tail.last_assistant.preview) if tail.last_assistant else "-"
        lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
        df = delivery_map.get(meta.key)
        compaction_cfg = cfg_snapshot.compaction_by_agent.get(meta.agent_id) or cfg_snapshot.compaction_by_agent.get("main")
        safeguard_ok = (compaction_cfg.mode == "safeguard") if compaction_cfg and compaction_cfg.mode else False
        working: Optional[WorkingSignal] = None
        if lock is None and meta.acp_state in ("running", "pending"):
            if acpx is None:
                working = WorkingSignal(kind="acp", created_at=_dt_from_ms(meta.updated_at_ms), pid=None, pid_alive=None)
            elif acpx_is_working(acpx):
                created_at = acpx.last_prompt_at or acpx.last_used_at or acpx.updated_at or _dt_from_ms(meta.updated_at_ms)
                working = WorkingSignal(kind="acp", created_at=created_at, pid=acpx.pid, pid_alive=None)
        computed = compute_state(meta.aborted_last_run, tail, lock, df, safeguard_ok=safeguard_ok, working=working)

        flags: List[str] = []
        key_info = parse_session_key(meta.key)
        session_kind = key_info.kind
        if key_info.kind == "subagent":
            flags.append("SUBAGENT")
        elif key_info.kind == "acp":
            flags.append("ACP")
        elif key_info.kind == "heartbeat":
            flags.append("HEARTBEAT")
        elif key_info.kind == "cron":
            flags.append("CRON")
        elif key_info.kind == "cron_run":
            flags.append("CRON_RUN")
        if working and working.kind == "acp":
            flags.append("ACP_RUNNING")

        agent_kind = "configured" if cfg_snapshot.configured_agent_ids.get(meta.agent_id, False) else "implicit"
        if agent_kind == "implicit":
            flags.append("IMPL_AGENT")
        aid = (meta.agent_id or "").lower()
        if aid == "codex" or aid.startswith("codex"):
            flags.append("CODEX")
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
        run_at = lock.created_at if lock else (working.created_at if working else None)
        if run_at:
            run_for = _fmt_age(int((datetime.now(timezone.utc) - run_at).total_seconds()))

        updated_dt = _dt_from_ms(meta.updated_at_ms)
        transcript_missing = bool(meta.session_file) and not bool(meta.session_file.exists())

        acct_info = _channel_account_info(channels, channel=meta.channel, account_id=meta.account_id)
        out_at = _dt_from_ms(int(acct_info.get("lastOutboundAt")) if isinstance(acct_info, dict) and isinstance(acct_info.get("lastOutboundAt"), int) else None)
        internal_at = _internal_activity_at(tail)
        last_tool_at = "-"
        last_tool_name = "-"
        last_tool_ok = "-"
        if getattr(tail, "last_tool_result", None):
            tr = tail.last_tool_result
            if tr:
                last_tool_at = _fmt_dt(tr.ts)
                last_tool_name = tr.tool_name or "-"
                last_tool_ok = "err" if tr.is_error else "ok"

        task_preview = "-"
        if computed.state == WorkState.WORKING:
            src = tail.last_user_send or tail.last_trigger
            task_preview = redact_text(src.preview) if src and src.preview else "-"

        agent_label = cfg_snapshot.agent_label(meta.agent_id)
        cron_job = match_cron_job(cron_snapshot, meta.key)
        session_label = meta.key
        if label_map:
            try:
                from .labels import session_display_label

                lbl = session_display_label(label_map, meta)
                if lbl:
                    session_label = lbl
            except Exception:
                pass
        if cron_job:
            base = cron_job.name or cron_job.id
            if key_info.kind == "cron_run":
                parts = (meta.key or "").split(":")
                run_id = parts[5] if len(parts) >= 6 and parts[4] == "run" else ""
                session_label = f"cron:{base}:run:{run_id}".rstrip(":")
            else:
                session_label = f"cron:{base}"

        rows.append(
            StatusRow(
                agent_id=meta.agent_id,
                agent_label=agent_label,
                channel=meta.channel,
                account_id=meta.account_id,
                key=meta.key,
                session_label=session_label,
                session_kind=session_kind,
                agent_kind=agent_kind,
                state=computed.state.value,
                flags=flags,
                updated_at=_fmt_dt(updated_dt),
                updated_age=_fmt_age(_age_seconds(updated_dt)),
                transcript_missing=transcript_missing,
                last_user_at=_fmt_dt(user_msg.ts if user_msg else None),
                last_assistant_at=_fmt_dt(tail.last_assistant.ts if tail.last_assistant else None),
                user_age=_fmt_age(_age_seconds(user_msg.ts if user_msg else None)),
                assistant_age=_fmt_age(_age_seconds(tail.last_assistant.ts if tail.last_assistant else None)),
                human_out_at=_fmt_dt(out_at),
                human_out_age=_fmt_age(_age_seconds(out_at)),
                internal_at=_fmt_dt(internal_at),
                internal_age=_fmt_age(_age_seconds(internal_at)),
                last_tool_at=last_tool_at,
                last_tool_name=last_tool_name,
                last_tool_ok=last_tool_ok,
                run_for=run_for,
                task_preview=task_preview[:120] if task_preview else "-",
                last_user_preview=user_preview[:120] if user_preview else "-",
                last_assistant_preview=assistant_preview[:120] if assistant_preview else "-",
                reason=computed.reason,
                last_entry_type=tail.last_entry_type or "-",
                assistant_model=(tail.last_assistant.model or "-") if tail.last_assistant else "-",
                assistant_provider=(tail.last_assistant.provider or "-") if tail.last_assistant else "-",
                acp_state=meta.acp_state or "-",
                acpx_session_id=meta.acpx_session_id or "-",
            )
        )
    return rows


def format_table(rows: List[StatusRow], limit: Optional[int] = None, *, detail: bool = False) -> str:
    shown = rows[:limit] if limit else rows
    def fit(text: str, width: int) -> str:
        s = text or "-"
        if len(s) <= width:
            return s.ljust(width)
        if width <= 1:
            return s[:width]
        return (s[: width - 1] + "…")[:width]

    agent_w = max(5, min(20, max((len(r.agent_label or r.agent_id or "") for r in shown), default=5)))
    if detail:
        header = f"{fit('AGENT', agent_w)}  KIND      STATE        RUN   FLAGS                TASK"
    else:
        header = f"{fit('AGENT', agent_w)}  CHAN      STATE        UPD   U_AGE  A_AGE  RUN   FLAGS                SESSION"
    lines = [header]
    for r in shown:
        flags_list = list(r.flags)
        if r.transcript_missing and "TRXM" not in flags_list:
            flags_list.append("TRXM")
        flags = ",".join(flags_list)[:20]
        if detail:
            kind = f"{r.session_kind}/{r.agent_kind}"
            agent_txt = r.agent_label or r.agent_id
            line = f"{fit(agent_txt, agent_w)}  {fit(kind, 8)}  {r.state:<11}  {r.run_for:>4}  {flags:<20}  {fit(r.task_preview, 60)}"
        else:
            agent_txt = r.agent_label or r.agent_id
            sess_txt = r.session_label or r.key
            line = f"{fit(agent_txt, agent_w)}  {(r.channel or '-')[:8]:<8}  {r.state:<11}  {r.updated_age:>4}  {r.user_age:>4}  {r.assistant_age:>4}  {r.run_for:>4}  {flags:<20}  {sess_txt}"
        lines.append(line)
    return "\n".join(lines)


def format_markdown(rows: List[StatusRow], limit: Optional[int] = None, *, detail: bool = False) -> str:
    shown = rows[:limit] if limit else rows
    header = [
        "agentId",
        "channel",
        "state",
        "updatedAge",
        "userAge",
        "assistantAge",
        "outAgeHuman",
        "thinkAge",
        "lastTool",
        "runFor",
        "flags",
        "sessionKey",
        "reason",
    ]
    if detail:
        header = header[:-2] + ["taskPreview", "lastUserPreview", "lastAssistantPreview"] + header[-2:]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(["---"] * len(header)) + " |"]
    def esc(v: str) -> str:
        return (v or "-").replace("|", "\\|").replace("\n", " ")

    for r in shown:
        row = [
            esc(r.agent_id),
            esc(r.channel or "-"),
            esc(r.state),
            esc(r.updated_age),
            esc(r.user_age),
            esc(r.assistant_age),
            esc(r.human_out_age),
            esc(r.internal_age),
            esc(f"{r.last_tool_name}:{r.last_tool_ok}" if r.last_tool_name != "-" and r.last_tool_ok != "-" else "-"),
            esc(r.run_for),
            esc(",".join(r.flags) if r.flags else "-"),
        ]
        if detail:
            row += [esc(r.task_preview), esc(r.last_user_preview), esc(r.last_assistant_preview)]
        row += [esc(r.key), esc(r.reason)]
        lines.append("| " + " | ".join(row) + " |")
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
                "sessionKind": r.session_kind,
                "agentKind": r.agent_kind,
                "state": r.state,
                "flags": r.flags,
                "updatedAt": r.updated_at,
                "updatedAge": r.updated_age,
                "transcriptMissing": r.transcript_missing,
                "lastUserAt": r.last_user_at,
                "lastAssistantAt": r.last_assistant_at,
                "lastEntryType": r.last_entry_type,
                "humanOutAt": r.human_out_at,
                "humanOutAge": r.human_out_age,
                "thinkAt": r.internal_at,
                "thinkAge": r.internal_age,
                "lastToolAt": r.last_tool_at,
                "lastToolName": r.last_tool_name,
                "lastToolOk": r.last_tool_ok,
                "assistantModel": r.assistant_model,
                "assistantProvider": r.assistant_provider,
                "acpState": r.acp_state,
                "acpxSessionId": r.acpx_session_id,
                "taskPreview": r.task_preview,
                "lastUserPreview": r.last_user_preview,
                "lastAssistantPreview": r.last_assistant_preview,
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
    label_map: Optional[Dict[str, str]] = None,
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
                label_map=label_map,
            )
            os.system("clear")
            print(format_table(rows, limit=limit))
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        return
