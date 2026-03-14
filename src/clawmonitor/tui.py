from __future__ import annotations

import curses
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple, Union
import time
import re
import threading
import unicodedata
import os

def _load_loading_art_lines() -> List[str]:
    """
    Load the TUI loading splash art.

    Precedence:
      1) $CLAWMONITOR_LOADING_ART (if set)
      2) docs/loadingart.txt in a source checkout (editable installs)
      3) packaged resource clawmonitor.assets/loadingart.txt
      4) small built-in fallback
    """
    env_path = os.environ.get("CLAWMONITOR_LOADING_ART", "").strip()
    if env_path:
        try:
            p = Path(env_path).expanduser()
            if p.exists():
                return p.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception:
            pass

    # Try to locate docs/loadingart.txt in a source tree (best-effort).
    try:
        here = Path(__file__).resolve()
        for parent in list(here.parents)[:6]:
            cand = parent / "docs" / "loadingart.txt"
            if cand.exists():
                return cand.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        pass

    try:
        import importlib.resources as resources

        art = resources.files("clawmonitor.assets").joinpath("loadingart.txt").read_text(encoding="utf-8")
        return art.splitlines()
    except Exception:
        return [
            "ClawMonitor",
            "",
            "loading…",
        ]

from .actions import TEMPLATES, send_nudge
from .acpx_sessions import AcpxSnapshot, acpx_is_working, acpx_session_path, load_acpx_snapshot, tail_acpx_messages
from .channels_status import ChannelsSnapshot, fetch_channels_status
from .config import Config
from .delivery_queue import DeliveryFailure, load_failed_delivery_map
from .diagnostics import Evidence, Finding, diagnose, related_logs
from .eventlog import EventLog
from .gateway_logs import GatewayLogTailer
from .config import write_labels
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .openclaw_config import OpenClawConfigSnapshot, read_openclaw_config_snapshot
from .openclaw_cron import CronJob, CronRunStatus, CronSnapshot, match_cron_job, read_cron_last_runs, read_cron_snapshot
from .labels import has_user_label, session_display_label
from .redact import redact_text
from .session_keys import parse_session_key
from .reports import write_report_files
from .session_store import SessionMeta, list_sessions
from .state import SessionComputed, WorkState, WorkingSignal, compute_state
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


def _tail_suffix(session_key: str, *, n: int = 4) -> str:
    key = (session_key or "").strip()
    if not key:
        return ""
    tail = key.split(":")[-1].strip()
    if not tail:
        return ""
    if len(tail) <= n:
        return tail
    return tail[-n:]


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


def _internal_activity_at(tail: TranscriptTail) -> Optional[datetime]:
    """
    Best-effort "agent is doing something" timestamp.

    Prefer assistant/tool activity. Avoid treating generic role=user wrapper
    messages as activity; they can be control-plane injections.
    """
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
    working: Optional[WorkingSignal]
    acpx: Optional[AcpxSnapshot]
    delivery_failure: Optional[DeliveryFailure]
    computed: SessionComputed
    findings: List[Finding]
    updated_at: Optional[datetime]
    transcript_missing: bool
    telegram_binding: Optional[TelegramThreadBinding]
    telegram_routed_elsewhere: bool
    # Lightweight "silent gap" metrics:
    # - human_out_at: last human-visible outbound send time (channel-level)
    # - internal_activity_at: last internal activity timestamp from transcript tail (assistant/tool/non-message)
    human_out_at: Optional[datetime]
    internal_activity_at: Optional[datetime]


class MonitorModel:
    def __init__(self, cfg: Config, elog: EventLog) -> None:
        self.cfg = cfg
        self.elog = elog
        self._sessions: List[SessionView] = []
        self._sessions_lock = threading.Lock()
        self._findings_by_key: Dict[str, List[Finding]] = {}
        self._tail_cache: Dict[Path, Tuple[float, int, TranscriptTail]] = {}
        self._acpx_cache: Dict[Path, Tuple[float, int, Optional[AcpxSnapshot], TranscriptTail]] = {}
        self._tail_key_cache: Dict[str, Tuple[Optional[int], Optional[str], float, TranscriptTail]] = {}
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
        self._cron_snapshot: Optional[CronSnapshot] = None
        self._cron_snapshot_last_load = 0.0
        self._cron_last_runs: Dict[str, CronRunStatus] = {}
        self._cron_last_runs_last_load = 0.0

    @property
    def gateway_log_tailer(self) -> GatewayLogTailer:
        return self._gateway_logs

    @property
    def channels(self) -> Optional[ChannelsSnapshot]:
        with self._sessions_lock:
            return self._channels

    @property
    def config_snapshot(self) -> Optional[OpenClawConfigSnapshot]:
        with self._sessions_lock:
            return self._cfg_snapshot

    @property
    def cron_snapshot(self) -> Optional[CronSnapshot]:
        with self._sessions_lock:
            return self._cron_snapshot

    @property
    def cron_last_runs(self) -> Dict[str, CronRunStatus]:
        with self._sessions_lock:
            return dict(self._cron_last_runs)

    @property
    def sessions(self) -> List[SessionView]:
        with self._sessions_lock:
            return list(self._sessions)

    def set_findings(self, session_key: str, findings: List[Finding]) -> None:
        with self._sessions_lock:
            self._findings_by_key[session_key] = list(findings)

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
        with self._sessions_lock:
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

    def _acpx_tail_for(self, acpx_session_id: str) -> Tuple[Optional[AcpxSnapshot], TranscriptTail]:
        path = acpx_session_path(acpx_session_id)
        if not path.exists():
            return None, TranscriptTail(None, None, None, None, None, None, None)
        try:
            st = path.stat()
            cached = self._acpx_cache.get(path)
            if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
                return cached[2], cached[3]
            snap, doc = load_acpx_snapshot(acpx_session_id)
            tail = tail_acpx_messages(doc) if doc else TranscriptTail(None, None, None, None, None, None, None)
            self._acpx_cache[path] = (st.st_mtime, st.st_size, snap, tail)
            return snap, tail
        except Exception:
            return None, TranscriptTail(None, None, None, None, None, None, None)

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
            snap = read_openclaw_config_snapshot(self.cfg.openclaw_root)
        except Exception:
            snap = None
        with self._sessions_lock:
            self._cfg_snapshot = snap
            self._cfg_snapshot_last_load = now

    def _refresh_cron_snapshot(self) -> None:
        now = time.time()
        if now - self._cron_snapshot_last_load < 2.0:
            return
        try:
            snap = read_cron_snapshot(self.cfg.openclaw_root)
        except Exception:
            snap = None
        with self._sessions_lock:
            self._cron_snapshot = snap
            self._cron_snapshot_last_load = now

    def _refresh_cron_last_runs(self) -> None:
        now = time.time()
        if now - self._cron_last_runs_last_load < 2.0:
            return
        try:
            runs = read_cron_last_runs(self.cfg.openclaw_root)
        except Exception:
            runs = {}
        with self._sessions_lock:
            self._cron_last_runs = runs
            self._cron_last_runs_last_load = now

    def _tail_for_meta(self, meta: SessionMeta, *, lock_present: bool) -> TranscriptTail:
        """
        Avoid re-statting/re-reading JSONL on every refresh by using sessions.json
        updatedAt as a cheap change detector.
        """
        key = meta.key
        updated = meta.updated_at_ms
        path_str = str(meta.session_file) if meta.session_file else None
        cached = self._tail_key_cache.get(key)
        now = time.time()
        if cached:
            last_updated, last_path, last_tailed_at, tail = cached
            if last_updated == updated and last_path == path_str:
                if not lock_present:
                    return tail
                # When working, re-tail with a small TTL to keep "last assistant" fresh.
                if now - last_tailed_at < 3.0:
                    return tail
        tail = self._tail_for(meta.session_file)
        self._tail_key_cache[key] = (updated, path_str, now, tail)
        return tail

    def _telegram_binding_for(self, *, account_id: Optional[str], to: Optional[str]) -> Optional[TelegramThreadBinding]:
        if not to or not isinstance(to, str) or not to.startswith("telegram:"):
            return None
        conv_id = to.split("telegram:", 1)[1].strip()
        if not conv_id:
            return None
        aid = (account_id or "default").strip() or "default"
        return (self._telegram_bindings.get(aid) or {}).get(conv_id)

    def refresh(self, *, progress: Optional[Callable[[str, int, int], None]] = None) -> None:
        def tick(msg: str, i: int, total: int) -> None:
            if progress:
                progress(msg, i, total)

        tick("Loading delivery queue…", 1, 8)
        self._refresh_delivery_map()
        tick("Tailing gateway logs…", 2, 8)
        self._refresh_gateway_logs()
        tick("Reading channels status…", 3, 8)
        self._refresh_channels()
        tick("Loading telegram bindings…", 4, 8)
        self._refresh_telegram_bindings()
        tick("Reading openclaw.json…", 5, 8)
        self._refresh_config_snapshot()
        tick("Reading cron jobs…", 6, 8)
        self._refresh_cron_snapshot()
        self._refresh_cron_last_runs()

        tick("Listing sessions…", 7, 8)
        metas = list_sessions(self.cfg.openclaw_root)
        views: List[SessionView] = []
        total = max(1, len(metas))
        for idx, meta in enumerate(metas):
            if self.cfg.hide_system_sessions and meta.system_sent:
                continue
            if progress and (idx % 12 == 0):
                tick(f"Tailing transcripts… ({idx+1}/{total})", 8, 8)
            lock = read_lock(lock_path_for_session_file(meta.session_file)) if meta.session_file else None
            acpx: Optional[AcpxSnapshot] = None
            tail = self._tail_for_meta(meta, lock_present=bool(lock))
            if (not meta.session_file or not meta.session_file.exists()) and meta.acpx_session_id:
                acpx, tail = self._acpx_tail_for(meta.acpx_session_id)
            df = self._delivery_map.get(meta.key)
            safeguard_ok = True
            try:
                snap = self.config_snapshot
                if snap:
                    compaction_cfg = snap.compaction_by_agent.get(meta.agent_id) or snap.compaction_by_agent.get("main")
                    safeguard_ok = bool(compaction_cfg and compaction_cfg.mode == "safeguard")
            except Exception:
                safeguard_ok = True
            working: Optional[WorkingSignal] = None
            if lock is None and meta.acp_state in ("running", "pending"):
                if acpx is None:
                    working = WorkingSignal(kind="acp", created_at=_dt_from_ms(meta.updated_at_ms), pid=None, pid_alive=None)
                elif acpx_is_working(acpx):
                    created_at = acpx.last_prompt_at or acpx.last_used_at or acpx.updated_at or _dt_from_ms(meta.updated_at_ms)
                    working = WorkingSignal(kind="acp", created_at=created_at, pid=acpx.pid, pid_alive=None)
            computed = compute_state(meta.aborted_last_run, tail, lock, df, safeguard_ok=safeguard_ok, working=working)
            with self._sessions_lock:
                findings = list(self._findings_by_key.get(meta.key, []))
            transcript_missing = bool(meta.session_file) and not bool(meta.session_file.exists())
            telegram_binding: Optional[TelegramThreadBinding] = None
            telegram_routed_elsewhere = False
            if (meta.channel or "") == "telegram":
                telegram_binding = self._telegram_binding_for(account_id=meta.account_id, to=meta.to)
                if telegram_binding and telegram_binding.target_session_key and telegram_binding.target_session_key != meta.key:
                    telegram_routed_elsewhere = True

            acct_info = _channel_account_info(self.channels, channel=meta.channel, account_id=meta.account_id)
            out_at = _dt_from_ms(int(acct_info.get("lastOutboundAt")) if isinstance(acct_info, dict) and isinstance(acct_info.get("lastOutboundAt"), int) else None)
            internal_at = _internal_activity_at(tail)
            views.append(
                SessionView(
                    meta=meta,
                    tail=tail,
                    lock=lock,
                    working=working,
                    acpx=acpx,
                    delivery_failure=df,
                    computed=computed,
                    findings=findings,
                    updated_at=_dt_from_ms(meta.updated_at_ms),
                    transcript_missing=transcript_missing,
                    telegram_binding=telegram_binding,
                    telegram_routed_elsewhere=telegram_routed_elsewhere,
                    human_out_at=out_at,
                    internal_activity_at=internal_at,
                )
            )
        with self._sessions_lock:
            self._sessions = views


@dataclass(frozen=True)
class _ListHeader:
    agent_id: str
    agent_kind: str  # configured|implicit
    count: int
    cron_count: int


@dataclass(frozen=True)
class _ListSession:
    sv: "SessionView"
    indent_units: int
    node_label: str
    key_tail: str


@dataclass(frozen=True)
class _ListCronJob:
    agent_id: str
    job: CronJob
    last_run: Optional[CronRunStatus]


ListItem = Union[_ListHeader, _ListSession, _ListCronJob]


class ClawMonitorTUI:
    def __init__(self, cfg: Config, *, config_path: Optional[Path] = None) -> None:
        self.cfg = cfg
        self.config_path = config_path
        self.elog = EventLog()
        self.model = MonitorModel(cfg, self.elog)
        self._loading_art_lines = _load_loading_art_lines()
        self.selected = 0
        self.scroll = 0
        self.selected_session_key: Optional[str] = None
        self.show_logs = True
        self.tree_view = True
        self.show_cron = True
        self.node_show_session_label = False
        self.focus_mode = False
        self.focus_recent_hours = 24.0
        self.refresh_seconds = float(cfg.ui_seconds)
        self._last_refresh_at: Optional[float] = None
        self._colors_enabled = False
        self._color_ok = 0
        self._color_working = 0
        self._color_idle = 0
        self._color_alert = 0
        self._color_magenta = 0
        self._refresh_lock = threading.Lock()
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._refresh_started_at: Optional[float] = None
        self._refresh_progress_msg: str = ""
        self._refresh_progress_step: int = 0
        self._refresh_progress_total: int = 0
        self._refresh_error: Optional[str] = None
        self._rel_cache_session_key: Optional[str] = None
        self._rel_cache_log_count: int = -1
        self._rel_cache_lines: List[str] = []
        self._rel_cache_last_activity: Optional[str] = None
        self._last_total_sessions = 0
        self._last_shown_sessions = 0

    def run(self) -> None:
        curses.wrapper(self._main)

    def _set_refresh_progress(self, msg: str, step: int, total: int) -> None:
        with self._refresh_lock:
            self._refresh_progress_msg = msg
            self._refresh_progress_step = step
            self._refresh_progress_total = total

    def _refresh_worker(self) -> None:
        try:
            self.model.refresh(progress=self._set_refresh_progress)
            err = None
        except Exception as e:
            err = str(e)
        with self._refresh_lock:
            self._refresh_in_progress = False
            self._refresh_error = err
            self._last_refresh_at = time.time()
            if self._refresh_pending:
                self._refresh_pending = False
                # Allow immediate next refresh; main loop will start it.

    def _request_refresh(self) -> None:
        with self._refresh_lock:
            if self._refresh_in_progress:
                self._refresh_pending = True
                return
            self._refresh_in_progress = True
            self._refresh_started_at = time.time()
            self._refresh_error = None
            self._refresh_progress_msg = "Refreshing…"
            self._refresh_progress_step = 0
            self._refresh_progress_total = 0
        t = threading.Thread(target=self._refresh_worker, name="clawmonitor-refresh", daemon=True)
        t.start()

    def _related_logs_cached(self, sv: SessionView) -> Tuple[List[str], Optional[str]]:
        if not self.show_logs:
            return [], None
        session_key = sv.meta.key
        log_count = self.model.gateway_log_tailer.line_count
        if self._rel_cache_session_key == session_key and self._rel_cache_log_count == log_count:
            return self._rel_cache_lines, self._rel_cache_last_activity
        rel = related_logs(self.model.gateway_log_tailer.lines, session_key, sv.meta.channel, sv.meta.account_id, limit=50)
        rel_lines = [ln.text for ln in rel][-20:]
        last_activity: Optional[str] = None
        if rel:
            ln = rel[-1]
            last_activity = (ln.text or ln.raw or "").strip()
        self._rel_cache_session_key = session_key
        self._rel_cache_log_count = log_count
        self._rel_cache_lines = rel_lines
        self._rel_cache_last_activity = last_activity
        return rel_lines, last_activity

    def _agent_kind(self, agent_id: str) -> str:
        snap = self.model.config_snapshot
        if snap and snap.configured_agent_ids.get(agent_id):
            return "configured"
        return "implicit"

    def _agent_label(self, agent_id: str) -> str:
        snap = self.model.config_snapshot
        if snap:
            return snap.agent_label(agent_id)
        return (agent_id or "").strip() or "-"

    def _indent_units_for(self, session_key: str) -> int:
        info = parse_session_key(session_key)
        if info.kind == "subagent":
            depth = max(1, info.subagent_depth)
            return 1 + depth
        if info.kind == "acp":
            return 2
        if info.kind == "cron_run":
            return 2
        return 1

    def _key_tail(self, session_key: str, *, agent_id: str) -> str:
        key = (session_key or "").strip()
        prefix = f"agent:{agent_id}:"
        if key.startswith(prefix):
            return key[len(prefix) :]
        return key

    def _node_label_for(self, sv: "SessionView") -> str:
        info = parse_session_key(sv.meta.key)
        if info.kind == "channel":
            return (sv.meta.channel or info.channel or "channel").strip() or "channel"
        if info.kind == "cron":
            job = match_cron_job(self.model.cron_snapshot, sv.meta.key)
            if job and job.name:
                return "cron"
            return "cron"
        if info.kind == "cron_run":
            return "run"
        if info.kind == "acp":
            st = (sv.meta.acp_state or "").strip()
            return f"acp:{st}" if st else "acp"
        return info.kind

    def _display_key_tail(self, sv: "SessionView") -> str:
        info = parse_session_key(sv.meta.key)
        if info.kind == "cron":
            job = match_cron_job(self.model.cron_snapshot, sv.meta.key)
            if job and job.name:
                return job.name
        if info.kind == "cron_run":
            # Prefer showing just the run id suffix if present.
            key = (sv.meta.key or "").strip()
            parts = key.split(":")
            if len(parts) >= 6 and parts[0] == "agent" and parts[2] == "cron" and parts[4] == "run":
                return f"run:{parts[5]}"
        # For channel sessions, prefer a human label if configured.
        if info.kind == "channel":
            raw_tail = self._key_tail(sv.meta.key, agent_id=(sv.meta.agent_id or "-"))
            lbl = session_display_label(self.cfg.labels, sv.meta)
            if lbl and lbl != raw_tail:
                # Disambiguate if multiple sessions share the same label.
                suf = _tail_suffix(sv.meta.key, n=4)
                return f"{lbl}({suf})" if suf else lbl
            if lbl:
                return lbl
        return self._key_tail(sv.meta.key, agent_id=(sv.meta.agent_id or "-"))

    def _build_list_items(self, sessions: List["SessionView"]) -> List[ListItem]:
        if not self.tree_view:
            return [
                _ListSession(
                    sv=sv,
                    indent_units=0,
                    node_label=self._agent_label(sv.meta.agent_id or "-"),
                    key_tail=sv.meta.key,
                )
                for sv in sessions
            ]

        by_agent: Dict[Tuple[str, str], List["SessionView"]] = {}
        for sv in sessions:
            agent_id = sv.meta.agent_id or "-"
            agent_kind = self._agent_kind(agent_id)
            by_agent.setdefault((agent_id, agent_kind), []).append(sv)

        cron_by_agent: Dict[str, List[CronJob]] = {}
        snap = self.model.cron_snapshot
        if snap:
            for job in snap.jobs_by_id.values():
                aid = (job.agent_id or "main").strip() if job.agent_id else "main"
                cron_by_agent.setdefault(aid, []).append(job)

        def sess_sort_key(sv: "SessionView") -> Tuple[int, str, str]:
            info = parse_session_key(sv.meta.key)
            kind = info.kind
            order = {
                "main": 0,
                "channel": 1,
                "heartbeat": 2,
                "cron": 3,
                "cron_run": 4,
                "acp": 5,
                "subagent": 6,
                "unknown": 9,
            }.get(kind, 9)
            surface = (sv.meta.channel or info.channel or kind or "-").lower()
            return (order, surface, sv.meta.key)

        items: List[ListItem] = []
        for (agent_id, agent_kind) in sorted(by_agent.keys(), key=lambda x: (x[1] != "configured", x[0])):
            rows = sorted(by_agent[(agent_id, agent_kind)], key=sess_sort_key)
            cron_count = len(cron_by_agent.get(agent_id, []))
            items.append(_ListHeader(agent_id=agent_id, agent_kind=agent_kind, count=len(rows), cron_count=cron_count))
            for sv in rows:
                items.append(
                    _ListSession(
                        sv=sv,
                        indent_units=self._indent_units_for(sv.meta.key),
                        node_label=self._node_label_for(sv),
                        key_tail=self._display_key_tail(sv),
                    )
                )
            if self.show_cron and cron_count:
                jobs = sorted(cron_by_agent.get(agent_id, []), key=lambda j: (j.enabled is False, (j.name or ""), j.id))
                runs = self.model.cron_last_runs
                for job in jobs:
                    items.append(_ListCronJob(agent_id=agent_id, job=job, last_run=runs.get(job.id)))
        return items

    def _is_focus_interesting(self, sv: SessionView) -> bool:
        # Always keep any session that is working or needs attention.
        if sv.computed.state.value in ("WORKING", "INTERRUPTED"):
            return True
        if sv.computed.no_feedback:
            return True
        if sv.delivery_failure is not None:
            return True
        if sv.computed.safety_alert or sv.computed.safeguard_alert:
            return True
        if sv.transcript_missing:
            return True
        if sv.lock and sv.lock.pid_alive is False:
            return True
        if sv.telegram_routed_elsewhere:
            return True
        if sv.working and sv.working.kind == "acp":
            return True
        # Keep sessions explicitly labeled by the user.
        if has_user_label(self.cfg.labels, sv.meta):
            return True
        # Keep recently active sessions.
        latest = sv.updated_at
        if sv.tail.last_user_send and sv.tail.last_user_send.ts and (latest is None or sv.tail.last_user_send.ts > latest):
            latest = sv.tail.last_user_send.ts
        if sv.tail.last_assistant and sv.tail.last_assistant.ts and (latest is None or sv.tail.last_assistant.ts > latest):
            latest = sv.tail.last_assistant.ts
        if latest:
            age = _age_seconds(latest)
            if age is not None and age <= int(self.focus_recent_hours * 3600):
                return True
        return False

    def _apply_session_filter(self, sessions: List[SessionView]) -> List[SessionView]:
        self._last_total_sessions = len(sessions)
        if not self.focus_mode:
            self._last_shown_sessions = len(sessions)
            return sessions
        out = [sv for sv in sessions if self._is_focus_interesting(sv)]
        self._last_shown_sessions = len(out)
        return out

    def _is_selectable(self, item: ListItem) -> bool:
        return isinstance(item, _ListSession)

    def _selected_session(self, items: List[ListItem]) -> Optional["SessionView"]:
        if not items or self.selected < 0 or self.selected >= len(items):
            return None
        it = items[self.selected]
        if isinstance(it, _ListSession):
            return it.sv
        return None

    def _reconcile_selection(self, items: List[ListItem]) -> None:
        if not items:
            self.selected = 0
            self.scroll = 0
            self.selected_session_key = None
            return

        if self.selected_session_key:
            for i, it in enumerate(items):
                if isinstance(it, _ListSession) and it.sv.meta.key == self.selected_session_key:
                    self.selected = i
                    return

        for i, it in enumerate(items):
            if self._is_selectable(it):
                self.selected = i
                if isinstance(items[i], _ListSession):
                    self.selected_session_key = items[i].sv.meta.key
                return
        self.selected = 0
        self.selected_session_key = None

    def _move_selection(self, items: List[ListItem], delta: int) -> None:
        if not items:
            return
        i = self.selected
        step = 1 if delta > 0 else -1
        remaining = abs(delta)
        while remaining > 0:
            j = i + step
            while 0 <= j < len(items) and not self._is_selectable(items[j]):
                j += step
            if j < 0 or j >= len(items):
                break
            i = j
            remaining -= 1
        self.selected = max(0, min(len(items) - 1, i))
        sv = self._selected_session(items)
        if sv:
            self.selected_session_key = sv.meta.key

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
        # Keep a small margin to avoid curses wrapping long/wide strings into the
        # next line (which can visually "spill" into the left pane).
        maxw = min(width, max(0, w - x - 2))
        if maxw <= 0:
            return
        text = _sanitize_for_curses(text)
        # Always pad to the target width so shorter redraws don't leave stale
        # characters on screen (common with dynamic status/details lines).
        text = _pad_right_cells(text, maxw)
        try:
            if attr:
                stdscr.addnstr(y, x, text, maxw, attr)
            else:
                stdscr.addnstr(y, x, text, maxw)
        except curses.error:
            return

    def _draw_loading(
        self,
        stdscr: "curses._CursesWindow",
        *,
        msg: str,
        step: int,
        total_steps: int,
        started_at: float,
    ) -> None:
        h, w = stdscr.getmaxyx()
        stdscr.erase()

        art = list(self._loading_art_lines or [])
        # Keep enough vertical space for progress + footer notes.
        max_art_h = max(0, h - 8)
        if max_art_h and len(art) > max_art_h:
            # Prefer keeping the "shrimp" block visible on small terminals.
            shrimp_start = None
            for i, ln in enumerate(art):
                if any(ch in ln for ch in ("⣀", "⣿", "⠀⠀")):
                    shrimp_start = i
                    break
            if shrimp_start is not None:
                art = art[shrimp_start : shrimp_start + max_art_h]
            else:
                art = art[:max_art_h]
        block_h = len(art) + 6
        y0 = max(0, (h - block_h) // 2)
        for i, ln in enumerate(art):
            ln = ln.rstrip("\n")
            ln_w = _display_width(ln)
            x0 = 0 if ln_w >= w - 2 else max(0, (w - ln_w) // 2)
            self._safe_addnstr(stdscr, y0 + i, x0, ln, max(0, w - 2))

        step = max(0, min(total_steps, step))
        ratio = (step / max(1, total_steps)) if total_steps else 0.0
        bar_w = max(10, min(60, w - 10))
        fill = int(bar_w * ratio)
        bar = "[" + ("#" * fill).ljust(bar_w, ".") + "]"
        elapsed = int(max(0.0, time.time() - started_at))
        info = f"{bar}  {step}/{total_steps}  {elapsed}s"

        self._safe_addnstr(stdscr, y0 + len(art) + 1, max(0, (w - len(info)) // 2), info, max(0, w - 2))
        self._safe_addnstr(stdscr, y0 + len(art) + 3, 2, f"OpenClaw: {self.cfg.openclaw_root}", max(0, w - 4))
        self._safe_addnstr(stdscr, y0 + len(art) + 4, 2, f"Phase: {msg}", max(0, w - 4))
        self._safe_addnstr(stdscr, h - 2, 2, "Loading… (initial refresh can take a few seconds if Gateway calls are slow)", max(0, w - 4))
        self._safe_addnstr(stdscr, h - 1, 2, "Tip: later you can press [r] to refresh and [?] for help.", max(0, w - 4))
        stdscr.refresh()

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

    def _draw_list(self, stdscr: "curses._CursesWindow", y: int, h: int, w: int, items: List[ListItem]) -> None:
        node_w = max(10, min(18, int(w * 0.20)))
        state_w = 11
        flags_w = max(8, min(12, int(w * 0.16)))
        header = f"{_fit('NODE', node_w)}  {_fit('STATE', state_w)}  U-AGE  A-AGE  RUN   {_fit('FLAGS', flags_w)}  SESSION"
        self._safe_addnstr(stdscr, y, 0, header.ljust(w), w, curses.A_BOLD)
        body_y = y + 1
        visible = max(0, h - 1)
        if self.selected < self.scroll:
            self.scroll = self.selected
        if self.selected >= self.scroll + visible:
            self.scroll = self.selected - visible + 1
        for i in range(visible):
            idx = self.scroll + i
            row_y = body_y + i
            if idx >= len(items):
                self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
                continue
            it = items[idx]
            if isinstance(it, _ListHeader):
                extra = f"  cron={it.cron_count}" if it.cron_count else ""
                line = f"{self._agent_label(it.agent_id)} ({it.agent_kind})  sessions={it.count}{extra}"
                self._safe_addnstr(stdscr, row_y, 0, _fit(line, w).ljust(w), w, curses.A_BOLD)
                continue
            if isinstance(it, _ListCronJob):
                job = it.job
                indent = "  " * 1
                node_text = f"{indent}- cron"
                status = (it.last_run.status or "-") if it.last_run else "-"
                status = status.upper()[:11]
                run_dt = _dt_from_ms(it.last_run.ts_ms) if it.last_run else None
                run_age = _fmt_age(_age_seconds(run_dt))
                flags_list: List[str] = ["CRONJOB"]
                if job.enabled is False:
                    flags_list.append("DISABLED")
                flags = ",".join(flags_list)
                name = job.name or job.id
                sess = f"{name} ({job.id[:8]})"
                line = (
                    f"{_fit(node_text, node_w)}  "
                    f"{_fit(status, state_w)}  "
                    f"{'-':>5}  {'-':>5}  {run_age:>5}  "
                    f"{_fit(flags, flags_w)}  "
                    f"{sess}"
                )
                self._safe_addnstr(stdscr, row_y, 0, _fit(line, w).ljust(w), w)
                continue

            sv = it.sv
            user_msg = sv.tail.last_user_send
            u_age = _fmt_age(_age_seconds(user_msg.ts if user_msg else sv.updated_at))
            a_age = _fmt_age(_age_seconds(sv.tail.last_assistant.ts if sv.tail.last_assistant else None))
            run = "-"
            run_at = sv.lock.created_at if sv.lock else (sv.working.created_at if sv.working else None)
            if run_at:
                run = _fmt_age(int((datetime.now(timezone.utc) - run_at).total_seconds()))
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
            if sv.working and sv.working.kind == "acp":
                flags.append("ACPRUN")
            if sv.computed.safety_alert:
                flags.append("SAFE")
            if sv.transcript_missing:
                flags.append("TRXM")
            if sv.telegram_routed_elsewhere:
                flags.append("BIND")
            flags.extend(_agent_markers(sv.meta, self.model.config_snapshot))
            # Keep the list compact so SESSION remains readable.
            keep = max(1, min(3, len(flags)))
            shown_flags = flags[:keep]
            extra = max(0, len(flags) - len(shown_flags))
            flag_str = ",".join(shown_flags) + (f"+{extra}" if extra else "")
            indent = "  " * max(0, it.indent_units)
            node_leaf = it.node_label
            if self.node_show_session_label:
                info = parse_session_key(sv.meta.key)
                if info.kind == "channel":
                    lbl = session_display_label(self.cfg.labels, sv.meta)
                    if lbl:
                        suf = _tail_suffix(sv.meta.key, n=4)
                        # Keep NODE readable even with repeated labels across old sessions.
                        lbl2 = f"{lbl}({suf})" if suf else lbl
                        node_leaf = f"{it.node_label}:{lbl2}"
            node_text = f"{indent}- {node_leaf}"
            line = (
                f"{_fit(node_text, node_w)}  "
                f"{_fit(sv.computed.state.value, state_w)}  "
                f"{u_age:>5}  {a_age:>5}  {run:>5}  "
                f"{_fit(flag_str, flags_w)}  "
                f"{it.key_tail}"
            )
            attr = self._row_attr(health_cls, selected=(idx == self.selected))
            self._safe_addnstr(stdscr, row_y, 0, line.ljust(w), w, attr)

    def _draw_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: Optional[SessionView]) -> None:
        if not sv:
            return
        rel_logs, last_activity = self._related_logs_cached(sv)

        log_budget = 0
        if self.show_logs:
            log_budget = min(18, max(0, h // 3))
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
            hdr_attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
            self._safe_addnstr(stdscr, log_y, x, "Related Logs:".ljust(w), w, hdr_attr)
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
        lines.append(
            f"Agent: {self._agent_label(sv.meta.agent_id)}{mark_str}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}"
        )
        cron_job = match_cron_job(self.model.cron_snapshot, sv.meta.key)
        if cron_job:
            label = cron_job.name or cron_job.id
            enabled = "-" if cron_job.enabled is None else ("enabled" if cron_job.enabled else "disabled")
            owner = cron_job.agent_id or "-"
            lines.append(f"Cron: {label}  jobId={cron_job.id[:8]}  agent={owner}  {enabled}")
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
        key_info = parse_session_key(sv.meta.key)
        agent_kind = "configured" if (self.model.config_snapshot and self.model.config_snapshot.configured_agent_ids.get(sv.meta.agent_id, False)) else "implicit"
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {self._agent_label(sv.meta.agent_id)}{mark_str}  Kind: {key_info.kind}/{agent_kind}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}",
            f"UpdatedAt: {_fmt_dt(sv.updated_at)}",
            f"Transcript: {'MISSING' if sv.transcript_missing else ('-' if not sv.meta.session_file else 'OK')}",
            f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}",
        ]
        cron_job = match_cron_job(self.model.cron_snapshot, sv.meta.key)
        if cron_job:
            label = cron_job.name or cron_job.id
            enabled = "-" if cron_job.enabled is None else ("enabled" if cron_job.enabled else "disabled")
            owner = cron_job.agent_id or "-"
            status_lines.insert(2, f"Cron: {label}  jobId={cron_job.id[:8]}  agent={owner}  {enabled}")
        if sv.tail.last_entry_type:
            status_lines.append(f"LastEntry: {sv.tail.last_entry_type} @ {_fmt_dt(sv.tail.last_entry_ts)}")
        if sv.acpx and sv.meta.acpx_session_id:
            st = sv.meta.acp_state or "-"
            exit_at = _fmt_dt(sv.acpx.last_agent_exit_at)
            status_lines.append(f"ACPX: id={sv.meta.acpx_session_id} state={st} closed={sv.acpx.closed} exitAt={exit_at}")
        if sv.working and not sv.lock:
            status_lines.append(
                f"Work: {sv.working.kind} pid={sv.working.pid or '-'} since={_fmt_dt(sv.working.created_at)}"
            )
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
            if sv.tail.last_tool_call and sv.tail.last_tool_call.tool_names:
                names = ",".join(sv.tail.last_tool_call.tool_names[:3])
                status_lines.append(f"ToolCall: {names}")
            if sv.tail.last_tool_result:
                tr = sv.tail.last_tool_result
                ok = "err" if tr.is_error else "ok"
                status_lines.append(f"ToolResult: {tr.tool_name} {ok} @ {_fmt_dt(tr.ts)}")
            if sv.tail.last_tool_error:
                ts, summary = sv.tail.last_tool_error
                status_lines.append(f"Last tool error: {_fmt_dt(ts)} {redact_text(summary)}")
            if last_activity:
                status_lines.extend(_wrap_lines(f"Activity: {redact_text(last_activity)}", max(10, w), max_lines=1)[:1])
        elif sv.working:
            task_src = sv.tail.last_user_send or sv.tail.last_trigger
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, w), max_lines=2)[:2])
            if sv.tail.last_assistant_thinking:
                think_lines = _wrap_lines(f"Thinking: {redact_text(sv.tail.last_assistant_thinking)}", max(10, w), max_lines=2)
                status_lines.extend(think_lines[:2])
            if sv.tail.last_tool_call and sv.tail.last_tool_call.tool_names:
                names = ",".join(sv.tail.last_tool_call.tool_names[:3])
                status_lines.append(f"ToolCall: {names}")
            if sv.tail.last_tool_result:
                tr = sv.tail.last_tool_result
                ok = "err" if tr.is_error else "ok"
                status_lines.append(f"ToolResult: {tr.tool_name} {ok} @ {_fmt_dt(tr.ts)}")
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
        if sv.human_out_at:
            status_lines.append(f"HumanOut: @ {_fmt_dt(sv.human_out_at)}  age={_fmt_age(_age_seconds(sv.human_out_at)).strip()}")
        else:
            status_lines.append("HumanOut: -")
        if sv.internal_activity_at:
            status_lines.append(
                f"Internal: @ {_fmt_dt(sv.internal_activity_at)}  age={_fmt_age(_age_seconds(sv.internal_activity_at)).strip()}"
            )
        else:
            status_lines.append("Internal: -")
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

        max_status_lines = max(0, status_h - 1)
        visible_status_lines = status_lines
        if max_status_lines and len(status_lines) > max_status_lines:
            # Keep the most important "header" lines, but always keep tail lines
            # where alerts/diagnosis live (including Diagnosis: (none)).
            head_n = min(5, max(1, max_status_lines - 3))
            tail_n = max(1, max_status_lines - head_n - 1)
            visible_status_lines = status_lines[:head_n] + ["…"] + status_lines[-tail_n:]

        for i in range(min(max_status_lines, len(visible_status_lines))):
            ln = visible_status_lines[i]
            attr = 0
            if ln.startswith(("Task:", "Thinking:", "Trigger:", "ToolCall:", "ToolResult:")):
                attr = self._color_magenta if self._colors_enabled else 0
            elif ln.startswith("Diagnosis:"):
                if not self._colors_enabled:
                    attr = curses.A_BOLD
                else:
                    low = ln.lower()
                    if "(none)" in low:
                        attr = curses.A_BOLD | self._color_ok
                    elif "[info]" in low:
                        attr = curses.A_BOLD | self._color_idle
                    else:
                        attr = curses.A_BOLD | self._color_alert
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(ln, w), w, attr)

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
            model = sv.tail.last_assistant.model or "-"
            provider = sv.tail.last_assistant.provider or "-"
            claw_lines = [
                f"@ {_fmt_dt(sv.tail.last_assistant.ts)}",
                f"stop={sv.tail.last_assistant.stop_reason or '-'}  model={provider}/{model}" if (model != "-" or provider != "-") else f"stop={sv.tail.last_assistant.stop_reason or '-'}",
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
        key_info = parse_session_key(sv.meta.key)
        agent_kind = "configured" if (self.model.config_snapshot and self.model.config_snapshot.configured_agent_ids.get(sv.meta.agent_id, False)) else "implicit"
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {self._agent_label(sv.meta.agent_id)}{mark_str}  Kind: {key_info.kind}/{agent_kind}  Channel: {sv.meta.channel or '-'}  Account: {sv.meta.account_id or '-'}",
            f"UpdatedAt: {_fmt_dt(sv.updated_at)}",
            f"Transcript: {'MISSING' if sv.transcript_missing else ('-' if not sv.meta.session_file else 'OK')}",
            f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}",
        ]
        cron_job = match_cron_job(self.model.cron_snapshot, sv.meta.key)
        if cron_job:
            label = cron_job.name or cron_job.id
            enabled = "-" if cron_job.enabled is None else ("enabled" if cron_job.enabled else "disabled")
            owner = cron_job.agent_id or "-"
            status_lines.insert(2, f"Cron: {label}  jobId={cron_job.id[:8]}  agent={owner}  {enabled}")
        if sv.tail.last_entry_type:
            status_lines.append(f"LastEntry: {sv.tail.last_entry_type} @ {_fmt_dt(sv.tail.last_entry_ts)}")
        if sv.acpx and sv.meta.acpx_session_id:
            st = sv.meta.acp_state or "-"
            exit_at = _fmt_dt(sv.acpx.last_agent_exit_at)
            status_lines.append(f"ACPX: id={sv.meta.acpx_session_id} state={st} closed={sv.acpx.closed} exitAt={exit_at}")
        if sv.working and not sv.lock:
            status_lines.append(
                f"Work: {sv.working.kind} pid={sv.working.pid or '-'} since={_fmt_dt(sv.working.created_at)}"
            )
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
        elif sv.working:
            task_src = sv.tail.last_user_send or sv.tail.last_trigger
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, w), max_lines=2)[:2])
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
            ln = status_lines[i]
            attr = 0
            if ln.startswith(("Task:", "Thinking:", "Trigger:")):
                attr = self._color_magenta if self._colors_enabled else 0
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(ln, w), w, attr)

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
            model = sv.tail.last_assistant.model or "-"
            provider = sv.tail.last_assistant.provider or "-"
            model_str = f"  model={provider}/{model}" if (model != "-" or provider != "-") else ""
            self._safe_addnstr(
                stdscr,
                y_claw + 1,
                x,
                _fit(f"@ {_fmt_dt(sv.tail.last_assistant.ts)}  stop={sv.tail.last_assistant.stop_reason or '-'}{model_str}", w),
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

    def _pick_label_key(self, stdscr: "curses._CursesWindow", meta: SessionMeta) -> Optional[str]:
        key = (meta.key or "").strip()
        chan = (meta.channel or "").strip()
        opts: List[Tuple[str, str]] = []
        if chan and key:
            tail = key.split(":")[-1].strip()
            if tail and (tail.startswith(("ou_", "oc_", "om_")) or (tail.isdigit() and len(tail) >= 5)):
                opts.append((f"id:{chan}:{tail}", f"id:{chan}:{tail}"))
        if chan and meta.to:
            opts.append((f"target:{chan}:{meta.to}", f"target:{chan}:{meta.to}"))
        if key:
            opts.append((f"sessionKey:{key}", f"sessionKey:{key}"))
        if not opts:
            return None

        idx = 0
        scroll = 0
        h, w = stdscr.getmaxyx()
        win_h = min(10, h - 4)
        win_w = min(96, max(50, w - 4))
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        while True:
            win.clear()
            win.border()
            self._safe_addnstr(win, 0, 2, " Choose label key ", win_w - 4)
            visible = max(1, win_h - 2)
            if idx < scroll:
                scroll = idx
            if idx >= scroll + visible:
                scroll = idx - visible + 1
            view = opts[scroll : scroll + visible]
            for i, (label, _) in enumerate(view):
                real_idx = scroll + i
                attr = curses.A_REVERSE if real_idx == idx else curses.A_NORMAL
                self._safe_addnstr(win, 1 + i, 2, _pad_right_cells(label, win_w - 4), win_w - 4, attr)
            win.refresh()
            ch = win.getch()
            if ch in (27, ord("q")):
                return None
            if ch in (curses.KEY_UP, ord("k")):
                idx = max(0, idx - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                idx = min(len(opts) - 1, idx + 1)
            elif ch in (10, 13):
                return opts[idx][1]

    def _prompt_label(self, stdscr: "curses._CursesWindow", *, title: str, current: str) -> Optional[str]:
        h, w = stdscr.getmaxyx()
        win_h = 9
        win_w = min(96, max(50, w - 4))
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        win.clear()
        win.border()
        self._safe_addnstr(win, 0, 2, f" {title} ", win_w - 4)
        self._safe_addnstr(win, 1, 2, "Current:", win_w - 4)
        self._safe_addnstr(win, 2, 4, _fit(current or "-", win_w - 6), win_w - 6)
        self._safe_addnstr(win, 4, 2, "New label (empty = clear, Esc = cancel):", win_w - 4)
        win.refresh()

        # Input line
        try:
            curses.curs_set(1)
        except Exception:
            pass
        win.move(5, 2)
        win.clrtoeol()
        win.refresh()
        curses.echo()
        try:
            raw = win.getstr(5, 2, max(1, win_w - 4))
        except KeyboardInterrupt:
            raw = None
        except Exception:
            raw = None
        finally:
            curses.noecho()
            try:
                curses.curs_set(0)
            except Exception:
                pass
        if raw is None:
            return None
        try:
            s = raw.decode("utf-8", errors="ignore")
        except Exception:
            s = str(raw)
        return s.strip()

    def _rename_selected(self, stdscr: "curses._CursesWindow", sv: SessionView) -> None:
        meta = sv.meta
        key = self._pick_label_key(stdscr, meta)
        if not key:
            return
        current = self.cfg.labels.get(key) or ""
        new_label = self._prompt_label(stdscr, title="Set session label", current=current)
        if new_label is None:
            return
        if not new_label:
            if key in self.cfg.labels:
                self.cfg.labels.pop(key, None)
                self.elog.write("labels.cleared", key=key)
        else:
            self.cfg.labels[key] = new_label
            self.elog.write("labels.set", key=key, label=new_label)
        if self.config_path:
            try:
                write_labels(self.config_path, self.cfg.labels)
            except Exception as e:
                self.elog.write("labels.write_failed", key=key, error=str(e))

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
            "  R              Rename/label selected session",
            "  f              Cycle refresh interval (up to 10 minutes)",
            "  t              Toggle tree view (group by agent)",
            "  c              Toggle cron jobs in tree view",
            "  n              Toggle NODE label mode (channel:label)",
            "  x              Toggle Focus filter (hide stale/boring sessions)",
            "  Enter          Send nudge (chat.send) using a template",
            "  e              Export redacted report (JSON+MD)",
            "  l              Toggle related logs panel",
            "  d              Diagnose selected session (includes silent-gap hints)",
            "",
            "States (STATE column):",
            "  WORKING        Task is running (lock present or ACPX indicates running).",
            "  FINISHED       No lock and assistant is not behind user.",
            "  INTERRUPTED    AbortedLastRun + pending reply (usually a crash/kill).",
            "  NO_MESSAGE     No real inbound user message detected for this session key.",
            "",
            "Performance:",
            "  - Refresh runs asynchronously; footer shows refresh progress/errors.",
            "  - Related Logs are cached per session to keep ↑/↓ selection responsive.",
            "",
            "Left list columns:",
            "  NODE, STATE, U-AGE, A-AGE, RUN, FLAGS, SESSION",
            "  (SESSION is the sessionKey tail; may be truncated in narrow terminals)",
            "",
            "FLAGS column (compact):",
            "  First token is a health label:",
            "    OK     Normal / completed",
            "    RUN    Working / long-running",
            "    IDLE   No inbound user message (often channel not bound / wrong session key)",
            "    ALERT  Needs attention (NOFB / delivery failure / safety / safeguard off / interrupted)",
            "  Then short flags (may show +N for hidden extras):",
            "    NOFB   User spoke after last assistant reply (pending response).",
            "    DLV    Delivery queue has failed outbound message(s).",
            "    ZLOCK  Lock pid is dead but lock file remains (stale lock).",
            "    ACPRUN ACP/ACPX run appears to be in progress.",
            "    SAFE   Provider stop_reason suggests a safety/refusal event.",
            "    TRXM   Transcript missing (sessionFile path missing).",
            "    BIND   Telegram thread-binding routes this chat elsewhere.",
            "    ACP/SUB/CODEX/IMPL/HEARTBEAT identify ACP/subagent/codex/implicit/heartbeat sessions.",
            "",
            "Notes:",
            "  - Related Logs require Gateway logs.tail (online mode).",
            "  - Focus filter keeps WORKING/ALERT/recent/labeled sessions; press [x] to see all.",
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

    def _silent_gap_findings(self, sv: SessionView) -> List[Finding]:
        """
        Lightweight heuristics for "agent active but no human-visible output".

        These findings are intentionally conservative and only meant to help
        interpret the gap between transcript activity and channel delivery.
        """
        out_age = _age_seconds(sv.human_out_at)
        internal_age = _age_seconds(sv.internal_activity_at)
        if out_age is None:
            return []
        is_working = sv.computed.state == WorkState.WORKING
        if not is_working:
            return []

        findings: List[Finding] = []
        # Thresholds: keep simple and avoid noisy alerts.
        # - if internal is fresh but human output is stale, suspect silent gap.
        if internal_age is not None and out_age >= 10 * 60 and internal_age <= 2 * 60 and (out_age - internal_age) >= 5 * 60:
            findings.append(
                Finding(
                    id="silent_output_gap",
                    severity="warn",
                    summary="Internal activity is recent, but human-visible outbound looks stale (possible silent loop, delivery, routing, or policy gate).",
                    evidence=[
                        Evidence(ts=None, text=f"HumanOut age={_fmt_age(out_age).strip()} last={_fmt_dt(sv.human_out_at)}"),
                        Evidence(ts=None, text=f"Internal age={_fmt_age(internal_age).strip()} last={_fmt_dt(sv.internal_activity_at)}"),
                    ],
                    next_steps=[
                        "Check `DELIVERY_FAILED` and the related logs panel for outbound errors.",
                        "Compare transcript `lastAssistantAt` vs channel `lastOutboundAt` (delivery gap).",
                        "If Telegram, verify thread bindings are not routing the chat elsewhere (BIND/BOUND_OTHER).",
                    ],
                )
            )
        # If both are stale but lock still present, suspect a stall/hang.
        if out_age >= 10 * 60 and (internal_age is None or internal_age >= 10 * 60) and (sv.lock is not None or sv.working is not None):
            findings.append(
                Finding(
                    id="possible_stall",
                    severity="warn",
                    summary="Session looks WORKING but there is no recent internal activity and no recent human-visible outbound.",
                    evidence=[
                        Evidence(ts=None, text=f"HumanOut age={_fmt_age(out_age).strip()} last={_fmt_dt(sv.human_out_at)}"),
                        Evidence(ts=None, text=f"Internal age={_fmt_age(internal_age).strip() if internal_age is not None else '-'} last={_fmt_dt(sv.internal_activity_at)}"),
                    ],
                    next_steps=[
                        "Inspect lock details (pid alive/createdAt) and consider restarting the agent/gateway if it is stuck.",
                        "Open related logs and look for upstream/tool timeouts or repeated failures.",
                    ],
                )
            )
        return findings

    def _diagnose_selected(self, sv: SessionView) -> None:
        # Pull latest logs for better evidence (best-effort).
        try:
            self.model.gateway_log_tailer.poll(limit=200)
        except Exception:
            pass
        findings = []
        try:
            findings.extend(self._silent_gap_findings(sv))
        except Exception:
            pass
        try:
            findings.extend(
                diagnose(
                    session_key=sv.meta.key,
                    channel=sv.meta.channel,
                    account_id=sv.meta.account_id,
                    delivery_failed=sv.delivery_failure is not None,
                    no_feedback=sv.computed.no_feedback,
                    is_working=sv.computed.state.value == "WORKING",
                    gateway_lines=self.model.gateway_log_tailer.lines,
                )
            )
        except Exception:
            pass
        self.model.set_findings(sv.meta.key, findings)
        sv.findings = findings

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
                curses.init_pair(5, curses.COLOR_MAGENTA, -1)
                self._colors_enabled = True
                self._color_ok = curses.color_pair(1)
                self._color_working = curses.color_pair(3)
                self._color_idle = curses.color_pair(2)
                self._color_alert = curses.color_pair(4)
                self._color_magenta = curses.color_pair(5)
        except Exception:
            self._colors_enabled = False
        stdscr.timeout(200)
        stdscr.keypad(True)
        started_at = time.time()
        self._draw_loading(stdscr, msg="Starting…", step=0, total_steps=7, started_at=started_at)
        # Initial refresh can block on slow Gateway calls; show progress in the splash.
        self.model.refresh(progress=lambda m, i, t: self._draw_loading(stdscr, msg=m, step=i, total_steps=t, started_at=started_at))
        last_refresh = time.time()
        self._last_refresh_at = last_refresh
        dirty = True
        while True:
            now = time.time()
            # Async refresh: keep UI responsive even if Gateway calls are slow.
            if self._last_refresh_at is None or (now - self._last_refresh_at >= self.refresh_seconds):
                with self._refresh_lock:
                    in_prog = self._refresh_in_progress
                if not in_prog:
                    self._request_refresh()
                    dirty = True

            sessions_all = self.model.sessions
            sessions = self._apply_session_filter(sessions_all)
            items = self._build_list_items(sessions)
            self._reconcile_selection(items)

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
                self._move_selection(items, -1)
                dirty = True
            elif ch in (curses.KEY_DOWN, ord("j")):
                self._move_selection(items, 1)
                dirty = True
            elif ch == ord("r"):
                self._request_refresh()
                last_refresh = time.time()
                dirty = True
            elif ch == ord("l"):
                self.show_logs = not self.show_logs
                dirty = True
            elif ch == ord("d"):
                sv = self._selected_session(items)
                if sv:
                    self._diagnose_selected(sv)
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
            elif ch == ord("t"):
                self.tree_view = not self.tree_view
                self.scroll = 0
                dirty = True
            elif ch == ord("c"):
                self.show_cron = not self.show_cron
                self.scroll = 0
                dirty = True
            elif ch == ord("n"):
                self.node_show_session_label = not self.node_show_session_label
                dirty = True
            elif ch == ord("x"):
                self.focus_mode = not self.focus_mode
                self.scroll = 0
                dirty = True
            elif ch == ord("R"):
                sv = self._selected_session(items)
                if sv:
                    self._rename_selected(stdscr, sv)
                dirty = True
            elif ch == ord("?"):
                self._help_overlay(stdscr)
                dirty = True
            elif ch in (ord("e"), 10, 13):
                sv = self._selected_session(items)
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

            # Rebuild in case view toggles changed.
            sessions_all = self.model.sessions
            sessions = self._apply_session_filter(sessions_all)
            items = self._build_list_items(sessions)
            self._reconcile_selection(items)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            self._draw_header(stdscr, w)

            list_h = h - 3
            list_w = max(52, min(max(52, int(w * 0.55)), max(0, w - 24)))
            detail_w = w - list_w - 1
            self._draw_list(stdscr, y=2, h=list_h, w=list_w, items=items)
            sv = self._selected_session(items)
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
            with self._refresh_lock:
                in_prog = self._refresh_in_progress
                prog_msg = self._refresh_progress_msg
                prog_step = self._refresh_progress_step
                prog_total = self._refresh_progress_total
                err = self._refresh_error
            selectable = [it for it in items if isinstance(it, _ListSession)]
            sel_total = len(selectable)
            sel_pos = 0
            if sv and sel_total:
                try:
                    sel_pos = 1 + [x.sv.meta.key for x in selectable].index(sv.meta.key)
                except ValueError:
                    sel_pos = 0
            refresh_note = ""
            if in_prog:
                if prog_total > 0:
                    refresh_note = f" refreshing={prog_step}/{prog_total}"
                else:
                    refresh_note = " refreshing"
                if prog_msg:
                    refresh_note += f"({prog_msg})"
            elif err:
                refresh_note = f" refreshErr={err[:40]}"
            footer = (
                f"[q]quit [?]help [↑↓]select [r]refresh [f]interval={int(self.refresh_seconds)}s "
                f"[t]{'tree' if self.tree_view else 'flat'} [c]{'cron' if self.show_cron else 'nocron'} "
                f"[x]{'focus' if self.focus_mode else 'all'} "
                f"[n]{'node:label' if self.node_show_session_label else 'node:plain'} "
                f"[R]rename [Enter]nudge [e]export [l]logs  sel={sel_pos}/{sel_total} "
                f"sessions={self._last_shown_sessions}/{self._last_total_sessions} lastRefresh={refresh_age}{refresh_note}"
            )
            self._safe_addnstr(stdscr, h - 1, 0, footer.ljust(w), w, curses.A_REVERSE)

            stdscr.refresh()
            dirty = False
