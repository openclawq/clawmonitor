from __future__ import annotations

import curses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
import time

from .actions import TEMPLATES, send_nudge
from .channels_status import ChannelsSnapshot, fetch_channels_status
from .config import Config
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .diagnostics import Finding, diagnose, related_logs
from .eventlog import EventLog
from .gateway_logs import GatewayLogTailer
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .redact import redact_text
from .reports import write_report
from .session_store import SessionMeta, list_sessions
from .state import SessionComputed, WorkState, compute_state
from .transcript_tail import TranscriptTail, tail_transcript


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

    def run(self) -> None:
        curses.wrapper(self._main)

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
        header = "AGENT  CHAN      STATE        U-AGE  A-AGE  RUN   FLAGS  SESSION"
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
            if sv.computed.no_feedback:
                flags.append("NOFB")
            if sv.delivery_failure:
                flags.append("DLV")
            if sv.lock and sv.lock.pid_alive is False:
                flags.append("ZLOCK")
            if sv.computed.safety_alert:
                flags.append("SAFE")
            flag_str = ",".join(flags)[:8]
            line = f"{sv.meta.agent_id[:5]:<5}  {(sv.meta.channel or '-')[:8]:<8}  {sv.computed.state.value:<11}  {u_age:>5}  {a_age:>5}  {run:>5}  {flag_str:<6}  {sv.meta.key}"
            attr = curses.A_REVERSE if idx == self.selected else curses.A_NORMAL
            self._safe_addnstr(stdscr, row_y, 0, line.ljust(w), w, attr)

    def _draw_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: Optional[SessionView]) -> None:
        if not sv:
            return
        lines: List[str] = []
        lines.append(f"SessionKey: {sv.meta.key}")
        lines.append(f"Agent: {sv.meta.agent_id}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}")
        lines.append(f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}")
        if sv.lock:
            lines.append(f"Lock: pid={sv.lock.pid} alive={sv.lock.pid_alive} createdAt={_fmt_dt(sv.lock.created_at)}")
        else:
            lines.append("Lock: -")
        lines.append(f"abortedLastRun: {sv.meta.aborted_last_run}  systemSent: {sv.meta.system_sent}")
        lines.append("")
        if sv.tail.last_user:
            lines.append(f"Last USER @ {_fmt_dt(sv.tail.last_user.ts)}")
            lines.append(f"  {redact_text(sv.tail.last_user.preview)}")
        else:
            lines.append("Last USER: -")
        lines.append("")
        if sv.tail.last_assistant:
            lines.append(f"Last ASST @ {_fmt_dt(sv.tail.last_assistant.ts)}  stopReason={sv.tail.last_assistant.stop_reason or '-'}")
            lines.append(f"  {redact_text(sv.tail.last_assistant.preview)}")
        else:
            lines.append("Last ASST: -")
        if sv.delivery_failure:
            lines.append("")
            lines.append(f"Delivery FAILED: retry={sv.delivery_failure.retry_count} err={redact_text(sv.delivery_failure.last_error or '-')}")
        lines.append("")
        lines.append("Diagnosis:")
        if sv.findings:
            for f in sv.findings[:6]:
                lines.append(f"- [{f.severity}] {f.id}: {f.summary}")
        else:
            lines.append("- (none)")

        rel_logs: List[str] = []
        if self.show_logs:
            rel = related_logs(self.model.gateway_log_tailer.lines, sv.meta.key, sv.meta.channel, sv.meta.account_id, limit=50)
            rel_logs = [ln.text for ln in rel][-20:]

        max_lines = h
        for i in range(min(max_lines, len(lines))):
            self._safe_addnstr(stdscr, y + i, x, lines[i].ljust(w), w)
        for j in range(len(lines), max_lines):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)

        if self.show_logs:
            log_y = y + min(max_lines, len(lines))
            if log_y < y + h - 1:
                self._safe_addnstr(stdscr, log_y, x, "Related Logs:".ljust(w), w, curses.A_BOLD)
                log_y += 1
                for ln in rel_logs[: (y + h - log_y)]:
                    if log_y >= y + h:
                        break
                    self._safe_addnstr(stdscr, log_y, x, ln[:w].ljust(w), w)
                    log_y += 1

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
        path = write_report(
            session_key=sv.meta.key,
            summary=summary,
            findings=sv.findings,
            related_logs=rel,
            max_log_lines=self.cfg.report_max_log_lines,
        )
        self.elog.write("report.written", sessionKey=sv.meta.key, path=str(path))

    def _main(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        stdscr.nodelay(True)
        stdscr.keypad(True)
        last_refresh = 0.0
        while True:
            now = time.time()
            if now - last_refresh >= self.cfg.ui_seconds:
                self.model.refresh()
                last_refresh = now

            sessions = self.model.sessions
            if sessions and self.selected >= len(sessions):
                self.selected = len(sessions) - 1
            if self.selected < 0:
                self.selected = 0

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            self._draw_header(stdscr, w)

            list_h = h - 2
            list_w = max(40, min(max(40, int(w * 0.55)), max(0, w - 20)))
            detail_w = w - list_w - 1
            self._draw_list(stdscr, y=2, h=list_h, w=list_w, sessions=sessions)
            sv = sessions[self.selected] if sessions else None
            if detail_w >= 20 and list_w < w - 1:
                try:
                    stdscr.vline(2, list_w, curses.ACS_VLINE, max(0, h - 2))
                except curses.error:
                    pass
                self._draw_details(stdscr, x=list_w + 1, y=2, h=h - 2, w=detail_w, sv=sv)
            else:
                # Terminal too narrow; show a one-line hint.
                self._safe_addnstr(stdscr, h - 1, 0, "Terminal too narrow for details panel. Widen window or use `clawmonitor status`.".ljust(w), w)

            stdscr.refresh()

            try:
                ch = stdscr.getch()
            except Exception:
                ch = -1
            if ch == -1:
                time.sleep(0.05)
                continue
            if ch in (ord("q"), 27):
                return
            if ch in (curses.KEY_UP, ord("k")):
                self.selected = max(0, self.selected - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                self.selected = min(len(sessions) - 1, self.selected + 1) if sessions else 0
            elif ch == ord("r"):
                self.model.refresh()
            elif ch == ord("l"):
                self.show_logs = not self.show_logs
            elif ch == ord("d"):
                # Diagnosis is recomputed each refresh; force refresh.
                self.model.refresh()
            elif ch == ord("e") and sv:
                self._export_report(sv)
            elif ch in (10, 13) and sv:
                tmpl = self._template_picker(stdscr)
                if tmpl:
                    self.elog.write("nudge.sent", sessionKey=sv.meta.key, template=tmpl)
                    res = send_nudge(self.cfg.openclaw_bin, sv.meta.key, tmpl, deliver=True)
                    self.elog.write("nudge.result", sessionKey=sv.meta.key, ok=res.ok, runId=res.run_id or "", status=res.status or "", error=res.error or "")
