from __future__ import annotations

import curses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time
import re

from .actions import TEMPLATES, send_nudge
from .channels_status import ChannelsSnapshot, fetch_channels_status
from .config import Config
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .diagnostics import Finding, diagnose, related_logs
from .eventlog import EventLog
from .gateway_logs import GatewayLogTailer
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .redact import redact_text
from .reports import write_report_files
from .session_store import SessionMeta, list_sessions
from .state import SessionComputed, WorkState, compute_state
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
    if len(text) <= width:
        return text.ljust(width)
    if width <= 1:
        return text[:width]
    return (text[: width - 1] + "…")[:width]


def _wrap_lines(text: str, width: int, max_lines: int) -> List[str]:
    if width <= 0 or max_lines <= 0:
        return []
    t = (text or "").replace("\t", " ").strip()
    if not t:
        return []
    words = t.split()
    # If there are no whitespace splits (common for CJK), treat as a single chunk.
    if len(words) <= 1 and len(t) > width:
        words = [t]
    lines: List[str] = []
    cur: List[str] = []
    cur_len = 0
    for w in words:
        if len(w) > width:
            # Break long tokens into width-sized chunks to avoid a single unwrapped line.
            if cur:
                lines.append(" ".join(cur)[:width])
                cur = []
                cur_len = 0
                if len(lines) >= max_lines:
                    break
            for i in range(0, len(w), width):
                lines.append(w[i : i + width])
                if len(lines) >= max_lines:
                    break
            continue
        if not cur:
            cur = [w]
            cur_len = len(w)
        elif cur_len + 1 + len(w) <= width:
            cur.append(w)
            cur_len += 1 + len(w)
        else:
            lines.append(" ".join(cur)[:width])
            cur = [w]
            cur_len = len(w)
        if len(lines) >= max_lines:
            break
    if len(lines) < max_lines and cur:
        lines.append(" ".join(cur)[:width])
    if len(lines) == max_lines:
        last = lines[-1]
        if last and not last.endswith("…") and width >= 1 and len(last) == width:
            lines[-1] = (last[: max(0, width - 1)] + "…")[:width]
    return lines


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


def _agent_markers(meta: SessionMeta) -> List[str]:
    markers: List[str] = []
    kind = ((meta.kind or "") + " " + (meta.chat_type or "")).lower()
    aid = (meta.agent_id or "").lower()
    if "subagent" in kind or "sub-agent" in kind:
        markers.append("SUBAG")
    if "codex" in kind or aid.startswith("codex") or "codex" in aid:
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

    @property
    def gateway_log_tailer(self) -> GatewayLogTailer:
        return self._gateway_logs

    @property
    def channels(self) -> Optional[ChannelsSnapshot]:
        return self._channels

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
            return TranscriptTail(None, None, None, None)
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
            return TranscriptTail(None, None, None, None)

    def refresh(self) -> None:
        self._refresh_delivery_map()
        self._refresh_gateway_logs()
        self._refresh_channels()

        metas = list_sessions(self.cfg.openclaw_root)
        views: List[SessionView] = []
        for meta in metas:
            if self.cfg.hide_system_sessions and meta.system_sent:
                continue
            tail = self._tail_for(meta.session_file)
            lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
            df = self._delivery_map.get(meta.key)
            computed = compute_state(meta.aborted_last_run, tail, lock, df)
            findings = diagnose(
                session_key=meta.key,
                channel=meta.channel,
                account_id=meta.account_id,
                delivery_failed=df is not None,
                no_feedback=computed.no_feedback,
                is_working=computed.state == WorkState.WORKING,
                gateway_lines=self._gateway_logs.lines,
            )
            views.append(SessionView(meta=meta, tail=tail, lock=lock, delivery_failure=df, computed=computed, findings=findings))
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
        maxw = min(width, w - x)
        if maxw <= 0:
            return
        text = _sanitize_for_curses(text)
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
            u_age = _fmt_age(_age_seconds(sv.tail.last_user.ts if sv.tail.last_user else None))
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
            flags.extend(_agent_markers(sv.meta))
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
        if self.show_logs:
            rel = related_logs(self.model.gateway_log_tailer.lines, sv.meta.key, sv.meta.channel, sv.meta.account_id, limit=50)
            rel_logs = [ln.text for ln in rel][-20:]

        log_budget = 0
        if self.show_logs:
            log_budget = min(10, max(0, h // 3))
        detail_h = max(0, h - (log_budget + (1 if log_budget else 0)))

        # Clear details region first.
        for j in range(detail_h):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)

        if w >= 90 and detail_h >= 10:
            self._draw_details_3col(stdscr, x=x, y=y, h=detail_h, w=w, sv=sv)
        elif w >= 24 and detail_h >= 12:
            self._draw_details_3pane(stdscr, x=x, y=y, h=detail_h, w=w, sv=sv)
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
        markers = _agent_markers(sv.meta)
        mark_str = f" ({','.join(markers)})" if markers else ""
        lines.append(f"Agent: {sv.meta.agent_id}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}")
        if sv.meta.kind or sv.meta.chat_type:
            lines.append(f"Kind: {sv.meta.kind or '-'}  ChatType: {sv.meta.chat_type or '-'}")
        lines.append(f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}")
        if sv.lock:
            lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            lines.append("Lock: -")
        lines.append(f"abortedLastRun: {sv.meta.aborted_last_run}  systemSent: {sv.meta.system_sent}")
        if sv.delivery_failure:
            lines.append(f"Delivery FAILED: retry={sv.delivery_failure.retry_count} err={redact_text(sv.delivery_failure.last_error or '-')}")
        lines.append("")
        if sv.tail.last_user:
            lines.append(f"Last USER @ {_fmt_dt(sv.tail.last_user.ts)}")
            for part in _wrap_lines(redact_text(sv.tail.last_user.preview), max(0, w - 2), max_lines=3):
                lines.append(f"  {part}")
        else:
            lines.append("Last USER: -")
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

    def _draw_details_3col(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: SessionView) -> None:
        # Three columns: Status | Last User Send | Last Claw Send
        sep = 1
        col1 = max(22, int(w * 0.34))
        col2 = max(22, int(w * 0.33))
        col3 = max(22, w - col1 - col2 - sep * 2)
        if col3 < 22:
            # Fallback: rebalance a bit
            col1 = max(22, int(w * 0.33))
            col2 = max(22, int(w * 0.33))
            col3 = max(22, w - col1 - col2 - sep * 2)
        x1 = x
        x2 = x + col1 + sep
        x3 = x2 + col2 + sep

        # Separators
        try:
            stdscr.vline(y, x + col1, curses.ACS_VLINE, max(0, h))
            stdscr.vline(y, x2 + col2, curses.ACS_VLINE, max(0, h))
        except curses.error:
            pass

        # Column headers
        self._safe_addnstr(stdscr, y, x1, _fit("Status", col1).ljust(col1), col1, curses.A_BOLD)
        self._safe_addnstr(stdscr, y, x2, _fit("Last User Send", col2).ljust(col2), col2, curses.A_BOLD)
        self._safe_addnstr(stdscr, y, x3, _fit("Last Claw Send", col3).ljust(col3), col3, curses.A_BOLD)

        # Status column content
        markers = _agent_markers(sv.meta)
        mark_str = f" ({','.join(markers)})" if markers else ""
        status_lines: List[str] = [
            f"session: {sv.meta.key}",
            f"agent: {sv.meta.agent_id}{mark_str}",
            f"chan: {sv.meta.channel or '-'}  acct: {sv.meta.account_id or '-'}",
        ]
        if sv.meta.kind or sv.meta.chat_type:
            status_lines.append(f"kind: {sv.meta.kind or '-'}")
        status_lines.append(f"state: {sv.computed.state.value}")
        status_lines.append(f"reason: {sv.computed.reason}")
        if sv.lock:
            status_lines.append(f"lock: pid={sv.lock.pid} alive={sv.lock.pid_alive}")
            status_lines.append(f"      at {_fmt_dt(sv.lock.created_at)}")
        else:
            status_lines.append("lock: -")
        if sv.delivery_failure:
            status_lines.append(f"delivery: FAILED x{sv.delivery_failure.retry_count}")
        if sv.computed.no_feedback:
            status_lines.append("alert: NO_FEEDBACK")
        if sv.computed.safety_alert:
            status_lines.append("alert: SAFETY")
        if sv.computed.safeguard_alert:
            status_lines.append("alert: SAFEGUARD_OFF")

        # Diagnosis (top few)
        status_lines.append("")
        status_lines.append("diagnosis:")
        if sv.findings:
            for f in sv.findings[:4]:
                status_lines.append(f"- {f.severity} {f.id}")
        else:
            status_lines.append("- (none)")

        # User column content
        user_lines: List[str] = []
        if sv.tail.last_user:
            user_lines.append(f"at: {_fmt_dt(sv.tail.last_user.ts)}")
            user_lines.append("")
            user_lines.extend(_wrap_lines(redact_text(sv.tail.last_user.preview), max(0, col2 - 2), max_lines=max(1, h - 4)))
        else:
            user_lines.append("-")

        # Assistant column content
        asst_lines: List[str] = []
        if sv.tail.last_assistant:
            asst_lines.append(f"at: {_fmt_dt(sv.tail.last_assistant.ts)}")
            asst_lines.append(f"stop: {sv.tail.last_assistant.stop_reason or '-'}")
            asst_lines.append("")
            asst_lines.extend(_wrap_lines(redact_text(sv.tail.last_assistant.preview), max(0, col3 - 2), max_lines=max(1, h - 5)))
        else:
            asst_lines.append("-")

        # Draw columns (start at y+1)
        max_body = max(0, h - 1)
        for i in range(min(max_body, len(status_lines))):
            self._safe_addnstr(stdscr, y + 1 + i, x1, _fit(status_lines[i], col1), col1)
        for i in range(min(max_body, len(user_lines))):
            self._safe_addnstr(stdscr, y + 1 + i, x2, _fit(user_lines[i], col2), col2)
        for i in range(min(max_body, len(asst_lines))):
            self._safe_addnstr(stdscr, y + 1 + i, x3, _fit(asst_lines[i], col3), col3)

    def _draw_details_3pane(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: SessionView) -> None:
        # Three stacked panes: Status / Last User Send / Last Claw Send
        status_h = max(6, min(12, h // 2))
        remaining = max(0, h - status_h - 2)  # 2 separators
        user_h = max(3, remaining // 2)
        asst_h = max(3, remaining - user_h)

        y_status = y
        y_sep1 = y_status + status_h
        y_user = y_sep1 + 1
        y_sep2 = y_user + user_h
        y_asst = y_sep2 + 1

        # separators
        try:
            stdscr.hline(y_sep1, x, curses.ACS_HLINE, max(0, w))
            stdscr.hline(y_sep2, x, curses.ACS_HLINE, max(0, w))
        except curses.error:
            pass

        # STATUS pane
        self._safe_addnstr(stdscr, y_status, x, _fit("Status", w), w, curses.A_BOLD)
        markers = _agent_markers(sv.meta)
        mark_str = f" ({','.join(markers)})" if markers else ""
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {sv.meta.agent_id}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}",
        ]
        if sv.meta.kind or sv.meta.chat_type:
            status_lines.append(f"Kind: {sv.meta.kind or '-'}  ChatType: {sv.meta.chat_type or '-'}")
        status_lines.append(f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}")
        if sv.lock:
            status_lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            status_lines.append("Lock: -")
        if sv.delivery_failure:
            status_lines.append(f"Delivery FAILED: retry={sv.delivery_failure.retry_count} err={redact_text(sv.delivery_failure.last_error or '-')}")
        flags: List[str] = []
        if sv.computed.no_feedback:
            flags.append("NO_FEEDBACK")
        if sv.computed.safety_alert:
            flags.append("SAFETY")
        if sv.computed.safeguard_alert:
            flags.append("SAFEGUARD_OFF")
        if flags:
            status_lines.append("Alerts: " + ",".join(flags))
        # Diagnosis short
        if sv.findings:
            status_lines.append("Diagnosis: " + f"[{sv.findings[0].severity}] {sv.findings[0].id}")
        else:
            status_lines.append("Diagnosis: (none)")

        body_h = max(0, status_h - 1)
        for i in range(min(body_h, len(status_lines))):
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(status_lines[i], w), w)

        # USER pane
        self._safe_addnstr(stdscr, y_user, x, _fit("Last User Send", w), w, curses.A_BOLD)
        if sv.tail.last_user:
            self._safe_addnstr(stdscr, y_user + 1, x, _fit(f"@ {_fmt_dt(sv.tail.last_user.ts)}", w), w)
            msg_lines = _wrap_lines(redact_text(sv.tail.last_user.preview), max(0, w), max_lines=max(0, user_h - 2))
            for i, ln in enumerate(msg_lines[: max(0, user_h - 2)]):
                self._safe_addnstr(stdscr, y_user + 2 + i, x, _fit(ln, w), w)
        else:
            self._safe_addnstr(stdscr, y_user + 1, x, _fit("-", w), w)

        # ASSISTANT pane
        self._safe_addnstr(stdscr, y_asst, x, _fit("Last Claw Send", w), w, curses.A_BOLD)
        if sv.tail.last_assistant:
            self._safe_addnstr(stdscr, y_asst + 1, x, _fit(f"@ {_fmt_dt(sv.tail.last_assistant.ts)}  stop={sv.tail.last_assistant.stop_reason or '-'}", w), w)
            msg_lines = _wrap_lines(redact_text(sv.tail.last_assistant.preview), max(0, w), max_lines=max(0, asst_h - 2))
            for i, ln in enumerate(msg_lines[: max(0, asst_h - 2)]):
                self._safe_addnstr(stdscr, y_asst + 2 + i, x, _fit(ln, w), w)
        else:
            self._safe_addnstr(stdscr, y_asst + 1, x, _fit("-", w), w)

    def _template_picker(self, stdscr: "curses._CursesWindow") -> Optional[str]:
        items = list(TEMPLATES.keys())
        idx = 0
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
            win.addnstr(0, 2, " Nudge template ", win_w - 4)
            for i, name in enumerate(items[: win_h - 2]):
                attr = curses.A_REVERSE if i == idx else curses.A_NORMAL
                preview = TEMPLATES[name][: (win_w - 6)]
                win.addnstr(1 + i, 2, f"{name}: {preview}".ljust(win_w - 4), win_w - 4, attr)
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
                win.addnstr(0, 2, " Help ", win_w - 4)
            except curses.error:
                pass
            view = lines[scroll : scroll + (win_h - 2)]
            for i, ln in enumerate(view):
                try:
                    win.addnstr(1 + i, 2, ln.ljust(win_w - 4), win_w - 4)
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
