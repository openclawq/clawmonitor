from __future__ import annotations

import curses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time
import re
import unicodedata

from .actions import TEMPLATES, send_nudge
from .channels_status import ChannelsSnapshot, fetch_channels_status
from .config import Config
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .diagnostics import Finding, diagnose, related_logs
from .eventlog import EventLog
from .gateway_logs import GatewayLogTailer
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .openclaw_config import OpenClawConfigSnapshot, read_openclaw_config_snapshot
from .redact import redact_text
from .session_keys import parse_session_key
from .reports import write_report_files
from .session_store import SessionMeta, list_sessions
from .state import SessionComputed, WorkState, compute_state
from .thread_bindings import TelegramThreadBinding, load_telegram_thread_bindings
from .transcript_tail import TranscriptTail, tail_transcript


_ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")


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
        return f"{age:>3}s"
    if age < 3600:
        return f"{age//60:>3}m"
    return f"{age//3600:>3}h"


def _fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    s = text or ""
    if _display_width(s) <= width:
        return _pad_right_cells(s, width)
    if width <= 1:
        return _truncate_cells(s, width)
    return _truncate_cells(s, width - 1) + "…"


def _wrap_lines(text: str, width: int, max_lines: int) -> List[str]:
    if width <= 0 or max_lines <= 0:
        return []
    t = (text or "").replace("\t", " ").strip()
    if not t:
        return []
    words = t.split()
    if len(words) <= 1:
        words = [t]

    lines: List[str] = []
    cur: List[str] = []
    cur_w = 0
    truncated = False

    for word in words:
        ww = _display_width(word)
        if ww > width:
            if cur:
                lines.append(_truncate_cells(" ".join(cur), width))
                cur = []
                cur_w = 0
                if len(lines) >= max_lines:
                    truncated = True
                    break
            for chunk in _split_cells(word, width):
                lines.append(chunk)
                if len(lines) >= max_lines:
                    if chunk != word:
                        truncated = True
                    break
            if len(lines) >= max_lines:
                if word not in lines:
                    truncated = True
                break
            continue

        if not cur:
            cur = [word]
            cur_w = ww
        else:
            if cur_w + 1 + ww <= width:
                cur.append(word)
                cur_w += 1 + ww
            else:
                lines.append(_truncate_cells(" ".join(cur), width))
                if len(lines) >= max_lines:
                    truncated = True
                    cur = []
                    cur_w = 0
                    break
                cur = [word]
                cur_w = ww

        if len(lines) >= max_lines:
            truncated = True
            break

    if len(lines) < max_lines and cur:
        lines.append(_truncate_cells(" ".join(cur), width))

    if truncated and lines:
        # Ensure ellipsis on the last visible line.
        last = lines[-1]
        if width >= 2:
            last = _truncate_cells(last, width - 1) + "…"
        else:
            last = _truncate_cells(last, width)
        lines[-1] = last
    return lines


def _cell_width(ch: str) -> int:
    if not ch:
        return 0
    if unicodedata.combining(ch):
        return 0
    eaw = unicodedata.east_asian_width(ch)
    if eaw in ("W", "F"):
        return 2
    return 1


def _display_width(text: str) -> int:
    s = text or ""
    total = 0
    for ch in s:
        total += _cell_width(ch)
    return total


def _truncate_cells(text: str, width: int) -> str:
    if width <= 0:
        return ""
    s = text or ""
    out: List[str] = []
    used = 0
    for ch in s:
        cw = _cell_width(ch)
        if used + cw > width:
            break
        out.append(ch)
        used += cw
    return "".join(out)


def _pad_right_cells(text: str, width: int) -> str:
    s = text or ""
    used = _display_width(s)
    if used >= width:
        return _truncate_cells(s, width)
    return s + (" " * (width - used))


def _split_cells(text: str, width: int) -> List[str]:
    if width <= 0:
        return []
    s = text or ""
    out: List[str] = []
    cur: List[str] = []
    used = 0
    for ch in s:
        cw = _cell_width(ch)
        if cur and used + cw > width:
            out.append("".join(cur))
            cur = [ch]
            used = cw
        else:
            cur.append(ch)
            used += cw
    if cur:
        out.append("".join(cur))
    return out


def _sanitize_for_curses(text: str) -> str:
    """
    Curses writes can be corrupted by control characters (\\n/\\r/ANSI escapes),
    which may move the cursor and visually "spill" across panels.
    """
    if not text:
        return ""
    s = _ANSI_ESCAPE_RE.sub("", text)
    out: List[str] = []
    for ch in s:
        o = ord(ch)
        if ch in ("\n", "\r", "\t"):
            out.append(" ")
        elif o < 32 or o == 127:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _dt_from_ms(ms: Optional[int]) -> Optional[datetime]:
    if ms is None:
        return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    except Exception:
        return None


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


def _agent_markers(meta: SessionMeta, cfg_snapshot: Optional[OpenClawConfigSnapshot]) -> List[str]:
    markers: List[str] = []
    info = parse_session_key(meta.key)
    if info.kind == "subagent":
        markers.append(f"SUB{max(1, info.subagent_depth)}")
    if info.kind == "acp":
        markers.append("ACP")
    if cfg_snapshot and not cfg_snapshot.configured_agent_ids.get(meta.agent_id, False):
        markers.append("IMPL")
    aid = (meta.agent_id or "").lower()
    if aid == "codex" or aid.startswith("codex"):
        markers.append("CODEX")
    return markers


def _health_class(
    *,
    state: WorkState,
    no_feedback: bool,
    delivery_failed: bool,
    safety_alert: bool,
    safeguard_alert: bool,
) -> str:
    if no_feedback:
        return "alert"
    if delivery_failed:
        return "alert"
    if safety_alert or safeguard_alert:
        return "alert"
    if state == WorkState.INTERRUPTED:
        return "alert"
    if state == WorkState.WORKING:
        return "working"
    if state == WorkState.NO_MESSAGE:
        return "idle"
    return "ok"


def _health_label(cls: str) -> str:
    if cls == "ok":
        return "OK"
    if cls == "working":
        return "RUN"
    if cls == "idle":
        return "IDLE"
    if cls == "alert":
        return "ALERT"
    return cls.upper()[:6]


@dataclass
class SessionView:
    meta: SessionMeta
    tail: TranscriptTail
    lock: Optional[LockInfo]
    delivery_failure: Optional[DeliveryFailure]
    computed: SessionComputed
    findings: List[Finding]
    updated_at: Optional[datetime]
    transcript_missing: bool
    telegram_binding: Optional[TelegramThreadBinding]
    telegram_routed_elsewhere: bool


class MonitorModel:
    def __init__(self, cfg: Config, elog: EventLog) -> None:
        self.cfg = cfg
        self.elog = elog
        self._sessions: List[SessionView] = []
        self._tail_cache: Dict[Path, Tuple[float, int, TranscriptTail]] = {}
        self._delivery_map: Dict[str, DeliveryFailure] = {}
        self._delivery_last_load = 0.0
        self._gateway_logs = GatewayLogTailer(cfg.openclaw_bin, ring_lines=cfg.gateway_log_ring_lines)
        self._gateway_last_poll = 0.0
        self._channels: Optional[ChannelsSnapshot] = None
        self._channels_last_poll = 0.0
        self._telegram_bindings: Dict[str, Dict[str, TelegramThreadBinding]] = {}
        self._telegram_bindings_last_load = 0.0
        self._cfg_snapshot: Optional[OpenClawConfigSnapshot] = None
        self._cfg_snapshot_last_load = 0.0

    @property
    def gateway_log_tailer(self) -> GatewayLogTailer:
        return self._gateway_logs

    @property
    def channels(self) -> Optional[ChannelsSnapshot]:
        return self._channels

    @property
    def config_snapshot(self) -> Optional[OpenClawConfigSnapshot]:
        return self._cfg_snapshot

    @property
    def sessions(self) -> List[SessionView]:
        return list(self._sessions)

    def _refresh_delivery_map(self) -> None:
        now = time.time()
        if now - self._delivery_last_load < self.cfg.delivery_queue_poll_seconds:
            return
        self._delivery_map = load_failed_delivery_map(self.cfg.openclaw_root)
        self._delivery_last_load = now

    def _refresh_gateway_logs(self) -> None:
        now = time.time()
        if now - self._gateway_last_poll < self.cfg.gateway_log_poll_seconds:
            return
        self._gateway_logs.poll(limit=200)
        self._gateway_last_poll = now

    def _refresh_channels(self) -> None:
        now = time.time()
        if now - self._channels_last_poll < self.cfg.channels_status_poll_seconds:
            return
        snap = fetch_channels_status(self.cfg.openclaw_bin, probe=False, timeout_ms=10000)
        self._channels = snap
        self._channels_last_poll = now

    def _tail_for(self, session_file: Optional[Path]) -> TranscriptTail:
        if not session_file:
            return TranscriptTail(None, None, None, None, None, None, None)
        try:
            st = session_file.stat()
            key = session_file
            cached = self._tail_cache.get(key)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                return cached[2]
            tail = tail_transcript(session_file, max_bytes=self.cfg.transcript_tail_bytes)
            self._tail_cache[key] = (st.st_mtime, st.st_size, tail)
            return tail
        except Exception:
            return TranscriptTail(None, None, None, None, None, None, None)

    def _refresh_telegram_bindings(self) -> None:
        # Lightweight local file read; throttle to avoid needless IO.
        now = time.time()
        if now - self._telegram_bindings_last_load < 2.0:
            return
        # Today we only need the default account; keep dict-of-dicts to allow
        # multi-account expansion later.
        bindings = load_telegram_thread_bindings(self.cfg.openclaw_root, account_id="default")
        self._telegram_bindings = {"default": bindings}
        self._telegram_bindings_last_load = now

    def _refresh_config_snapshot(self) -> None:
        now = time.time()
        if now - self._cfg_snapshot_last_load < 2.0:
            return
        try:
            self._cfg_snapshot = read_openclaw_config_snapshot(self.cfg.openclaw_root)
        except Exception:
            self._cfg_snapshot = None
        self._cfg_snapshot_last_load = now

    def _telegram_binding_for(self, *, account_id: Optional[str], to: Optional[str]) -> Optional[TelegramThreadBinding]:
        if not to or not isinstance(to, str) or not to.startswith("telegram:"):
            return None
        conv_id = to.split("telegram:", 1)[1].strip()
        if not conv_id:
            return None
        aid = (account_id or "default").strip() or "default"
        return (self._telegram_bindings.get(aid) or {}).get(conv_id)

    def refresh(self) -> None:
        self._refresh_delivery_map()
        self._refresh_gateway_logs()
        self._refresh_channels()
        self._refresh_telegram_bindings()
        self._refresh_config_snapshot()

        metas = list_sessions(self.cfg.openclaw_root)
        views: List[SessionView] = []
        for meta in metas:
            if self.cfg.hide_system_sessions and meta.system_sent:
                continue
            tail = self._tail_for(meta.session_file)
            lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
            df = self._delivery_map.get(meta.key)
            safeguard_ok = True
            try:
                if self._cfg_snapshot:
                    compaction_cfg = self._cfg_snapshot.compaction_by_agent.get(meta.agent_id) or self._cfg_snapshot.compaction_by_agent.get("main")
                    safeguard_ok = bool(compaction_cfg and compaction_cfg.mode == "safeguard")
            except Exception:
                safeguard_ok = True
            computed = compute_state(meta.aborted_last_run, tail, lock, df, safeguard_ok=safeguard_ok)
            findings = diagnose(
                session_key=meta.key,
                channel=meta.channel,
                account_id=meta.account_id,
                delivery_failed=df is not None,
                no_feedback=computed.no_feedback,
                is_working=computed.state == WorkState.WORKING,
                gateway_lines=self._gateway_logs.lines,
            )
            transcript_missing = bool(meta.session_file) and not bool(meta.session_file.exists())
            telegram_binding: Optional[TelegramThreadBinding] = None
            telegram_routed_elsewhere = False
            if (meta.channel or "") == "telegram":
                telegram_binding = self._telegram_binding_for(account_id=meta.account_id, to=meta.to)
                if telegram_binding and telegram_binding.target_session_key and telegram_binding.target_session_key != meta.key:
                    telegram_routed_elsewhere = True
            views.append(
                SessionView(
                    meta=meta,
                    tail=tail,
                    lock=lock,
                    delivery_failure=df,
                    computed=computed,
                    findings=findings,
                    updated_at=_dt_from_ms(meta.updated_at_ms),
                    transcript_missing=transcript_missing,
                    telegram_binding=telegram_binding,
                    telegram_routed_elsewhere=telegram_routed_elsewhere,
                )
            )
        self._sessions = views


class ClawMonitorTUI:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.elog = EventLog()
        self.model = MonitorModel(cfg, self.elog)
        self.selected = 0
        self.scroll = 0
        self.show_logs = True
        self.refresh_seconds = float(cfg.ui_seconds)
        self._last_refresh_at: Optional[float] = None
        self._colors_enabled = False
        self._color_ok = 0
        self._color_working = 0
        self._color_idle = 0
        self._color_alert = 0

    def run(self) -> None:
        curses.wrapper(self._main)

    def _row_attr(self, health_cls: str, *, selected: bool) -> int:
        attr = curses.A_NORMAL
        if self._colors_enabled:
            if health_cls == "ok":
                attr |= self._color_ok
            elif health_cls == "working":
                attr |= self._color_working
            elif health_cls == "idle":
                attr |= self._color_idle
            elif health_cls == "alert":
                attr |= self._color_alert
        if selected:
            attr |= curses.A_REVERSE
        return attr

    def _safe_addnstr(
        self,
        stdscr: "curses._CursesWindow",
        y: int,
        x: int,
        text: str,
        width: int,
        attr: int = 0,
    ) -> None:
        if width <= 0:
            return
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        # Keep a 1-col margin to avoid curses wrapping long/wide strings into the
        # next line (which can visually "spill" into the left pane).
        maxw = min(width, max(0, w - x - 1))
        if maxw <= 0:
            return
        text = _sanitize_for_curses(text)
        text = _truncate_cells(text, maxw)
        try:
            if attr:
                stdscr.addnstr(y, x, text, maxw, attr)
            else:
                stdscr.addnstr(y, x, text, maxw)
        except curses.error:
            return

    def _draw_header(self, stdscr: "curses._CursesWindow", width: int) -> None:
        channels = self.model.channels
        mode = "online" if self.model.gateway_log_tailer.available else "offline"
        head = f"ClawMonitor  |  OpenClaw: {self.cfg.openclaw_root}  |  Gateway: {mode}  |  {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        self._safe_addnstr(stdscr, 0, 0, head.ljust(width), width, curses.A_REVERSE)
        if channels and isinstance(channels.raw.get("channelOrder"), list):
            chan_names = ", ".join([str(x) for x in channels.raw.get("channelOrder", [])])
            self._safe_addnstr(stdscr, 1, 0, f"Channels: {chan_names}".ljust(width), width)
        else:
            err = self.model.gateway_log_tailer.last_error
            self._safe_addnstr(stdscr, 1, 0, f"Channels: (unavailable) {err or ''}".ljust(width), width)

    def _draw_list(self, stdscr: "curses._CursesWindow", y: int, h: int, w: int, sessions: List[SessionView]) -> None:
        agent_w = 10
        chan_w = 8
        state_w = 11
        flags_w = 12
        header = f"{_fit('AGENT', agent_w)}  {_fit('CHAN', chan_w)}  {_fit('STATE', state_w)}  U-AGE  A-AGE  RUN   {_fit('FLAGS', flags_w)}  SESSION"
        self._safe_addnstr(stdscr, y, 0, header.ljust(w), w, curses.A_BOLD)
        body_y = y + 1
        visible = h - 1
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + visible:
            self.scroll = self.selected - visible + 1
        for i in range(visible):
            idx = self.scroll + i
            row_y = body_y + i
            if idx >= len(sessions):
                self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
                continue
            sv = sessions[idx]
            user_msg = sv.tail.last_user_send
            u_age = _fmt_age(_age_seconds(user_msg.ts if user_msg else sv.updated_at))
            a_age = _fmt_age(_age_seconds(sv.tail.last_assistant.ts if sv.tail.last_assistant else None))
            run = "-"
            if sv.lock and sv.lock.created_at:
                run = _fmt_age(int((datetime.now(timezone.utc) - sv.lock.created_at).total_seconds()))
            flags: List[str] = []
            health_cls = _health_class(
                state=sv.computed.state,
                no_feedback=sv.computed.no_feedback,
                delivery_failed=sv.delivery_failure is not None,
                safety_alert=sv.computed.safety_alert,
                safeguard_alert=sv.computed.safeguard_alert,
            )
            flags.append(_health_label(health_cls))
            if sv.computed.no_feedback:
                flags.append("NOFB")
            if sv.delivery_failure:
                flags.append("DLV")
            if sv.lock and sv.lock.pid_alive is False:
                flags.append("ZLOCK")
            if sv.computed.safety_alert:
                flags.append("SAFE")
            if sv.transcript_missing:
                flags.append("TRXM")
            if sv.telegram_routed_elsewhere:
                flags.append("BIND")
            flags.extend(_agent_markers(sv.meta, self.model.config_snapshot))
            flag_str = ",".join(flags)
            line = (
                f"{_fit(sv.meta.agent_id, agent_w)}  "
                f"{_fit((sv.meta.channel or '-'), chan_w)}  "
                f"{_fit(sv.computed.state.value, state_w)}  "
                f"{u_age:>5}  {a_age:>5}  {run:>5}  "
                f"{_fit(flag_str, flags_w)}  "
                f"{sv.meta.key}"
            )
            attr = self._row_attr(health_cls, selected=(idx == self.selected))
            self._safe_addnstr(stdscr, row_y, 0, line.ljust(w), w, attr)

    def _draw_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: Optional[SessionView]) -> None:
        if not sv:
            return
        rel_logs: List[str] = []
        last_activity: Optional[str] = None
        if self.show_logs:
            rel = related_logs(self.model.gateway_log_tailer.lines, sv.meta.key, sv.meta.channel, sv.meta.account_id, limit=50)
            rel_logs = [ln.text for ln in rel][-20:]
            if rel:
                ln = rel[-1]
                last_activity = (ln.text or ln.raw or "").strip()

        log_budget = 0
        if self.show_logs:
            log_budget = min(10, max(0, h // 3))
        detail_h = max(0, h - (log_budget + (1 if log_budget else 0)))

        # Clear details region first.
        for j in range(detail_h):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)

        if w >= 110 and detail_h >= 14:
            self._draw_details_status_split3(stdscr, x=x, y=y, h=detail_h, w=w, sv=sv, last_activity=last_activity)
        elif w >= 24 and detail_h >= 14:
            self._draw_details_status_stacked(stdscr, x=x, y=y, h=detail_h, w=w, sv=sv, last_activity=last_activity)
        else:
            self._draw_details_stacked(stdscr, x=x, y=y, h=detail_h, w=w, sv=sv)

        if self.show_logs and log_budget:
            log_y = y + detail_h
            self._safe_addnstr(stdscr, log_y, x, "Related Logs:".ljust(w), w, curses.A_BOLD)
            log_y += 1
            for i in range(min(log_budget, len(rel_logs))):
                self._safe_addnstr(stdscr, log_y + i, x, rel_logs[i][:w].ljust(w), w)
            for j in range(len(rel_logs), log_budget):
                self._safe_addnstr(stdscr, log_y + j, x, " ".ljust(w), w)

    def _draw_details_stacked(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: SessionView) -> None:
        lines: List[str] = []
        lines.append(f"SessionKey: {sv.meta.key}")
        markers = _agent_markers(sv.meta, self.model.config_snapshot)
        mark_str = f" ({','.join(markers)})" if markers else ""
        lines.append(f"Agent: {sv.meta.agent_id}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}")
        if sv.meta.kind or sv.meta.chat_type:
            lines.append(f"Kind: {sv.meta.kind or '-'}  ChatType: {sv.meta.chat_type or '-'}")
        lines.append(f"UpdatedAt: {_fmt_dt(sv.updated_at)}")
        if sv.meta.session_file:
            if sv.transcript_missing:
                lines.append(f"Transcript: MISSING ({sv.meta.session_file})")
            else:
                lines.append(f"Transcript: {sv.meta.session_file}")
        if sv.telegram_binding:
            b = sv.telegram_binding
            note = ""
            if sv.telegram_routed_elsewhere:
                note = "  (ROUTED ELSEWHERE)"
            lines.append(
                f"Telegram Binding: conv={b.conversation_id} -> {b.target_session_key} kind={b.target_kind or '-'} agent={b.agent_id or '-'}{note}"
            )
        lines.append(f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}")
        if sv.lock:
            lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            lines.append("Lock: -")
        lines.append(f"abortedLastRun: {sv.meta.aborted_last_run}  systemSent: {sv.meta.system_sent}")
        if sv.delivery_failure:
            lines.append(f"Delivery FAILED: retry={sv.delivery_failure.retry_count} err={redact_text(sv.delivery_failure.last_error or '-')}")
        lines.append("")
        # Keep the legacy stacked view, but prefer strict user send.
        if sv.tail.last_user_send:
            lines.append(f"Last User Send @ {_fmt_dt(sv.tail.last_user_send.ts)}")
            for part in _wrap_lines(redact_text(sv.tail.last_user_send.preview), max(0, w - 2), max_lines=3):
                lines.append(f"  {part}")
        else:
            lines.append("Last User Send: -")
        lines.append("")
        if sv.tail.last_assistant:
            lines.append(f"Last ASST @ {_fmt_dt(sv.tail.last_assistant.ts)}  stopReason={sv.tail.last_assistant.stop_reason or '-'}")
            for part in _wrap_lines(redact_text(sv.tail.last_assistant.preview), max(0, w - 2), max_lines=4):
                lines.append(f"  {part}")
        else:
            lines.append("Last ASST: -")
        lines.append("")
        lines.append("Diagnosis:")
        if sv.findings:
            for f in sv.findings[:6]:
                lines.append(f"- [{f.severity}] {f.id}: {f.summary}")
        else:
            lines.append("- (none)")

        for i in range(min(h, len(lines))):
            self._safe_addnstr(stdscr, y + i, x, lines[i].ljust(w), w)

    def _draw_details_status_split3(
        self,
        stdscr: "curses._CursesWindow",
        x: int,
        y: int,
        h: int,
        w: int,
        sv: SessionView,
        *,
        last_activity: Optional[str],
    ) -> None:
        # Top status block spans width; bottom has 3 columns:
        # Last User Send | Last Claw Send | Last Trigger
        status_attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        user_attr = curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        claw_attr = curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
        trig_attr = curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)

        status_h = max(7, min(12, h // 2))
        msg_h = max(0, h - status_h - 1)
        y_status = y
        y_sep = y_status + status_h
        y_msgs = y_sep + 1

        try:
            stdscr.hline(y_sep, x, curses.ACS_HLINE, max(0, w))
        except curses.error:
            pass

        # Status header + lines
        self._safe_addnstr(stdscr, y_status, x, _fit("Status", w), w, status_attr)
        markers = _agent_markers(sv.meta, self.model.config_snapshot)
        mark_str = f" ({','.join(markers)})" if markers else ""
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {sv.meta.agent_id}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}",
            f"UpdatedAt: {_fmt_dt(sv.updated_at)}",
            f"Transcript: {'MISSING' if sv.transcript_missing else ('-' if not sv.meta.session_file else 'OK')}",
            f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}",
        ]
        # Task summary (best-effort): show what the agent is working on right now.
        if sv.lock:
            task_src = sv.tail.last_user_send
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, w), max_lines=2)[:2])
            elif sv.tail.last_trigger and sv.tail.last_trigger.preview:
                status_lines.extend(_wrap_lines(f"Trigger: {redact_text(sv.tail.last_trigger.preview)}", max(10, w), max_lines=2)[:2])
            if sv.tail.last_assistant_thinking:
                think_lines = _wrap_lines(f"Thinking: {redact_text(sv.tail.last_assistant_thinking)}", max(10, w), max_lines=2)
                status_lines.extend(think_lines[:2])
            if sv.tail.last_tool_error:
                ts, summary = sv.tail.last_tool_error
                status_lines.append(f"Last tool error: {_fmt_dt(ts)} {redact_text(summary)}")
            if last_activity:
                status_lines.extend(_wrap_lines(f"Activity: {redact_text(last_activity)}", max(10, w), max_lines=1)[:1])
        acct_info = _channel_account_info(self.model.channels, channel=sv.meta.channel, account_id=sv.meta.account_id)
        if acct_info:
            in_at = _dt_from_ms(int(acct_info.get("lastInboundAt")) if isinstance(acct_info.get("lastInboundAt"), int) else None)
            out_at = _dt_from_ms(int(acct_info.get("lastOutboundAt")) if isinstance(acct_info.get("lastOutboundAt"), int) else None)
            status_lines.append(f"Channel IO: in={_fmt_dt(in_at)} out={_fmt_dt(out_at)} running={acct_info.get('running')}")
        if sv.telegram_binding:
            b = sv.telegram_binding
            note = " (ROUTED ELSEWHERE)" if sv.telegram_routed_elsewhere else ""
            status_lines.append(
                f"Telegram Binding: conv={b.conversation_id} -> {b.target_session_key} kind={b.target_kind or '-'} agent={b.agent_id or '-'}{note}"
            )
        if sv.lock:
            status_lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            status_lines.append("Lock: -")
        if sv.delivery_failure:
            status_lines.append(f"Delivery FAILED: retry={sv.delivery_failure.retry_count} err={redact_text(sv.delivery_failure.last_error or '-')}")
        alerts: List[str] = []
        if sv.computed.no_feedback:
            alerts.append("NO_FEEDBACK")
        if sv.computed.safety_alert:
            alerts.append("SAFETY")
        if sv.computed.safeguard_alert:
            alerts.append("SAFEGUARD_OFF")
        if alerts:
            status_lines.append("Alerts: " + ",".join(alerts))
        if sv.findings:
            status_lines.append(f"Diagnosis: [{sv.findings[0].severity}] {sv.findings[0].id}")
        else:
            status_lines.append("Diagnosis: (none)")

        for i in range(min(status_h - 1, len(status_lines))):
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(status_lines[i], w), w)

        if msg_h <= 2:
            return

        sep = 1
        col = (w - 2 * sep) // 3
        col1 = col
        col2 = col
        col3 = w - col1 - col2 - 2 * sep
        x1 = x
        x2 = x1 + col1 + sep
        x3 = x2 + col2 + sep

        try:
            stdscr.vline(y_msgs, x1 + col1, curses.ACS_VLINE, max(0, msg_h))
            stdscr.vline(y_msgs, x2 + col2, curses.ACS_VLINE, max(0, msg_h))
        except curses.error:
            pass

        self._safe_addnstr(stdscr, y_msgs, x1, _fit("Last User Send", col1), col1, user_attr)
        self._safe_addnstr(stdscr, y_msgs, x2, _fit("Last Claw Send", col2), col2, claw_attr)
        self._safe_addnstr(stdscr, y_msgs, x3, _fit("Last Trigger", col3), col3, trig_attr)

        # Body lines
        user_lines: List[str] = ["-"]
        if sv.tail.last_user_send:
            user_lines = [f"@ {_fmt_dt(sv.tail.last_user_send.ts)}", ""] + _wrap_lines(redact_text(sv.tail.last_user_send.preview), max(0, col1), max_lines=msg_h - 2)
        claw_lines: List[str] = ["-"]
        if sv.tail.last_assistant:
            claw_lines = [
                f"@ {_fmt_dt(sv.tail.last_assistant.ts)}",
                f"stop={sv.tail.last_assistant.stop_reason or '-'}",
                "",
            ] + _wrap_lines(redact_text(sv.tail.last_assistant.preview), max(0, col2), max_lines=msg_h - 3)
        trig_lines: List[str] = ["-"]
        if sv.tail.last_trigger:
            trig_lines = [f"@ {_fmt_dt(sv.tail.last_trigger.ts)}", ""] + _wrap_lines(redact_text(sv.tail.last_trigger.preview), max(0, col3), max_lines=msg_h - 2)

        body_h = msg_h - 1
        for i in range(min(body_h, len(user_lines))):
            self._safe_addnstr(stdscr, y_msgs + 1 + i, x1, _fit(user_lines[i], col1), col1)
        for i in range(min(body_h, len(claw_lines))):
            self._safe_addnstr(stdscr, y_msgs + 1 + i, x2, _fit(claw_lines[i], col2), col2)
        for i in range(min(body_h, len(trig_lines))):
            self._safe_addnstr(stdscr, y_msgs + 1 + i, x3, _fit(trig_lines[i], col3), col3)

    def _draw_details_status_stacked(
        self,
        stdscr: "curses._CursesWindow",
        x: int,
        y: int,
        h: int,
        w: int,
        sv: SessionView,
        *,
        last_activity: Optional[str],
    ) -> None:
        # Stacked panes: Status / Last User Send / Last Claw Send / Last Trigger
        status_attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        user_attr = curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        claw_attr = curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
        trig_attr = curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)

        status_h = max(7, min(12, h // 2))
        remaining = max(0, h - status_h - 3)  # 3 separators
        pane_h = max(3, remaining // 3) if remaining else 3
        user_h = pane_h
        claw_h = pane_h
        trig_h = max(3, remaining - user_h - claw_h) if remaining else 3

        y_status = y
        y_sep1 = y_status + status_h
        y_user = y_sep1 + 1
        y_sep2 = y_user + user_h
        y_claw = y_sep2 + 1
        y_sep3 = y_claw + claw_h
        y_trig = y_sep3 + 1

        try:
            stdscr.hline(y_sep1, x, curses.ACS_HLINE, max(0, w))
            stdscr.hline(y_sep2, x, curses.ACS_HLINE, max(0, w))
            stdscr.hline(y_sep3, x, curses.ACS_HLINE, max(0, w))
        except curses.error:
            pass

        # Status
        self._safe_addnstr(stdscr, y_status, x, _fit("Status", w), w, status_attr)
        markers = _agent_markers(sv.meta, self.model.config_snapshot)
        mark_str = f" ({','.join(markers)})" if markers else ""
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {sv.meta.agent_id}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}",
            f"UpdatedAt: {_fmt_dt(sv.updated_at)}",
            f"Transcript: {'MISSING' if sv.transcript_missing else ('-' if not sv.meta.session_file else 'OK')}",
            f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}",
        ]
        if sv.lock:
            task_src = sv.tail.last_user_send
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, w), max_lines=2)[:2])
            elif sv.tail.last_trigger and sv.tail.last_trigger.preview:
                status_lines.extend(_wrap_lines(f"Trigger: {redact_text(sv.tail.last_trigger.preview)}", max(10, w), max_lines=2)[:2])
            if sv.tail.last_assistant_thinking:
                status_lines.extend(
                    _wrap_lines(f"Thinking: {redact_text(sv.tail.last_assistant_thinking)}", max(10, w), max_lines=2)[:2]
                )
            if sv.tail.last_tool_error:
                ts, summary = sv.tail.last_tool_error
                status_lines.append(f"Last tool error: {_fmt_dt(ts)} {redact_text(summary)}")
            if last_activity:
                status_lines.extend(_wrap_lines(f"Activity: {redact_text(last_activity)}", max(10, w), max_lines=1)[:1])
        acct_info = _channel_account_info(self.model.channels, channel=sv.meta.channel, account_id=sv.meta.account_id)
        if acct_info:
            in_at = _dt_from_ms(int(acct_info.get("lastInboundAt")) if isinstance(acct_info.get("lastInboundAt"), int) else None)
            out_at = _dt_from_ms(int(acct_info.get("lastOutboundAt")) if isinstance(acct_info.get("lastOutboundAt"), int) else None)
            status_lines.append(f"Channel IO: in={_fmt_dt(in_at)} out={_fmt_dt(out_at)} running={acct_info.get('running')}")
        if sv.telegram_binding:
            b = sv.telegram_binding
            note = " (ROUTED ELSEWHERE)" if sv.telegram_routed_elsewhere else ""
            status_lines.append(
                f"Telegram Binding: conv={b.conversation_id} -> {b.target_session_key} kind={b.target_kind or '-'} agent={b.agent_id or '-'}{note}"
            )
        if sv.lock:
            status_lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            status_lines.append("Lock: -")
        if sv.findings:
            status_lines.append(f"Diagnosis: [{sv.findings[0].severity}] {sv.findings[0].id}")
        for i in range(min(status_h - 1, len(status_lines))):
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(status_lines[i], w), w)

        # Last User Send
        self._safe_addnstr(stdscr, y_user, x, _fit("Last User Send", w), w, user_attr)
        if sv.tail.last_user_send:
            self._safe_addnstr(stdscr, y_user + 1, x, _fit(f"@ {_fmt_dt(sv.tail.last_user_send.ts)}", w), w)
            msg_lines = _wrap_lines(redact_text(sv.tail.last_user_send.preview), max(0, w), max_lines=max(0, user_h - 2))
            for i, ln in enumerate(msg_lines[: max(0, user_h - 2)]):
                self._safe_addnstr(stdscr, y_user + 2 + i, x, _fit(ln, w), w)
        else:
            self._safe_addnstr(stdscr, y_user + 1, x, _fit("-", w), w)

        # Last Claw Send
        self._safe_addnstr(stdscr, y_claw, x, _fit("Last Claw Send", w), w, claw_attr)
        if sv.tail.last_assistant:
            self._safe_addnstr(
                stdscr,
                y_claw + 1,
                x,
                _fit(f"@ {_fmt_dt(sv.tail.last_assistant.ts)}  stop={sv.tail.last_assistant.stop_reason or '-'}", w),
                w,
            )
            msg_lines = _wrap_lines(redact_text(sv.tail.last_assistant.preview), max(0, w), max_lines=max(0, claw_h - 2))
            for i, ln in enumerate(msg_lines[: max(0, claw_h - 2)]):
                self._safe_addnstr(stdscr, y_claw + 2 + i, x, _fit(ln, w), w)
        else:
            self._safe_addnstr(stdscr, y_claw + 1, x, _fit("-", w), w)

        # Last Trigger
        self._safe_addnstr(stdscr, y_trig, x, _fit("Last Trigger", w), w, trig_attr)
        if sv.tail.last_trigger:
            self._safe_addnstr(stdscr, y_trig + 1, x, _fit(f"@ {_fmt_dt(sv.tail.last_trigger.ts)}", w), w)
            msg_lines = _wrap_lines(redact_text(sv.tail.last_trigger.preview), max(0, w), max_lines=max(0, trig_h - 2))
            for i, ln in enumerate(msg_lines[: max(0, trig_h - 2)]):
                self._safe_addnstr(stdscr, y_trig + 2 + i, x, _fit(ln, w), w)
        else:
            self._safe_addnstr(stdscr, y_trig + 1, x, _fit("-", w), w)

    def _template_picker(self, stdscr: "curses._CursesWindow") -> Optional[str]:
        items = list(TEMPLATES.keys())
        idx = 0
        scroll = 0
        h, w = stdscr.getmaxyx()
        win_h = min(10, h - 4)
        win_w = min(70, w - 4)
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        while True:
            win.clear()
            win.border()
            self._safe_addnstr(win, 0, 2, " Nudge template ", win_w - 4)
            visible = max(1, win_h - 2)
            if idx < scroll:
                scroll = idx
            if idx >= scroll + visible:
                scroll = idx - visible + 1
            view = items[scroll : scroll + visible]
            for i, name in enumerate(view):
                real_idx = scroll + i
                attr = curses.A_REVERSE if real_idx == idx else curses.A_NORMAL
                preview = TEMPLATES[name]
                self._safe_addnstr(win, 1 + i, 2, _pad_right_cells(f"{name}: {preview}", win_w - 4), win_w - 4, attr)
            win.refresh()
            ch = win.getch()
            if ch in (27, ord("q")):
                return None
            if ch in (curses.KEY_UP, ord("k")):
                idx = max(0, idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                idx = min(len(items) - 1, idx + 1)
            elif ch in (10, 13):
                return items[idx]

    def _help_overlay(self, stdscr: "curses._CursesWindow") -> None:
        lines = [
            "ClawMonitor TUI Help",
            "",
            "Navigation:",
            "  ↑/↓ (or k/j)   Select session",
            "  q or Esc       Quit",
            "",
            "Actions:",
            "  r              Refresh now",
            "  f              Cycle refresh interval (up to 10 minutes)",
            "  Enter          Send nudge (chat.send) using a template",
            "  e              Export redacted report (JSON+MD)",
            "  l              Toggle related logs panel",
            "  d              Re-run diagnosis (forces refresh)",
            "",
            "Health labels (FLAGS column):",
            "  OK     Normal / completed",
            "  RUN    Working (lock present)",
            "  IDLE   No user message seen",
            "  ALERT  Abnormal (NOFB / delivery failure / safety / safeguard off / interrupted)",
            "",
            "Notes:",
            "  - Related Logs require Gateway logs.tail (online mode).",
            "  - Reports/logs are redacted, but review before sharing.",
            "",
            "Press any key to close.",
        ]

        h, w = stdscr.getmaxyx()
        win_h = min(max(12, len(lines) + 2), max(6, h - 4))
        win_w = min(92, max(20, w - 4))
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        win.timeout(-1)
        scroll = 0
        while True:
            win.clear()
            win.border()
            try:
                self._safe_addnstr(win, 0, 2, " Help ", win_w - 4)
            except curses.error:
                pass
            view = lines[scroll : scroll + (win_h - 2)]
            for i, ln in enumerate(view):
                try:
                    self._safe_addnstr(win, 1 + i, 2, _pad_right_cells(ln, win_w - 4), win_w - 4)
                except curses.error:
                    pass
            win.refresh()
            ch = win.getch()
            if ch in (-1, 27, ord("q"), ord("?"), 10, 13):
                return
            if ch in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                scroll = min(max(0, len(lines) - (win_h - 2)), scroll + 1)

    def _export_report(self, sv: SessionView) -> None:
        rel = related_logs(self.model.gateway_log_tailer.lines, sv.meta.key, sv.meta.channel, sv.meta.account_id, limit=self.cfg.report_max_log_lines)
        summary = {
            "agent_id": sv.meta.agent_id,
            "channel": sv.meta.channel,
            "account_id": sv.meta.account_id,
            "state": sv.computed.state.value,
            "no_feedback": sv.computed.no_feedback,
            "delivery_failed": sv.delivery_failure is not None,
            "last_user_at": _fmt_dt(sv.tail.last_user.ts if sv.tail.last_user else None),
            "last_assistant_at": _fmt_dt(sv.tail.last_assistant.ts if sv.tail.last_assistant else None),
        }
        paths = write_report_files(
            session_key=sv.meta.key,
            summary=summary,
            findings=sv.findings,
            related_logs=rel,
            max_log_lines=self.cfg.report_max_log_lines,
            formats=["json", "md"],
        )
        for fmt, path in paths.items():
            self.elog.write("report.written", sessionKey=sv.meta.key, format=fmt, path=str(path))

    def _main(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        try:
            if curses.has_colors():
                curses.start_color()
                try:
                    curses.use_default_colors()
                except Exception:
                    pass
                curses.init_pair(1, curses.COLOR_GREEN, -1)
                curses.init_pair(2, curses.COLOR_YELLOW, -1)
                curses.init_pair(3, curses.COLOR_CYAN, -1)
                curses.init_pair(4, curses.COLOR_RED, -1)
                self._colors_enabled = True
                self._color_ok = curses.color_pair(1)
                self._color_working = curses.color_pair(3)
                self._color_idle = curses.color_pair(2)
                self._color_alert = curses.color_pair(4)
        except Exception:
            self._colors_enabled = False
        stdscr.timeout(200)
        stdscr.keypad(True)
        last_refresh = 0.0
        dirty = True
        while True:
            now = time.time()
            if now - last_refresh >= self.refresh_seconds:
                self.model.refresh()
                last_refresh = now
                self._last_refresh_at = now
                dirty = True

            sessions = self.model.sessions
            if sessions and self.selected >= len(sessions):
                self.selected = len(sessions) - 1
            if self.selected < 0:
                self.selected = 0

            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1
            if ch == -1:
                if dirty:
                    # fall through to redraw
                    pass
                else:
                    continue
            if ch in (ord("q"), 27):
                return
            if ch in (curses.KEY_UP, ord("k")):
                self.selected = max(0, self.selected - 1)
                dirty = True
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.selected = min(len(sessions) - 1, self.selected + 1) if sessions else 0
                dirty = True
            elif ch == ord("r"):
                self.model.refresh()
                self._last_refresh_at = time.time()
                dirty = True
            elif ch == ord("l"):
                self.show_logs = not self.show_logs
                dirty = True
            elif ch == ord("d"):
                # Diagnosis is recomputed each refresh; force refresh.
                self.model.refresh()
                self._last_refresh_at = time.time()
                dirty = True
            elif ch == ord("f"):
                opts = [1.0, 2.0, 5.0, 10.0, 30.0, 60.0, 300.0, 600.0]
                cur = float(self.refresh_seconds)
                try:
                    i = opts.index(cur)
                except ValueError:
                    i = 2
                self.refresh_seconds = opts[(i + 1) % len(opts)]
                dirty = True
            elif ch == ord("?"):
                self._help_overlay(stdscr)
                dirty = True
            elif ch in (ord("e"), 10, 13):
                sv = sessions[self.selected] if sessions else None
                if ch == ord("e") and sv:
                    self._export_report(sv)
                    dirty = True
                elif ch in (10, 13) and sv:
                    tmpl = self._template_picker(stdscr)
                    if tmpl:
                        self.elog.write("nudge.sent", sessionKey=sv.meta.key, template=tmpl)
                        res = send_nudge(self.cfg.openclaw_bin, sv.meta.key, tmpl, deliver=True)
                        self.elog.write(
                            "nudge.result",
                            sessionKey=sv.meta.key,
                            ok=res.ok,
                            runId=res.run_id or "",
                            status=res.status or "",
                            error=res.error or "",
                        )
                    dirty = True

            if not dirty:
                continue

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            self._draw_header(stdscr, w)

            list_h = h - 3
            list_w = max(52, min(max(52, int(w * 0.55)), max(0, w - 24)))
            detail_w = w - list_w - 1
            self._draw_list(stdscr, y=2, h=list_h, w=list_w, sessions=sessions)
            sv = sessions[self.selected] if sessions else None
            if detail_w >= 24 and list_w < w - 1:
                try:
                    stdscr.vline(2, list_w, curses.ACS_VLINE, max(0, h - 3))
                except curses.error:
                    pass
                self._draw_details(stdscr, x=list_w + 1, y=2, h=h - 3, w=detail_w, sv=sv)
            else:
                self._safe_addnstr(
                    stdscr,
                    h - 2,
                    0,
                    "Terminal too narrow for details panel. Widen window or use `clawmonitor status`.".ljust(w),
                    w,
                )

            refresh_age = "-"
            if self._last_refresh_at is not None:
                refresh_age = _fmt_age(int(time.time() - self._last_refresh_at))
            footer = (
                f"[q]quit [?]help [↑↓]select [r]refresh [f]interval={int(self.refresh_seconds)}s "
                f"[Enter]nudge [e]export [l]logs  sel={self.selected+1}/{len(sessions)} lastRefresh={refresh_age}"
            )
            self._safe_addnstr(stdscr, h - 1, 0, footer.ljust(w), w, curses.A_REVERSE)

            stdscr.refresh()
            dirty = False
