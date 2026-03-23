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
from .eventlog import Event, EventLog, read_recent_events
from .gateway_logs import GatewayLogTailer
from .config import write_labels
from .locks import LockInfo, lock_path_for_session_file, read_lock
from .model_monitor import ModelProbeOptions, ModelRow, collect_model_rows
from .openclaw_config import OpenClawConfigSnapshot, read_openclaw_config_snapshot
from .openclaw_cron import CronJob, CronRunStatus, CronSnapshot, match_cron_job, read_cron_last_runs, read_cron_snapshot
from .labels import has_user_label, session_display_label
from .redact import redact_text
from .session_keys import is_modelprobe_session_key, parse_session_key
from .reports import write_report_files
from .session_store import SessionMeta, list_sessions
from .session_history import SessionHistoryResult, TaskHistoryEvent, filter_history_events, history_is_stale, load_session_history
from .session_usage import SessionUsageRangeEntry, SessionUsageRangeResult, fetch_sessions_usage_range, history_usage_is_stale
from .state import SessionComputed, WorkState, WorkingSignal, compute_state
from .system_monitor import SystemFamilySummary, SystemSnapshot, collect_system_snapshot
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


def _fmt_tokens_short(value: Optional[int]) -> str:
    if value is None:
        return "-"
    if value < 1000:
        return str(value)
    if value < 100000:
        text = f"{value / 1000.0:.1f}k"
    elif value < 1000000:
        text = f"{round(value / 1000.0):.0f}k"
    else:
        text = f"{value / 1000000.0:.1f}m"
    return text.replace(".0k", "k").replace(".0m", "m")


def _fmt_bytes_short(value: Optional[int]) -> str:
    if value is None:
        return "-"
    size = max(0, int(value))
    if size < 1024:
        return f"{size}B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f}K"
    if size < 1024 * 1024 * 1024:
        text = f"{size / (1024 * 1024):.1f}M"
    else:
        text = f"{size / (1024 * 1024 * 1024):.1f}G"
    return text.replace(".0M", "M").replace(".0G", "G")


def _fmt_kib_short(value: Optional[int]) -> str:
    if value is None:
        return "-"
    return _fmt_bytes_short(int(value) * 1024)


def _fmt_pct_short(value: Optional[float]) -> str:
    if value is None:
        return "-"
    try:
        val = float(value)
    except Exception:
        return "-"
    if abs(val) >= 100:
        return f"{val:.0f}%"
    if abs(val) >= 10:
        return f"{val:.1f}%".replace(".0%", "%")
    return f"{val:.1f}%".replace(".0%", "%")


def _fmt_ratio_pct(numer: Optional[int], denom: Optional[int]) -> str:
    if numer is None or denom is None or denom <= 0:
        return "-"
    try:
        pct = max(0, min(999, int(round((numer / denom) * 100.0))))
    except Exception:
        return "-"
    return f"{pct}%"


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


@dataclass
class _HistoryPaneState:
    load_state: str = "not_loaded"  # not_loaded | loading | ready | error
    result: Optional[SessionHistoryResult] = None
    error: Optional[str] = None
    progress_msg: str = ""
    started_at: Optional[float] = None
    last_loaded_at: Optional[float] = None


@dataclass
class _TokenUsagePaneState:
    load_state: str = "not_loaded"  # not_loaded | loading | ready | error
    result: Optional[SessionUsageRangeResult] = None
    error: Optional[str] = None
    progress_msg: str = ""
    started_at: Optional[float] = None
    last_loaded_at: Optional[float] = None


@dataclass
class _SystemPaneState:
    load_state: str = "not_loaded"  # not_loaded | loading | ready | error
    snapshot: Optional[SystemSnapshot] = None
    error: Optional[str] = None
    progress_msg: str = ""
    started_at: Optional[float] = None
    last_loaded_at: Optional[float] = None


class ModelMonitorState:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._rows: List[ModelRow] = []
        self._lock = threading.Lock()
        self._last_options = ModelProbeOptions()

    @property
    def rows(self) -> List[ModelRow]:
        with self._lock:
            return list(self._rows)

    @property
    def last_options(self) -> ModelProbeOptions:
        with self._lock:
            return self._last_options

    def refresh(
        self,
        *,
        options: Optional[ModelProbeOptions] = None,
        progress: Optional[Callable[[str, int, int], None]] = None,
    ) -> None:
        opts = options or self.last_options
        rows = collect_model_rows(
            openclaw_root=self.cfg.openclaw_root,
            openclaw_bin=self.cfg.openclaw_bin,
            options=opts,
            progress=progress,
        )
        with self._lock:
            self._rows = rows
            self._last_options = opts


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
            if is_modelprobe_session_key(meta.key) and lock is None:
                continue
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
        self.model_monitor = ModelMonitorState(cfg)
        self._loading_art_lines = _load_loading_art_lines()
        self.view_mode = "sessions"
        self.selected = 0
        self.scroll = 0
        self.selected_session_key: Optional[str] = None
        self.model_selected = 0
        self.model_scroll = 0
        self.selected_model_key: Optional[str] = None
        self.system_selected = 0
        self.system_scroll = 0
        self.selected_system_key: str = "__summary__"
        self.system_pane_zoom_mode = "detail90"  # detail90 | even | left100 | detail100
        self.show_logs = True
        self.session_detail_mode = "status"
        self.session_metric_page = "activity"
        self.session_token_window_days = 0  # 0=current snapshot; otherwise usage range days
        self.history_range_days = 1
        self.pane_zoom_mode = "even"  # even | detail | list | sessions
        self.detail_fullscreen = False
        self._history_selected_by_key: Dict[str, int] = {}
        self._history_expanded_by_key: Dict[str, bool] = {}
        self._last_nav_key: Optional[str] = None
        self._last_nav_at: float = 0.0
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
        self._color_section = 0
        self._refresh_lock = threading.Lock()
        self._refresh_in_progress = False
        self._refresh_pending = False
        self._refresh_started_at: Optional[float] = None
        self._refresh_progress_msg: str = ""
        self._refresh_progress_step: int = 0
        self._refresh_progress_total: int = 0
        self._refresh_error: Optional[str] = None
        self._model_refresh_lock = threading.Lock()
        self._model_refresh_in_progress = False
        self._model_refresh_started_at: Optional[float] = None
        self._model_refresh_progress_msg: str = ""
        self._model_refresh_progress_step: int = 0
        self._model_refresh_progress_total: int = 0
        self._model_refresh_error: Optional[str] = None
        self._model_last_refresh_at: Optional[float] = None
        self._rel_cache_session_key: Optional[str] = None
        self._rel_cache_log_count: int = -1
        self._rel_cache_lines: List[str] = []
        self._rel_cache_last_activity: Optional[str] = None
        self._history_lock = threading.Lock()
        self._history_states: Dict[str, _HistoryPaneState] = {}
        self._history_scroll_by_key: Dict[str, int] = {}
        self._token_usage_lock = threading.Lock()
        self._token_usage_states: Dict[int, _TokenUsagePaneState] = {}
        self._system_lock = threading.Lock()
        self._system_state = _SystemPaneState()
        self._system_last_request_manual = False
        self._model_last_request_manual = False
        self._event_cache_mtime: Optional[float] = None
        self._event_cache_items: List[Event] = []
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

    def _set_model_refresh_progress(self, msg: str, step: int, total: int) -> None:
        with self._model_refresh_lock:
            self._model_refresh_progress_msg = msg
            self._model_refresh_progress_step = step
            self._model_refresh_progress_total = total

    def _model_refresh_worker(self) -> None:
        try:
            self.model_monitor.refresh(progress=self._set_model_refresh_progress)
            err = None
        except Exception as e:
            err = str(e)
        with self._model_refresh_lock:
            self._model_refresh_in_progress = False
            self._model_refresh_error = err
            self._model_last_refresh_at = time.time()
            manual = self._model_last_request_manual
            self._model_last_request_manual = False
        if manual:
            if err:
                self.elog.write("model.probe.error", error=err)
            else:
                self.elog.write("model.probe.ready", rows=len(self.model_monitor.rows))

    def _request_model_refresh(self, *, manual: bool = True) -> None:
        with self._model_refresh_lock:
            if self._model_refresh_in_progress:
                return
            self._model_refresh_in_progress = True
            self._model_refresh_started_at = time.time()
            self._model_refresh_error = None
            self._model_refresh_progress_msg = "Refreshing model probes..."
            self._model_refresh_progress_step = 0
            self._model_refresh_progress_total = 0
            self._model_last_request_manual = manual
        if manual:
            self.elog.write("model.probe.requested")
        t = threading.Thread(target=self._model_refresh_worker, name="clawmonitor-model-refresh", daemon=True)
        t.start()

    def _system_snapshot(self) -> Optional[SystemSnapshot]:
        with self._system_lock:
            return self._system_state.snapshot

    def _system_refresh_worker(self) -> None:
        try:
            snap = collect_system_snapshot()
            err = None
        except Exception as e:
            snap = None
            err = str(e)
        with self._system_lock:
            manual = self._system_last_request_manual
            self._system_last_request_manual = False
            if err is None and snap is not None:
                self._system_state = _SystemPaneState(
                    load_state="ready",
                    snapshot=snap,
                    progress_msg="systemctl + ps + /proc/cgroup",
                    started_at=None,
                    last_loaded_at=time.time(),
                )
            else:
                prev = self._system_state.snapshot
                self._system_state = _SystemPaneState(
                    load_state="error",
                    snapshot=prev,
                    error=err or "system refresh failed",
                    progress_msg="systemctl + ps + /proc/cgroup",
                    started_at=None,
                    last_loaded_at=self._system_state.last_loaded_at,
                )
        if manual:
            if err is None and snap is not None:
                self.elog.write(
                    "system.refresh.ready",
                    risk=snap.service_risk,
                    reclaimableKiB=snap.reclaimable_kib,
                    zombies=snap.zombie_count,
                    problematic=snap.problematic_count,
                )
            else:
                self.elog.write("system.refresh.error", error=err or "system refresh failed")

    def _request_system_refresh(self, *, manual: bool = True) -> None:
        with self._system_lock:
            if self._system_state.load_state == "loading":
                return
            self._system_state = _SystemPaneState(
                load_state="loading",
                snapshot=self._system_state.snapshot,
                error=None,
                progress_msg="reading systemctl + ps + /proc cgroups",
                started_at=time.time(),
                last_loaded_at=self._system_state.last_loaded_at,
            )
            self._system_last_request_manual = manual
        if manual:
            self.elog.write("system.refresh.requested")
        t = threading.Thread(target=self._system_refresh_worker, name="clawmonitor-system-refresh", daemon=True)
        t.start()

    def _maybe_request_system_refresh(self) -> None:
        with self._system_lock:
            state = self._system_state
            if state.load_state == "loading":
                return
            if state.last_loaded_at is not None and (time.time() - state.last_loaded_at) < max(2.0, float(self.refresh_seconds)):
                return
        self._request_system_refresh(manual=False)

    def _history_state_for(self, session_key: str) -> _HistoryPaneState:
        with self._history_lock:
            cur = self._history_states.get(session_key)
            if cur is None:
                cur = _HistoryPaneState()
                self._history_states[session_key] = cur
            return cur

    def _history_scroll_for(self, session_key: str) -> int:
        return max(0, int(self._history_scroll_by_key.get(session_key, 0)))

    def _set_history_scroll(self, session_key: str, value: int) -> None:
        self._history_scroll_by_key[session_key] = max(0, int(value))

    def _history_selected_for(self, session_key: str) -> int:
        return max(0, int(self._history_selected_by_key.get(session_key, 0)))

    def _set_history_selected(self, session_key: str, value: int) -> None:
        self._history_selected_by_key[session_key] = max(0, int(value))

    def _history_expanded_for(self, session_key: str) -> bool:
        return bool(self._history_expanded_by_key.get(session_key, False))

    def _toggle_history_expanded(self, session_key: str) -> None:
        self._history_expanded_by_key[session_key] = not self._history_expanded_for(session_key)

    def _history_worker(self, session_key: str, session_id: str, session_file: Path) -> None:
        try:
            result = load_session_history(
                session_key=session_key,
                session_id=session_id,
                session_file=session_file,
            )
            err = None
        except Exception as e:
            result = None
            err = str(e)
        with self._history_lock:
            if err is None and result is not None:
                self._history_states[session_key] = _HistoryPaneState(
                    load_state="ready",
                    result=result,
                    error=None,
                    progress_msg="History ready",
                    started_at=None,
                    last_loaded_at=time.time(),
                )
                self.elog.write("history.load.ready", sessionKey=session_key, mode=result.mode, events=len(result.events))
            else:
                prev = self._history_states.get(session_key)
                self._history_states[session_key] = _HistoryPaneState(
                    load_state="error",
                    result=prev.result if prev else None,
                    error=err or "history load failed",
                    progress_msg="",
                    started_at=None,
                    last_loaded_at=prev.last_loaded_at if prev else None,
                )
                self.elog.write("history.load.error", sessionKey=session_key, error=err or "history load failed")

    def _request_history_load(self, sv: SessionView) -> None:
        if not sv.meta.session_file:
            with self._history_lock:
                self._history_states[sv.meta.key] = _HistoryPaneState(
                    load_state="error",
                    result=None,
                    error="session has no transcript file",
                    progress_msg="",
                    started_at=None,
                    last_loaded_at=None,
                )
            self.elog.write("history.load.error", sessionKey=sv.meta.key, error="session has no transcript file")
            return
        with self._history_lock:
            cur = self._history_states.get(sv.meta.key)
            if cur and cur.load_state == "loading":
                return
            self._history_states[sv.meta.key] = _HistoryPaneState(
                load_state="loading",
                result=cur.result if cur else None,
                error=None,
                progress_msg="Reading transcript and updating history cache...",
                started_at=time.time(),
                last_loaded_at=cur.last_loaded_at if cur else None,
            )
        self.elog.write("history.load.requested", sessionKey=sv.meta.key, sessionId=sv.meta.session_id)
        t = threading.Thread(
            target=self._history_worker,
            args=(sv.meta.key, sv.meta.session_id, sv.meta.session_file),
            name=f"clawmonitor-history-{sv.meta.session_id[:8]}",
            daemon=True,
        )
        t.start()

    def _token_usage_state_for(self, days: int) -> _TokenUsagePaneState:
        key = max(1, int(days))
        with self._token_usage_lock:
            cur = self._token_usage_states.get(key)
            if cur is None:
                cur = _TokenUsagePaneState()
                self._token_usage_states[key] = cur
            return cur

    def _token_usage_worker(self, days: int) -> None:
        try:
            result = fetch_sessions_usage_range(self.cfg.openclaw_bin, days=days)
            err = None
        except Exception as e:
            result = None
            err = str(e)
        with self._token_usage_lock:
            prev = self._token_usage_states.get(days)
            if err is None and result is not None:
                self._token_usage_states[days] = _TokenUsagePaneState(
                    load_state="ready",
                    result=result,
                    error=None,
                    progress_msg="Usage ready",
                    started_at=None,
                    last_loaded_at=time.time(),
                )
                self.elog.write("token.load.ready", days=days, sessions=len(result.sessions_by_key))
            else:
                self._token_usage_states[days] = _TokenUsagePaneState(
                    load_state="error",
                    result=prev.result if prev else None,
                    error=err or "usage load failed",
                    progress_msg="",
                    started_at=None,
                    last_loaded_at=prev.last_loaded_at if prev else None,
                )
                self.elog.write("token.load.error", days=days, error=err or "usage load failed")

    def _request_token_usage_load(self, days: int) -> None:
        if days <= 0:
            return
        with self._token_usage_lock:
            cur = self._token_usage_states.get(days)
            if cur and cur.load_state == "loading":
                return
            self._token_usage_states[days] = _TokenUsagePaneState(
                load_state="loading",
                result=cur.result if cur else None,
                error=None,
                progress_msg=f"Loading {days}d token usage from Gateway...",
                started_at=time.time(),
                last_loaded_at=cur.last_loaded_at if cur else None,
            )
        self.elog.write("token.load.requested", days=days)
        t = threading.Thread(
            target=self._token_usage_worker,
            args=(days,),
            name=f"clawmonitor-token-usage-{days}d",
            daemon=True,
        )
        t.start()

    def _maybe_request_token_usage_load(self, days: int, *, reload_stale: bool = True) -> None:
        if days <= 0:
            return
        state = self._token_usage_state_for(days)
        if state.load_state == "loading":
            return
        if state.result is None or state.load_state in ("not_loaded", "error"):
            self._request_token_usage_load(days)
            return
        if reload_stale and history_usage_is_stale(state.last_loaded_at):
            self._request_token_usage_load(days)

    def _token_usage_entry_for_session(
        self, sv: SessionView
    ) -> Tuple[Optional[_TokenUsagePaneState], Optional[SessionUsageRangeEntry], bool]:
        if self.session_token_window_days <= 0:
            return None, None, False
        state = self._token_usage_state_for(self.session_token_window_days)
        result = state.result
        entry = result.sessions_by_key.get(sv.meta.key) if result else None
        stale = history_usage_is_stale(state.last_loaded_at) if state.load_state == "ready" else False
        return state, entry, stale

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

    def _recent_monitor_events(self, *, limit: int = 8) -> List[Event]:
        path = self.elog.path
        try:
            st = path.stat()
            mtime = st.st_mtime
        except Exception:
            self._event_cache_mtime = None
            self._event_cache_items = []
            return []
        if self._event_cache_mtime != mtime:
            self._event_cache_items = read_recent_events(path, limit=max(20, limit))
            self._event_cache_mtime = mtime
        return list(self._event_cache_items[:limit])

    def _format_monitor_event(self, event: Event) -> str:
        ts = event.ts
        try:
            ts_dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            ts_text = ts_dt.astimezone().strftime("%H:%M:%S")
        except Exception:
            ts_text = ts[:8]
        label = (event.event or "").replace(".", " ")
        data = event.data or {}
        detail = ""
        if "sessionKey" in data:
            detail = _tail_suffix(str(data.get("sessionKey") or ""), n=6)
        elif "days" in data:
            detail = f"{data.get('days')}d"
        elif "rows" in data:
            detail = f"rows={data.get('rows')}"
        elif "risk" in data:
            detail = f"risk={data.get('risk')}"
        elif "error" in data:
            detail = str(data.get("error") or "")[:36]
        if detail:
            return f"{ts_text}  {label}  {detail}"
        return f"{ts_text}  {label}"

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

    def _channel_surface_label(self, channel: Optional[str]) -> str:
        raw = (channel or "").strip()
        if not raw:
            return "-"
        lowered = raw.lower()
        if lowered in ("openclaw-weixin", "weixin", "wechat", "openclaw-wechat"):
            return "weixin"
        return raw

    def _target_prefix_label(self, channel: Optional[str]) -> str:
        raw = (channel or "").strip()
        if not raw:
            return "-"
        lowered = raw.lower()
        if lowered in ("openclaw-weixin", "weixin", "wechat", "openclaw-wechat"):
            return "WEIXIN"
        if lowered == "feishu":
            return "FEISHU"
        if lowered == "telegram":
            return "TELEGRAM"
        return raw.upper()

    def _target_display_text(self, meta: SessionMeta) -> Optional[str]:
        chan = (meta.channel or "").strip()
        target = (meta.to or "").strip()
        if not chan or not target:
            return None
        return f"Target: {self._target_prefix_label(chan)}  target:{chan}:{target}"

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
            return self._channel_surface_label(sv.meta.channel or info.channel or "channel")
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

    def _move_selection_to_edge(self, items: List[ListItem], *, end: bool) -> None:
        if not items:
            return
        indexes = [i for i, item in enumerate(items) if self._is_selectable(item)]
        if not indexes:
            return
        self.selected = indexes[-1] if end else indexes[0]
        sv = self._selected_session(items)
        if sv:
            self.selected_session_key = sv.meta.key

    def _model_key(self, row: ModelRow) -> str:
        return f"{row.target.agent_id}:{row.target.model_ref}"

    def _selected_model(self, rows: List[ModelRow]) -> Optional[ModelRow]:
        if not rows or self.model_selected < 0 or self.model_selected >= len(rows):
            return None
        return rows[self.model_selected]

    def _reconcile_model_selection(self, rows: List[ModelRow]) -> None:
        if not rows:
            self.model_selected = 0
            self.model_scroll = 0
            self.selected_model_key = None
            return
        if self.selected_model_key:
            for idx, row in enumerate(rows):
                if self._model_key(row) == self.selected_model_key:
                    self.model_selected = idx
                    return
        self.model_selected = min(max(0, self.model_selected), len(rows) - 1)
        self.selected_model_key = self._model_key(rows[self.model_selected])

    def _move_model_selection(self, rows: List[ModelRow], delta: int) -> None:
        if not rows:
            return
        self.model_selected = max(0, min(len(rows) - 1, self.model_selected + delta))
        self.selected_model_key = self._model_key(rows[self.model_selected])

    def _move_model_to_edge(self, rows: List[ModelRow], *, end: bool) -> None:
        if not rows:
            return
        self.model_selected = len(rows) - 1 if end else 0
        self.selected_model_key = self._model_key(rows[self.model_selected])

    def _system_row_keys(self, snapshot: Optional[SystemSnapshot]) -> List[str]:
        if snapshot is None:
            return ["__summary__"]
        return ["__summary__"] + [row.family for row in snapshot.families]

    def _selected_system_family(self, snapshot: Optional[SystemSnapshot]) -> Optional[SystemFamilySummary]:
        if snapshot is None:
            return None
        rows = list(snapshot.families)
        if self.system_selected <= 0:
            return None
        idx = self.system_selected - 1
        if idx < 0 or idx >= len(rows):
            return None
        return rows[idx]

    def _reconcile_system_selection(self, snapshot: Optional[SystemSnapshot]) -> None:
        keys = self._system_row_keys(snapshot)
        if not keys:
            self.system_selected = 0
            self.system_scroll = 0
            self.selected_system_key = "__summary__"
            return
        if self.selected_system_key in keys:
            self.system_selected = keys.index(self.selected_system_key)
        else:
            self.system_selected = min(max(0, self.system_selected), len(keys) - 1)
            self.selected_system_key = keys[self.system_selected]

    def _move_system_selection(self, snapshot: Optional[SystemSnapshot], delta: int) -> None:
        keys = self._system_row_keys(snapshot)
        if not keys:
            return
        self.system_selected = max(0, min(len(keys) - 1, self.system_selected + delta))
        self.selected_system_key = keys[self.system_selected]

    def _move_system_to_edge(self, snapshot: Optional[SystemSnapshot], *, end: bool) -> None:
        keys = self._system_row_keys(snapshot)
        if not keys:
            return
        self.system_selected = len(keys) - 1 if end else 0
        self.selected_system_key = keys[self.system_selected]

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

    def _safe_add_segments(
        self,
        stdscr: "curses._CursesWindow",
        y: int,
        x: int,
        segments: List[Tuple[str, int]],
        width: int,
        *,
        pad_attr: int = 0,
    ) -> None:
        if width <= 0:
            return
        h, w = stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        maxw = min(width, max(0, w - x - 2))
        if maxw <= 0:
            return
        cur_x = x
        remaining = maxw
        for raw_text, attr in segments:
            if remaining <= 0:
                break
            text = _sanitize_for_curses(raw_text)
            if not text:
                continue
            clipped = _truncate_cells(text, remaining)
            if not clipped:
                continue
            try:
                if attr:
                    stdscr.addnstr(y, cur_x, clipped, len(clipped), attr)
                else:
                    stdscr.addnstr(y, cur_x, clipped, len(clipped))
            except curses.error:
                break
            seg_w = _display_width(clipped)
            cur_x += seg_w
            remaining -= seg_w
        if remaining > 0:
            self._safe_addnstr(stdscr, y, cur_x, " " * remaining, remaining, pad_attr)

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

    def _view_label(self) -> str:
        return {
            "sessions": "Sessions",
            "models": "Models",
            "system": "System",
        }.get(self.view_mode, self.view_mode.title())

    def _risk_attr(self, risk: str, *, selected: bool = False, loading: bool = False) -> int:
        health_cls = "ok"
        if loading:
            health_cls = "working"
        elif risk == "alert":
            health_cls = "alert"
        elif risk == "warn":
            health_cls = "idle"
        return self._row_attr(health_cls, selected=selected)

    def _section_attr(self, name: str, *, risk: Optional[str] = None, active: bool = False) -> int:
        attr = curses.A_BOLD
        if active:
            attr |= self._color_working if self._colors_enabled else 0
            return attr
        if risk == "alert":
            attr |= self._color_alert if self._colors_enabled else 0
            return attr
        if risk == "warn":
            attr |= self._color_idle if self._colors_enabled else 0
            return attr
        label = (name or "").upper()
        if label in ("SERVICE", "PROCESS DETAIL", "STATUS", "DIRECT PROBE:", "OPENCLAW PROBE:"):
            attr |= self._color_working if self._colors_enabled else 0
        elif label in ("ISSUES", "ERRORS"):
            attr |= self._color_alert if self._colors_enabled else 0
        elif label in ("MONITOR EVENTS", "SELECTED EVENT", "HISTORY DETAIL"):
            attr |= self._color_magenta if self._colors_enabled else 0
        elif label in ("RESOURCES", "COUNTS", "TOKEN", "USAGE", "MODEL VIEW", "SYSTEM ABBREVIATIONS"):
            attr |= self._color_section if self._colors_enabled else 0
        else:
            attr |= self._color_section if self._colors_enabled else 0
        return attr

    def _monitor_event_attr(self, event_name: str) -> int:
        event = (event_name or "").strip().lower()
        attr = 0
        if event.endswith(".error") or event.startswith("labels.write_failed"):
            attr |= self._color_alert if self._colors_enabled else 0
        elif event.endswith(".ready") or event.endswith(".result"):
            attr |= self._color_ok if self._colors_enabled else 0
        elif event.endswith(".requested"):
            attr |= self._color_working if self._colors_enabled else 0
        elif event.startswith(("nudge.", "labels.", "report.")):
            attr |= self._color_magenta if self._colors_enabled else 0
        elif event.startswith(("history.", "token.", "model.", "system.")):
            attr |= self._color_section if self._colors_enabled else 0
        return attr

    def _semantic_attr(self, level: str, *, badge: bool = False) -> int:
        attr = curses.A_BOLD
        key = (level or "").strip().lower()
        if key in ("ok", "ready", "healthy", "done", "success"):
            attr |= self._color_ok if self._colors_enabled else 0
        elif key in ("working", "loading", "running", "active", "progress"):
            attr |= self._color_working if self._colors_enabled else 0
        elif key in ("warn", "waiting", "stale", "degraded", "unknown"):
            attr |= self._color_idle if self._colors_enabled else 0
        elif key in ("alert", "error", "down", "failed"):
            attr |= self._color_alert if self._colors_enabled else 0
        elif key in ("action", "history", "event"):
            attr |= self._color_magenta if self._colors_enabled else 0
        else:
            attr |= self._color_section if self._colors_enabled else 0
        if badge:
            attr |= curses.A_REVERSE
        return attr

    def _selected_attr(self, attr: int, *, selected: bool) -> int:
        if selected:
            return attr | curses.A_REVERSE | curses.A_BOLD
        return attr

    def _attention_attr(self, level: str, *, selected: bool = False, badge: bool = False) -> int:
        attr = self._semantic_attr(level, badge=badge)
        key = (level or "").strip().lower()
        if key in ("alert", "error", "failed", "down"):
            attr |= curses.A_STANDOUT
        elif key in ("warn", "stale", "degraded") and not badge:
            attr |= curses.A_BOLD
        return self._selected_attr(attr, selected=selected)

    def _footer_badge(self, label: str, level: str) -> List[Tuple[str, int]]:
        return [
            (" ", 0),
            (f" {label} ", self._semantic_attr(level, badge=True)),
            (" ", 0),
        ]

    def _system_reclaim_level(self, reclaimable_kib: int) -> str:
        if reclaimable_kib >= 1024 * 1024:
            return "alert"
        if reclaimable_kib >= 256 * 1024:
            return "warn"
        return "ok"

    def _system_problem_level(self, count: int) -> str:
        if count >= 5:
            return "alert"
        if count > 0:
            return "warn"
        return "ok"

    def _system_zombie_level(self, count: int) -> str:
        if count >= 3:
            return "alert"
        if count > 0:
            return "warn"
        return "ok"

    def _system_orphan_level(self, count: int) -> str:
        return "alert" if count > 0 else "ok"

    def _system_helper_level(self, count: int) -> str:
        if count >= 12:
            return "warn"
        return "ok"

    def _session_run_level(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "ok"
        if seconds >= 30 * 60:
            return "alert"
        if seconds >= 10 * 60:
            return "warn"
        return "working"

    def _session_idle_level(self, seconds: Optional[int]) -> str:
        if seconds is None:
            return "ok"
        if seconds >= 24 * 3600:
            return "warn"
        return "ok"

    def _session_state_cell_attr(self, sv: SessionView, health_cls: str, *, selected: bool) -> int:
        state = (sv.computed.state.value or "").strip().upper()
        if state == "WORKING":
            return self._attention_attr("working", selected=selected)
        if state == "FINISHED":
            return self._attention_attr("ok", selected=selected)
        if state in ("NO_MESSAGE", "NOMSG"):
            return self._attention_attr("warn", selected=selected)
        if health_cls == "alert":
            return self._attention_attr("alert", selected=selected)
        return self._selected_attr(self._row_attr(health_cls, selected=False), selected=selected)

    def _session_flag_attr(self, flag_str: str, health_cls: str, *, selected: bool) -> int:
        upper = (flag_str or "").upper()
        if any(token in upper for token in ("NOFB", "DLV", "ZLOCK", "SAFE", "TRXM")):
            return self._attention_attr("alert", selected=selected)
        if "RUN" in upper or "ACPRUN" in upper:
            return self._attention_attr("working", selected=selected)
        if health_cls == "idle":
            return self._attention_attr("warn", selected=selected)
        return self._attention_attr("ok", selected=selected)

    def _token_volume_level(self, value: Optional[int]) -> str:
        if value is None:
            return "ok"
        if value >= 2_000_000:
            return "alert"
        if value >= 500_000:
            return "warn"
        return "ok"

    def _context_level(self, numer: Optional[int], denom: Optional[int]) -> str:
        if numer is None or denom is None or denom <= 0:
            return "ok"
        pct = (numer / denom) * 100.0
        if pct >= 95:
            return "alert"
        if pct >= 80:
            return "warn"
        return "ok"

    def _probe_cell_attr(self, probe: Optional[object], *, selected: bool) -> int:
        if probe is None:
            return self._selected_attr(curses.A_DIM, selected=selected)
        status = getattr(probe, "status", "")
        latency = getattr(probe, "latency_ms", None)
        if status == "ok":
            if latency is not None and latency >= 15000:
                return self._attention_attr("alert", selected=selected)
            if latency is not None and latency >= 5000:
                return self._attention_attr("warn", selected=selected)
            return self._attention_attr("ok", selected=selected)
        return self._attention_attr(status, selected=selected)

    def _service_state_attr(self, active_state: str, sub_state: str) -> int:
        state = f"{active_state}/{sub_state}".strip("/").lower()
        if state in ("active/running", "running", "active"):
            return curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
        if any(token in state for token in ("activating", "reloading", "reload", "starting")):
            return curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        if any(token in state for token in ("failed", "dead", "deactivating", "inactive", "exited")):
            return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
        return curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)

    def _model_status_attr(self, status: str, *, selected: bool = False) -> int:
        value = (status or "").strip().lower()
        if value == "ok":
            return self._row_attr("ok", selected=selected)
        if value in ("degraded", "timeout", "rate_limit", "overloaded", "unsupported", "unknown"):
            return self._row_attr("idle", selected=selected)
        if value in ("running", "loading", "probing", "active"):
            return self._row_attr("working", selected=selected)
        return self._row_attr("alert", selected=selected)

    def _probe_status_attr(self, status: str) -> int:
        return curses.A_BOLD | self._model_status_attr(status)

    def _session_status_line_attr(self, line: str) -> int:
        lower = (line or "").lower()
        if line.startswith(("Task:", "Thinking:", "Trigger:", "Activity:")):
            return self._color_magenta if self._colors_enabled else 0
        if line.startswith("ToolCall:"):
            return curses.A_BOLD | (self._color_magenta if self._colors_enabled else 0)
        if line.startswith("ToolResult:"):
            if " err " in lower or lower.endswith(" err"):
                return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
            return curses.A_BOLD | (self._color_magenta if self._colors_enabled else 0)
        if line.startswith("Last tool error:"):
            return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
        if line.startswith(("Token:", "Context:", "Usage ", "UsageCost ")):
            return curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        if line.startswith("State:"):
            if "working" in lower or "running" in lower:
                return curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
            if "finished" in lower or "done" in lower:
                return curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
            if "no_message" in lower or "nomsg" in lower:
                return curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
            return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
        if line.startswith("Transcript:"):
            if "missing" in lower:
                return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
            if "ok" in lower:
                return curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
        if line.startswith(("Work:", "ACPX:")):
            return curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        if line.startswith("Lock:"):
            if "alive=false" in lower:
                return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
            if "alive=true" in lower:
                return curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        if line.startswith("Delivery FAILED:") or line.startswith("Alerts:"):
            return curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
        if line.startswith("Target:"):
            return curses.A_BOLD | (self._color_section if self._colors_enabled else 0)
        if line.startswith("Telegram Binding:"):
            return curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        if line.startswith("Diagnosis:"):
            if not self._colors_enabled:
                return curses.A_BOLD
            if "(none)" in lower:
                return curses.A_BOLD | self._color_ok
            if "[info]" in lower:
                return curses.A_BOLD | self._color_idle
            return curses.A_BOLD | self._color_alert
        return 0

    def _visible_status_lines(self, lines: List[str], max_lines: int) -> List[str]:
        if max_lines <= 0 or len(lines) <= max_lines:
            return lines[:max_lines] if max_lines > 0 else []
        head_n = min(5, max(1, max_lines - 3))
        tail_n = max(1, max_lines - head_n - 1)
        return lines[:head_n] + ["…"] + lines[-tail_n:]

    def _build_session_status_lines(
        self,
        sv: SessionView,
        *,
        last_activity: Optional[str],
        width: int,
    ) -> List[str]:
        markers = _agent_markers(sv.meta, self.model.config_snapshot)
        mark_str = f" ({','.join(markers)})" if markers else ""
        key_info = parse_session_key(sv.meta.key)
        agent_kind = "configured" if (self.model.config_snapshot and self.model.config_snapshot.configured_agent_ids.get(sv.meta.agent_id, False)) else "implicit"
        status_lines: List[str] = [
            f"SessionKey: {sv.meta.key}",
            f"Agent: {self._agent_label(sv.meta.agent_id)}{mark_str}  Kind: {key_info.kind}/{agent_kind}  Channel: {self._channel_surface_label(sv.meta.channel)}  Account: {sv.meta.account_id or '-'}",
            f"UpdatedAt: {_fmt_dt(sv.updated_at)}",
            f"Transcript: {'MISSING' if sv.transcript_missing else ('-' if not sv.meta.session_file else 'OK')}",
            f"State: {sv.computed.state.value}  Reason: {sv.computed.reason}",
        ]
        target_line = self._target_display_text(sv.meta)
        if target_line:
            status_lines.insert(2, target_line)
        status_lines.extend(self._session_usage_lines(sv))
        status_lines.extend(self._session_range_usage_lines(sv))
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
            status_lines.append(f"Work: {sv.working.kind} pid={sv.working.pid or '-'} since={_fmt_dt(sv.working.created_at)}")
        if sv.lock:
            task_src = sv.tail.last_user_send
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, width), max_lines=2)[:2])
            elif sv.tail.last_trigger and sv.tail.last_trigger.preview:
                status_lines.extend(_wrap_lines(f"Trigger: {redact_text(sv.tail.last_trigger.preview)}", max(10, width), max_lines=2)[:2])
        elif sv.working:
            task_src = sv.tail.last_user_send or sv.tail.last_trigger
            if task_src and task_src.preview:
                status_lines.extend(_wrap_lines(f"Task: {redact_text(task_src.preview)}", max(10, width), max_lines=2)[:2])
        if (sv.lock or sv.working) and sv.tail.last_assistant_thinking:
            think_lines = _wrap_lines(f"Thinking: {redact_text(sv.tail.last_assistant_thinking)}", max(10, width), max_lines=2)
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
        if (sv.lock or sv.working) and last_activity:
            status_lines.extend(_wrap_lines(f"Activity: {redact_text(last_activity)}", max(10, width), max_lines=1)[:1])
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
            status_lines.append(f"Internal: @ {_fmt_dt(sv.internal_activity_at)}  age={_fmt_age(_age_seconds(sv.internal_activity_at)).strip()}")
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
        return status_lines

    def _cycle_system_pane_zoom_mode(self) -> None:
        order = ["detail90", "even", "left100", "detail100"]
        try:
            idx = order.index(self.system_pane_zoom_mode)
        except ValueError:
            idx = 0
        self.system_pane_zoom_mode = order[(idx + 1) % len(order)]

    def _system_pane_zoom_label(self) -> str:
        return {
            "detail90": "10/90",
            "even": "50/50",
            "left100": "left100",
            "detail100": "right100",
        }.get(self.system_pane_zoom_mode, "10/90")

    def _draw_header(self, stdscr: "curses._CursesWindow", width: int) -> None:
        channels = self.model.channels
        mode = "online" if self.model.gateway_log_tailer.available else "offline"
        head = (
            f"ClawMonitor  |  View: {self._view_label()}  |  OpenClaw: {self.cfg.openclaw_root}  |  "
            f"Gateway: {mode}  |  {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S')}"
        )
        self._safe_addnstr(stdscr, 0, 0, head.ljust(width), width, curses.A_REVERSE)
        legend = ""
        if self.view_mode == "sessions":
            legend = (
                "  |  Legend: USER=last user idle  ASST=last assistant idle  RUN=active run duration"
                f"  |  Cols: {self._session_metric_page_label()}"
            )
        elif self.view_mode == "system":
            legend = "  |  Legend: RSS=resident memory  RECL=potential reclaimable estimate  Z=zombies  ORPH=orphans"
        if channels and isinstance(channels.raw.get("channelOrder"), list):
            chan_names = ", ".join([str(x) for x in channels.raw.get("channelOrder", [])])
            self._safe_addnstr(stdscr, 1, 0, f"Channels: {chan_names}{legend}".ljust(width), width)
        else:
            err = self.model.gateway_log_tailer.last_error
            self._safe_addnstr(stdscr, 1, 0, f"Channels: (unavailable) {err or ''}{legend}".ljust(width), width)

    def _model_banner(self) -> Tuple[str, int]:
        with self._model_refresh_lock:
            in_prog = self._model_refresh_in_progress
            started_at = self._model_refresh_started_at
            prog_msg = self._model_refresh_progress_msg
            prog_step = self._model_refresh_progress_step
            prog_total = self._model_refresh_progress_total
            err = self._model_refresh_error
        rows = self.model_monitor.rows
        elapsed = ""
        if started_at is not None and in_prog:
            elapsed = f" elapsed={int(max(0.0, time.time() - started_at))}s"
        if in_prog:
            state = "RUNNING"
            msg = prog_msg or "Running probes..."
            progress = f" {prog_step}/{prog_total}" if prog_total > 0 else ""
            text = f"MODEL PROBE: {state}{progress}{elapsed}  {msg}"
            attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
            return text, attr
        if err:
            text = f"MODEL PROBE: ERROR  {err}"
            attr = curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
            return text, attr
        if self._model_last_refresh_at is not None:
            text = f"MODEL PROBE: DONE  rows={len(rows)}  lastRefresh={_fmt_age(int(time.time() - self._model_last_refresh_at))} ago"
            attr = curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
            return text, attr
        text = "MODEL PROBE: WAITING  Press [r] to start probing configured models."
        attr = curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        return text, attr

    def _token_banner(self) -> Optional[Tuple[str, int]]:
        if self.view_mode != "sessions" or self.session_metric_page != "tokens":
            return None
        if self.session_token_window_days <= 0:
            text = "TOKEN VIEW: NOW  local sessions.json snapshot  [u] window  [←/→] columns"
            attr = curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
            return text, attr
        state = self._token_usage_state_for(self.session_token_window_days)
        label = "WAITING" if state.load_state == "not_loaded" else state.load_state.upper().replace("_", " ")
        if state.load_state == "ready" and history_usage_is_stale(state.last_loaded_at):
            label = "READY/STALE"
        elapsed = ""
        if state.load_state == "loading" and state.started_at is not None:
            elapsed = f" elapsed={int(max(0.0, time.time() - state.started_at))}s"
        detail = state.progress_msg or state.error or ""
        text = (
            f"TOKEN USAGE {self.session_token_window_days}D: {label}{elapsed}  "
            f"{detail or '[u] window  [r] reload  [←/→] columns'}"
        )
        if state.load_state == "loading":
            attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        elif state.load_state == "error":
            attr = curses.A_BOLD | (self._color_alert if self._colors_enabled else 0)
        elif label == "READY/STALE":
            attr = curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
        else:
            attr = curses.A_BOLD | (self._color_ok if self._colors_enabled else 0)
        return text, attr

    def _system_banner(self) -> Tuple[str, int]:
        with self._system_lock:
            state = self._system_state
        elapsed = ""
        if state.load_state == "loading" and state.started_at is not None:
            elapsed = f" elapsed={int(max(0.0, time.time() - state.started_at))}s"
        if state.load_state == "loading":
            text = f"SYSTEM SNAPSHOT: LOADING{elapsed}  {state.progress_msg or 'reading systemctl + ps + /proc cgroups'}"
            return text, curses.A_BOLD | self._risk_attr("ok", loading=True)
        if state.load_state == "error":
            snap = state.snapshot
            if snap is not None:
                sample_age = "-"
                if state.last_loaded_at is not None:
                    sample_age = _fmt_age(int(max(0.0, time.time() - state.last_loaded_at))).strip()
                text = (
                    f"SYSTEM SNAPSHOT: ERROR  {state.error or 'refresh failed'}  "
                    f"using last snapshot age={sample_age} reclaim~{_fmt_kib_short(snap.reclaimable_kib)}"
                )
                return text, curses.A_BOLD | self._risk_attr("alert")
            return f"SYSTEM SNAPSHOT: ERROR  {state.error or 'refresh failed'}", curses.A_BOLD | self._risk_attr("alert")
        if state.snapshot is not None:
            snap = state.snapshot
            sample_age = "-"
            if state.last_loaded_at is not None:
                sample_age = _fmt_age(int(max(0.0, time.time() - state.last_loaded_at))).strip()
            risk = snap.service_risk
            text = (
                f"SYSTEM SNAPSHOT: READY  sampleAge={sample_age}  "
                f"reclaim~{_fmt_kib_short(snap.reclaimable_kib)}  zombies={snap.zombie_count}  "
                f"helpers={snap.helper_process_count}  risk={risk.upper()}"
            )
            return text, curses.A_BOLD | self._risk_attr(risk)
        return (
            "SYSTEM SNAPSHOT: WAITING  Press [r] to inspect service/cgroup state, or wait for auto refresh.",
            curses.A_BOLD | self._risk_attr("warn"),
        )

    def _system_subbanner_segments(self) -> List[Tuple[str, int]]:
        snap = self._system_snapshot()
        label_attr = self._section_attr("SYSTEM ABBREVIATIONS")
        shortcut_attr = curses.A_BOLD | (self._color_magenta if self._colors_enabled else 0)
        normal_attr = curses.A_BOLD
        if snap is None:
            return [
                ("Cols: ", label_attr),
                ("FAMILY", label_attr),
                ("=process group  ", normal_attr),
                ("RISK", label_attr),
                ("=OK/WARN/ALERT  ", normal_attr),
                ("PROC", label_attr),
                ("=count  ", normal_attr),
                ("RSS", label_attr),
                ("=live RSS  ", normal_attr),
                ("Z", label_attr),
                ("=zombies  ", normal_attr),
                ("ORPH", label_attr),
                ("=ppid=1  ", normal_attr),
                ("RECL", label_attr),
                ("=estimated reclaimable  ", normal_attr),
                ("[z]", shortcut_attr),
                (f"={self._system_pane_zoom_label()}", curses.A_BOLD | (self._color_working if self._colors_enabled else 0)),
            ]
        svc_attr = self._service_state_attr(snap.service.active_state, snap.service.sub_state)
        kill_mode_attr = self._risk_attr("ok" if (snap.service.kill_mode or "").strip() == "control-group" else "warn")
        prob_risk = self._system_problem_level(snap.problematic_count)
        reclaim_risk = self._system_reclaim_level(snap.reclaimable_kib)
        return [
            ("Svc", label_attr),
            ("=", normal_attr),
            (f"{snap.service.active_state}/{snap.service.sub_state}", svc_attr),
            ("  KillMode", label_attr),
            ("=", normal_attr),
            (f"{snap.service.kill_mode}", curses.A_BOLD | kill_mode_attr),
            ("  MainPID", label_attr),
            ("=", normal_attr),
            (f"{snap.service.main_pid or '-'}", normal_attr),
            ("  Tasks", label_attr),
            ("=", normal_attr),
            (f"{snap.service.tasks_current or '-'}", normal_attr),
            ("  Mem", label_attr),
            ("=", normal_attr),
            (f"{_fmt_bytes_short(snap.service.memory_current_bytes)}", normal_attr),
            ("  Procs", label_attr),
            ("=", normal_attr),
            (f"{snap.cgroup_process_count}", normal_attr),
            ("  Prob", label_attr),
            ("=", normal_attr),
            (f"{snap.problematic_count}", self._semantic_attr(prob_risk, badge=(prob_risk == "alert"))),
            ("  Reclaim~", label_attr),
            ("=", normal_attr),
            (f"{_fmt_kib_short(snap.reclaimable_kib)}", self._semantic_attr(reclaim_risk, badge=(reclaim_risk == "alert"))),
            ("  [z]", shortcut_attr),
            ("=", normal_attr),
            (f"{self._system_pane_zoom_label()}", curses.A_BOLD | (self._color_working if self._colors_enabled else 0)),
        ]

    def _system_service_line_segments(self, snapshot: SystemSnapshot, *, sample_age: str) -> List[Tuple[str, int]]:
        label_attr = self._section_attr("SERVICE")
        normal_attr = curses.A_BOLD
        return [
            ("Unit", label_attr),
            ("=", normal_attr),
            (snapshot.service.unit_name, normal_attr),
            ("  state", label_attr),
            ("=", normal_attr),
            (f"{snapshot.service.active_state}/{snapshot.service.sub_state}", self._service_state_attr(snapshot.service.active_state, snapshot.service.sub_state)),
            ("  KillMode", label_attr),
            ("=", normal_attr),
            (snapshot.service.kill_mode or "-", self._semantic_attr("ok" if (snapshot.service.kill_mode or "").strip() == "control-group" else "warn", badge=((snapshot.service.kill_mode or "").strip() != "control-group"))),
            ("  risk", label_attr),
            ("=", normal_attr),
            (snapshot.service_risk.upper(), self._semantic_attr(snapshot.service_risk, badge=(snapshot.service_risk == "alert"))),
            ("  sampleAge", label_attr),
            ("=", normal_attr),
            (sample_age, normal_attr),
        ]

    def _system_counts_line_segments(self, snapshot: SystemSnapshot) -> List[Tuple[str, int]]:
        label_attr = self._section_attr("COUNTS")
        normal_attr = curses.A_BOLD
        helper_level = self._system_helper_level(snapshot.helper_process_count)
        zombie_level = self._system_zombie_level(snapshot.zombie_count)
        orphan_level = self._system_orphan_level(snapshot.orphan_count)
        problem_level = self._system_problem_level(snapshot.problematic_count)
        reclaim_level = self._system_reclaim_level(snapshot.reclaimable_kib)
        return [
            ("procs", label_attr),
            ("=", normal_attr),
            (str(snapshot.cgroup_process_count), normal_attr),
            ("  helpers", label_attr),
            ("=", normal_attr),
            (str(snapshot.helper_process_count), self._attention_attr(helper_level, badge=(helper_level == "alert"))),
            ("  zombies", label_attr),
            ("=", normal_attr),
            (str(snapshot.zombie_count), self._attention_attr(zombie_level, badge=(zombie_level == "alert"))),
            ("  orphans", label_attr),
            ("=", normal_attr),
            (str(snapshot.orphan_count), self._attention_attr(orphan_level, badge=(orphan_level == "alert"))),
            ("  problematic", label_attr),
            ("=", normal_attr),
            (str(snapshot.problematic_count), self._attention_attr(problem_level, badge=(problem_level == "alert"))),
            ("  reclaim~", label_attr),
            ("=", normal_attr),
            (_fmt_kib_short(snapshot.reclaimable_kib), self._attention_attr(reclaim_level, badge=(reclaim_level == "alert"))),
        ]

    def _system_operator_note_lines(self, snapshot: Optional[SystemSnapshot]) -> List[str]:
        if snapshot is None:
            return [
                "No system snapshot is loaded yet.",
                "",
                "Press [r] in System view first, then reopen this note.",
            ]
        lines: List[str] = [
            "Operator Note",
            "",
            "This panel is read-only. It does not kill or restart anything.",
            "Use it to estimate whether a later maintenance action is worth it.",
            "",
            f"Current service state: {snapshot.service.active_state}/{snapshot.service.sub_state}",
            f"KillMode: {snapshot.service.kill_mode}",
            f"Potential reclaimable memory estimate: {_fmt_kib_short(snapshot.reclaimable_kib)}",
            f"Problematic processes: {snapshot.problematic_count}",
            f"Zombies: {snapshot.zombie_count}    Orphans: {snapshot.orphan_count}",
            "",
        ]
        if snapshot.reclaimable_kib > 0:
            lines.append(
                f"If you later clean residual helpers or restart the gateway cleanly, the system may recover roughly {_fmt_kib_short(snapshot.reclaimable_kib)} of RSS."
            )
        else:
            lines.append("This snapshot does not currently suggest a large memory recovery opportunity.")
        if (snapshot.service.kill_mode or "").strip() != "control-group":
            lines.extend(
                [
                    "",
                    "Important:",
                    "The runbook strongly prefers KillMode=control-group for the gateway service.",
                    "KillMode=process can leave helper processes behind after restart, which is one of the main reasons TasksCurrent and MemoryCurrent become misleadingly high over time.",
                    "",
                    "Suggested drop-in:",
                    "~/.config/systemd/user/openclaw-gateway.service.d/30-killmode-control-group.conf",
                    "[Service]",
                    "KillMode=control-group",
                ]
            )
        if snapshot.orphan_count > 0 or snapshot.zombie_count > 0 or snapshot.problematic_count > 0:
            lines.extend(
                [
                    "",
                    "Why cleanup may help:",
                    "The snapshot shows residual helpers, zombies, or orphaned processes inside or around the gateway lifecycle.",
                    "That usually means the service has become dirty after long uptime or incomplete restarts.",
                ]
            )
        lines.extend(
            [
                "",
                "Before a maintenance restart, the runbook suggests checking:",
                "systemctl --user show openclaw-gateway.service -p MainPID -p TasksCurrent -p MemoryCurrent -p KillMode -p SubState",
                "systemd-cgls --user-unit openclaw-gateway.service",
                "openclaw gateway probe --timeout 10000 --json",
                "",
                "Standard restart sequence:",
                "systemctl --user daemon-reload",
                "systemctl --user restart openclaw-gateway.service",
                "",
                "After restart, confirm:",
                "- service is running",
                "- KillMode=control-group",
                "- TasksCurrent and MemoryCurrent dropped",
                "- only the current gateway generation remains in the cgroup",
                "- gateway probe is healthy",
                "",
                "Risk warning:",
                "A restart interrupts in-flight runs that belong to openclaw-gateway.service.",
                "KillMode=control-group does not create that risk; it makes the restart cleanup complete and predictable.",
                "",
                "Practical rule:",
                "Do not restart while a critical long-running task is active unless interruption is acceptable.",
            ]
        )
        return lines

    def _session_cache_tokens(self, sv: SessionView) -> Optional[int]:
        vals = [sv.meta.cache_read_tokens or 0, sv.meta.cache_write_tokens or 0]
        total = sum(vals)
        return total if total > 0 else None

    def _session_context_pct(self, sv: SessionView) -> str:
        return _fmt_ratio_pct(sv.meta.total_tokens, sv.meta.context_tokens)

    def _session_usage_lines(self, sv: SessionView) -> List[str]:
        provider = sv.meta.model_provider
        model = sv.meta.model_name
        if sv.tail.last_assistant:
            provider = provider or sv.tail.last_assistant.provider
            model = model or sv.tail.last_assistant.model
        model_text = f"{provider or '-'} / {model or '-'}"
        fresh = "-"
        if sv.meta.total_tokens_fresh is True:
            fresh = "yes"
        elif sv.meta.total_tokens_fresh is False:
            fresh = "no"
        line1 = (
            f"Token: in={_fmt_tokens_short(sv.meta.input_tokens)} "
            f"out={_fmt_tokens_short(sv.meta.output_tokens)} "
            f"cacheR={_fmt_tokens_short(sv.meta.cache_read_tokens)} "
            f"cacheW={_fmt_tokens_short(sv.meta.cache_write_tokens)} "
            f"fresh={fresh}"
        )
        line2 = (
            f"Context: prompt={_fmt_tokens_short(sv.meta.total_tokens)} / {_fmt_tokens_short(sv.meta.context_tokens)} "
            f"used={self._session_context_pct(sv)}  model={model_text}"
        )
        return [line1, line2]

    def _session_range_usage_lines(self, sv: SessionView) -> List[str]:
        state, entry, stale = self._token_usage_entry_for_session(sv)
        if state is None:
            return []
        status = "WAITING" if state.load_state == "not_loaded" else state.load_state.upper().replace("_", " ")
        if stale and state.load_state == "ready":
            status = "READY/STALE"
        elapsed = ""
        if state.load_state == "loading" and state.started_at is not None:
            elapsed = f" elapsed={int(max(0.0, time.time() - state.started_at))}s"
        head = f"Usage {self.session_token_window_days}d: {status}{elapsed}"
        if state.load_state == "not_loaded":
            return [f"{head}  Press [r] to load Gateway usage."]
        if state.load_state == "loading":
            return [f"{head}  {state.progress_msg or ''}".strip()]
        if state.load_state == "error":
            return [f"{head}  {state.error or 'usage load failed'}"]
        if entry is None:
            return [f"{head}  No usage row for this session in the selected window."]
        totals = entry.totals
        line1 = (
            f"{head}  in={_fmt_tokens_short(totals.input_tokens)} "
            f"out={_fmt_tokens_short(totals.output_tokens)} "
            f"cache={_fmt_tokens_short(totals.cache_read_tokens + totals.cache_write_tokens)} "
            f"total={_fmt_tokens_short(totals.total_tokens)}"
        )
        line2 = (
            f"UsageCost {self.session_token_window_days}d: ${totals.total_cost:.3f} "
            f"msgs={totals.message_count} errors={totals.error_count}"
        )
        return [line1, line2]

    def _session_list_layout(self, width: int) -> Dict[str, int | bool | str]:
        page = self.session_metric_page
        if page == "tokens":
            if width < 56:
                return {
                    "page": page,
                    "node_w": max(7, min(10, width // 5)),
                    "state_w": 6,
                    "tok_in_w": 6,
                    "tok_out_w": 6,
                    "tok_cache_w": 0,
                    "tok_ctx_w": 0,
                    "show_tok_in": True,
                    "show_tok_out": True,
                    "show_tok_cache": False,
                    "show_tok_ctx": False,
                }
            if width < 74:
                return {
                    "page": page,
                    "node_w": max(8, min(11, int(width * 0.16))),
                    "state_w": 6,
                    "tok_in_w": 6,
                    "tok_out_w": 6,
                    "tok_cache_w": 0,
                    "tok_ctx_w": 5,
                    "show_tok_in": True,
                    "show_tok_out": True,
                    "show_tok_cache": False,
                    "show_tok_ctx": True,
                }
            if width < 92:
                return {
                    "page": page,
                    "node_w": max(8, min(12, int(width * 0.16))),
                    "state_w": 7,
                    "tok_in_w": 6,
                    "tok_out_w": 6,
                    "tok_cache_w": 6,
                    "tok_ctx_w": 5,
                    "show_tok_in": True,
                    "show_tok_out": True,
                    "show_tok_cache": True,
                    "show_tok_ctx": True,
                }
            return {
                "page": page,
                "node_w": max(9, min(14, int(width * 0.16))),
                "state_w": 8,
                "tok_in_w": 7,
                "tok_out_w": 7,
                "tok_cache_w": 7,
                "tok_ctx_w": 6,
                "show_tok_in": True,
                "show_tok_out": True,
                "show_tok_cache": True,
                "show_tok_ctx": True,
            }
        if width < 56:
            node_w = max(7, min(10, width // 5))
            state_w = 6
            return {
                "page": page,
                "node_w": node_w,
                "state_w": state_w,
                "flags_w": 0,
                "show_u_age": False,
                "show_a_age": False,
                "show_run": False,
                "show_flags": False,
            }
        if width < 74:
            node_w = max(8, min(11, int(width * 0.16)))
            state_w = 6
            return {
                "page": page,
                "node_w": node_w,
                "state_w": state_w,
                "flags_w": 0,
                "show_u_age": True,
                "show_a_age": True,
                "show_run": True,
                "show_flags": False,
            }
        if width < 92:
            node_w = max(8, min(12, int(width * 0.16)))
            state_w = 7
            flags_w = max(7, min(9, int(width * 0.12)))
            return {
                "page": page,
                "node_w": node_w,
                "state_w": state_w,
                "flags_w": flags_w,
                "show_u_age": True,
                "show_a_age": True,
                "show_run": True,
                "show_flags": True,
            }
        node_w = max(9, min(14, int(width * 0.16)))
        state_w = 8
        flags_w = max(7, min(10, int(width * 0.12)))
        return {
            "page": page,
            "node_w": node_w,
            "state_w": state_w,
            "flags_w": flags_w,
            "show_u_age": True,
            "show_a_age": True,
            "show_run": True,
            "show_flags": True,
        }

    def _footer_height(self, total_h: int) -> int:
        return 2 if total_h >= 12 else 1

    def _draw_footer(self, stdscr: "curses._CursesWindow", width: int, lines: List[Union[str, List[Tuple[str, int]]]]) -> None:
        h, _ = stdscr.getmaxyx()
        footer_h = self._footer_height(h)
        start_y = h - footer_h
        render_lines = lines[-footer_h:]
        while len(render_lines) < footer_h:
            render_lines.insert(0, "")
        for idx, line in enumerate(render_lines):
            row_y = start_y + idx
            if isinstance(line, list):
                self._safe_addnstr(stdscr, row_y, 0, " ".ljust(width), width)
                self._safe_add_segments(stdscr, row_y, 0, line, width)
            else:
                self._safe_addnstr(stdscr, row_y, 0, line.ljust(width), width, curses.A_REVERSE)

    def _cycle_pane_zoom_mode(self) -> None:
        order = ["sessions", "even", "detail", "list"]
        try:
            idx = order.index(self.pane_zoom_mode)
        except ValueError:
            idx = 0
        self.pane_zoom_mode = order[(idx + 1) % len(order)]

    def _pane_zoom_label(self) -> str:
        return {
            "even": "50/50",
            "detail": "detail",
            "list": "left80",
            "sessions": "left100",
        }.get(self.pane_zoom_mode, "50/50")

    def _session_metric_pages(self) -> List[str]:
        return ["activity", "tokens"]

    def _token_window_label(self) -> str:
        if self.session_token_window_days <= 0:
            return "now"
        return f"{self.session_token_window_days}d"

    def _session_metric_page_label(self) -> str:
        return {
            "activity": "activity",
            "tokens": f"tokens-{self._token_window_label()}",
        }.get(self.session_metric_page, self.session_metric_page)

    def _cycle_session_metric_page(self, delta: int) -> None:
        pages = self._session_metric_pages()
        try:
            idx = pages.index(self.session_metric_page)
        except ValueError:
            idx = 0
        self.session_metric_page = pages[(idx + delta) % len(pages)]

    def _cycle_session_token_window(self) -> None:
        windows = [0, 1, 7, 30]
        try:
            idx = windows.index(self.session_token_window_days)
        except ValueError:
            idx = 0
        self.session_token_window_days = windows[(idx + 1) % len(windows)]

    def _surface_is_default(self) -> bool:
        return (
            self.view_mode == "sessions"
            and self.session_detail_mode == "status"
            and self.session_metric_page == "activity"
            and self.session_token_window_days == 0
            and self.pane_zoom_mode == "even"
            and not self.detail_fullscreen
        )

    def _reset_surface_state(self) -> None:
        self.view_mode = "sessions"
        self.session_detail_mode = "status"
        self.session_metric_page = "activity"
        self.session_token_window_days = 0
        self.pane_zoom_mode = "even"
        self.detail_fullscreen = False

    def _switch_view_mode(self, mode: str) -> None:
        self.view_mode = mode
        if mode == "system":
            self._maybe_request_system_refresh()

    def _footer_refresh_segments(
        self,
        *,
        label: str,
        refresh_age: str,
        in_prog: bool,
        err: Optional[str],
        prog_step: int,
        prog_total: int,
        prog_msg: str,
    ) -> List[Tuple[str, int]]:
        parts: List[Tuple[str, int]] = []
        if in_prog:
            badge = label
            if prog_total > 0:
                badge += f" {prog_step}/{prog_total}"
            parts.extend(self._footer_badge(badge, "loading"))
            if prog_msg:
                parts.append((prog_msg[:28], self._semantic_attr("working")))
        elif err:
            parts.extend(self._footer_badge(f"{label} ERROR", "error"))
            parts.append((err[:36], self._semantic_attr("alert")))
        else:
            parts.extend(self._footer_badge(f"{label} READY", "ready"))
            parts.append((f"age={refresh_age}", self._semantic_attr("ok")))
        return parts

    def _session_footer_status_segments(
        self,
        *,
        refresh_age: str,
        in_prog: bool,
        err: Optional[str],
        prog_step: int,
        prog_total: int,
        prog_msg: str,
        sel_pos: int,
        sel_total: int,
        sv: Optional[SessionView],
    ) -> List[Tuple[str, int]]:
        segs: List[Tuple[str, int]] = []
        segs.extend(self._footer_badge(f"SESSIONS {self.session_detail_mode.upper()}", "info"))
        segs.extend(
            self._footer_refresh_segments(
                label="REFRESH",
                refresh_age=refresh_age,
                in_prog=in_prog,
                err=err,
                prog_step=prog_step,
                prog_total=prog_total,
                prog_msg=prog_msg,
            )
        )
        if self.session_detail_mode == "history" and sv:
            hs, _, stale = self._history_events_for_view(sv)
            label = hs.load_state.upper().replace("_", " ")
            level = "warn" if hs.load_state == "not_loaded" else hs.load_state
            if stale and hs.load_state == "ready":
                label = "READY/STALE"
                level = "stale"
            segs.extend(self._footer_badge(f"HIST {label} {self.history_range_days}D", level))
        if self.session_metric_page == "tokens":
            if self.session_token_window_days <= 0:
                segs.extend(self._footer_badge("TOK NOW", "ready"))
            else:
                tus = self._token_usage_state_for(self.session_token_window_days)
                label = tus.load_state.upper().replace("_", " ")
                level = "warn" if tus.load_state == "not_loaded" else tus.load_state
                if tus.load_state == "ready" and history_usage_is_stale(tus.last_loaded_at):
                    label = "READY/STALE"
                    level = "stale"
                segs.extend(self._footer_badge(f"TOK {self.session_token_window_days}D {label}", level))
        if sv and sv.findings:
            sev = (sv.findings[0].severity or "").lower()
            level = "alert" if sev in ("error", "critical", "high", "warn", "warning") else "warn"
            segs.extend(self._footer_badge(f"DIAG {sv.findings[0].id}", level))
        elif sv and (sv.delivery_failure or sv.computed.no_feedback or sv.computed.safety_alert or sv.computed.safeguard_alert):
            segs.extend(self._footer_badge("ALERTS", "alert"))
        segs.append((f"sel={sel_pos}/{sel_total} ", curses.A_BOLD))
        segs.append((f"cols={self._session_metric_page_label()} panes={self._pane_zoom_label()} ", self._semantic_attr("info")))
        segs.append((f"full={'on' if self.detail_fullscreen else 'off'}", curses.A_BOLD))
        return segs

    def _model_footer_status_segments(
        self,
        *,
        refresh_age: str,
        in_prog: bool,
        err: Optional[str],
        prog_step: int,
        prog_total: int,
        prog_msg: str,
        sel_pos: int,
        sel_total: int,
        row_count: int,
    ) -> List[Tuple[str, int]]:
        segs: List[Tuple[str, int]] = []
        segs.extend(self._footer_badge("MODELS", "info"))
        segs.extend(
            self._footer_refresh_segments(
                label="PROBE",
                refresh_age=refresh_age,
                in_prog=in_prog,
                err=err,
                prog_step=prog_step,
                prog_total=prog_total,
                prog_msg=prog_msg,
            )
        )
        segs.append((f"sel={sel_pos}/{sel_total} rows={row_count}", curses.A_BOLD))
        return segs

    def _system_footer_status_segments(
        self,
        *,
        refresh_age: str,
        in_prog: bool,
        err: Optional[str],
        prog_msg: str,
        sel_pos: int,
        sel_total: int,
        snapshot: Optional[SystemSnapshot],
    ) -> List[Tuple[str, int]]:
        segs: List[Tuple[str, int]] = []
        segs.extend(self._footer_badge("SYSTEM", "info"))
        segs.extend(
            self._footer_refresh_segments(
                label="SNAPSHOT",
                refresh_age=refresh_age,
                in_prog=in_prog,
                err=err,
                prog_step=0,
                prog_total=0,
                prog_msg=prog_msg,
            )
        )
        if snapshot is not None:
            segs.extend(self._footer_badge(f"RISK {snapshot.service_risk.upper()}", snapshot.service_risk))
            segs.append(("z=", self._section_attr("COUNTS")))
            segs.append((str(snapshot.zombie_count), self._semantic_attr(self._system_zombie_level(snapshot.zombie_count), badge=(self._system_zombie_level(snapshot.zombie_count) == "alert"))))
            segs.append(("  orph=", self._section_attr("COUNTS")))
            segs.append((str(snapshot.orphan_count), self._semantic_attr(self._system_orphan_level(snapshot.orphan_count), badge=(self._system_orphan_level(snapshot.orphan_count) == "alert"))))
            segs.append(("  prob=", self._section_attr("COUNTS")))
            segs.append((str(snapshot.problematic_count), self._semantic_attr(self._system_problem_level(snapshot.problematic_count), badge=(self._system_problem_level(snapshot.problematic_count) == "alert"))))
            segs.append(("  reclaim~", self._section_attr("COUNTS")))
            segs.append((_fmt_kib_short(snapshot.reclaimable_kib), self._semantic_attr(self._system_reclaim_level(snapshot.reclaimable_kib), badge=(self._system_reclaim_level(snapshot.reclaimable_kib) == "alert"))))
            segs.append(("  ", 0))
        segs.append((f"sel={sel_pos}/{sel_total} panes={self._system_pane_zoom_label()}", curses.A_BOLD))
        return segs

    def _cycle_view_mode(self) -> None:
        order = ["sessions", "models", "system"]
        try:
            idx = order.index(self.view_mode)
        except ValueError:
            idx = 0
        self._switch_view_mode(order[(idx + 1) % len(order)])

    def _should_jump_agent(self, key_name: str) -> bool:
        now = time.time()
        if self._last_nav_key == key_name and (now - self._last_nav_at) <= 0.35:
            self._last_nav_key = None
            self._last_nav_at = 0.0
            return True
        self._last_nav_key = key_name
        self._last_nav_at = now
        return False

    def _move_selection_agent(self, items: List[ListItem], direction: int) -> None:
        sv = self._selected_session(items)
        if not sv or not self.tree_view:
            self._move_selection(items, direction)
            return
        current_agent = sv.meta.agent_id or "-"
        selectable_indexes = [i for i, item in enumerate(items) if isinstance(item, _ListSession)]
        if not selectable_indexes:
            return
        current_idx = self.selected
        if direction > 0:
            search = [i for i in selectable_indexes if i > current_idx]
            target = None
            for idx in search:
                item = items[idx]
                if not isinstance(item, _ListSession):
                    continue
                if (item.sv.meta.agent_id or "-") != current_agent:
                    target = idx
                    break
        else:
            prev_agent = None
            target = None
            for idx in reversed(selectable_indexes):
                if idx >= current_idx:
                    continue
                item = items[idx]
                if not isinstance(item, _ListSession):
                    continue
                agent_id = item.sv.meta.agent_id or "-"
                if agent_id == current_agent and prev_agent is None:
                    continue
                if prev_agent is None and agent_id != current_agent:
                    prev_agent = agent_id
                    target = idx
                    continue
                if prev_agent is not None:
                    if agent_id == prev_agent:
                        target = idx
                        continue
                    break
        if target is None:
            end_idx = selectable_indexes[-1] if direction > 0 else selectable_indexes[0]
            target = end_idx
        self.selected = target
        new_sv = self._selected_session(items)
        if new_sv:
            self.selected_session_key = new_sv.meta.key

    def _draw_list(self, stdscr: "curses._CursesWindow", y: int, h: int, w: int, items: List[ListItem]) -> None:
        layout = self._session_list_layout(w)
        node_w = int(layout["node_w"])
        state_w = int(layout["state_w"])
        page = str(layout.get("page") or "activity")
        range_result: Optional[SessionUsageRangeResult] = None
        if page == "tokens" and self.session_token_window_days > 0:
            range_result = self._token_usage_state_for(self.session_token_window_days).result
        header_parts = [_fit("NODE", node_w), _fit("STATE", state_w)]
        flags_w = int(layout.get("flags_w") or 0)
        if page == "tokens":
            if bool(layout["show_tok_in"]):
                header_parts.append(_fit("IN", int(layout["tok_in_w"])))
            if bool(layout["show_tok_out"]):
                header_parts.append(_fit("OUT", int(layout["tok_out_w"])))
            if bool(layout["show_tok_cache"]):
                header_parts.append(_fit("CACHE", int(layout["tok_cache_w"])))
            if bool(layout["show_tok_ctx"]):
                header_parts.append(_fit("TOT" if self.session_token_window_days > 0 else "CTX", int(layout["tok_ctx_w"])))
        else:
            if bool(layout["show_u_age"]):
                header_parts.append("USER")
            if bool(layout["show_a_age"]):
                header_parts.append("ASST")
            if bool(layout["show_run"]):
                header_parts.append("RUN")
            if bool(layout["show_flags"]):
                header_parts.append(_fit("FLAGS", flags_w))
        header_parts.append("SESSION")
        header = "  ".join(header_parts)
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
                parts = [
                    _fit(node_text, node_w),
                    _fit(status, state_w),
                ]
                if page == "tokens":
                    if bool(layout["show_tok_in"]):
                        parts.append(_fit("-", int(layout["tok_in_w"])))
                    if bool(layout["show_tok_out"]):
                        parts.append(_fit("-", int(layout["tok_out_w"])))
                    if bool(layout["show_tok_cache"]):
                        parts.append(_fit("-", int(layout["tok_cache_w"])))
                    if bool(layout["show_tok_ctx"]):
                        parts.append(_fit("-", int(layout["tok_ctx_w"])))
                else:
                    if bool(layout["show_u_age"]):
                        parts.append(f"{'-':>5}")
                    if bool(layout["show_a_age"]):
                        parts.append(f"{'-':>5}")
                    if bool(layout["show_run"]):
                        parts.append(f"{run_age:>5}")
                    if bool(layout["show_flags"]):
                        parts.append(_fit(flags, flags_w))
                parts.append(sess)
                line = "  ".join(parts)
                self._safe_addnstr(stdscr, row_y, 0, _fit(line, w).ljust(w), w)
                continue

            sv = it.sv
            user_msg = sv.tail.last_user_send
            u_age_sec = _age_seconds(user_msg.ts if user_msg else sv.updated_at)
            a_age_sec = _age_seconds(sv.tail.last_assistant.ts if sv.tail.last_assistant else None)
            u_age = _fmt_age(u_age_sec)
            a_age = _fmt_age(a_age_sec)
            run = "-"
            run_at = sv.lock.created_at if sv.lock else (sv.working.created_at if sv.working else None)
            run_sec: Optional[int] = None
            if run_at:
                run_sec = int((datetime.now(timezone.utc) - run_at).total_seconds())
                run = _fmt_age(run_sec)
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
            selected = (idx == self.selected)
            row_attr = self._row_attr(health_cls, selected=selected)
            segments: List[Tuple[str, int]] = [
                (_fit(node_text, node_w), row_attr),
                ("  ", row_attr),
                (_fit(self._state_short_label(sv.computed.state.value), state_w), self._session_state_cell_attr(sv, health_cls, selected=selected)),
            ]
            if page == "tokens":
                range_entry = range_result.sessions_by_key.get(sv.meta.key) if range_result else None
                if bool(layout["show_tok_in"]):
                    val = range_entry.totals.input_tokens if self.session_token_window_days > 0 and range_entry else (sv.meta.input_tokens if self.session_token_window_days == 0 else None)
                    text = _fmt_tokens_short(val) if val is not None else "-"
                    segments.extend([("  ", row_attr), (_fit(text, int(layout["tok_in_w"])), self._selected_attr(self._semantic_attr(self._token_volume_level(val)), selected=selected))])
                if bool(layout["show_tok_out"]):
                    val = range_entry.totals.output_tokens if self.session_token_window_days > 0 and range_entry else (sv.meta.output_tokens if self.session_token_window_days == 0 else None)
                    text = _fmt_tokens_short(val) if val is not None else "-"
                    segments.extend([("  ", row_attr), (_fit(text, int(layout["tok_out_w"])), self._selected_attr(self._semantic_attr(self._token_volume_level(val)), selected=selected))])
                if bool(layout["show_tok_cache"]):
                    val = (
                        range_entry.totals.cache_read_tokens + range_entry.totals.cache_write_tokens
                        if self.session_token_window_days > 0 and range_entry
                        else (self._session_cache_tokens(sv) if self.session_token_window_days == 0 else None)
                    )
                    text = _fmt_tokens_short(val) if val is not None else "-"
                    segments.extend([("  ", row_attr), (_fit(text, int(layout["tok_cache_w"])), self._selected_attr(self._semantic_attr(self._token_volume_level(val)), selected=selected))])
                if bool(layout["show_tok_ctx"]):
                    if self.session_token_window_days > 0:
                        val = range_entry.totals.total_tokens if range_entry else None
                        text = _fmt_tokens_short(val) if val is not None else "-"
                        attr = self._selected_attr(self._semantic_attr(self._token_volume_level(val)), selected=selected)
                    else:
                        val = sv.meta.total_tokens
                        text = self._session_context_pct(sv)
                        attr = self._selected_attr(self._semantic_attr(self._context_level(sv.meta.total_tokens, sv.meta.context_tokens)), selected=selected)
                    segments.extend([("  ", row_attr), (_fit(text, int(layout["tok_ctx_w"])), attr)])
            else:
                if bool(layout["show_u_age"]):
                    segments.extend([("  ", row_attr), (f"{u_age:>5}", self._selected_attr(self._semantic_attr(self._session_idle_level(u_age_sec)), selected=selected))])
                if bool(layout["show_a_age"]):
                    segments.extend([("  ", row_attr), (f"{a_age:>5}", self._selected_attr(self._semantic_attr(self._session_idle_level(a_age_sec)), selected=selected))])
                if bool(layout["show_run"]):
                    segments.extend([("  ", row_attr), (f"{run:>5}", self._selected_attr(self._semantic_attr(self._session_run_level(run_sec)), selected=selected))])
                if bool(layout["show_flags"]):
                    segments.extend([("  ", row_attr), (_fit(flag_str, flags_w), self._session_flag_attr(flag_str, health_cls, selected=selected))])
            segments.extend([("  ", row_attr), (it.key_tail, row_attr)])
            self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
            self._safe_add_segments(stdscr, row_y, 0, segments, w, pad_attr=row_attr)

    def _state_short_label(self, value: str) -> str:
        mapping = {
            "WORKING": "WORK",
            "FINISHED": "DONE",
            "INTERRUPTED": "INT",
            "NO_MESSAGE": "NOMSG",
        }
        return mapping.get((value or "").strip(), (value or "").strip()[:8])

    def _model_health_class(self, row: ModelRow) -> str:
        status = (row.overall_status or "").strip()
        if status == "ok":
            return "ok"
        if status in ("degraded", "timeout", "rate_limit", "overloaded", "unsupported", "unknown"):
            return "idle"
        return "alert"

    def _probe_metric(self, probe: Optional[ModelRow | object]) -> str:
        if probe is None:
            return "-"
        latency = getattr(probe, "latency_ms", None)
        if latency is None:
            return "-"
        return f"{latency}ms"

    def _draw_model_list(self, stdscr: "curses._CursesWindow", y: int, h: int, w: int, rows: List[ModelRow]) -> None:
        model_w = max(18, min(30, int(w * 0.34)))
        role_w = max(10, min(18, int(w * 0.16)))
        state_w = 10
        probe_w = 13
        header = (
            f"{_fit('STATE', state_w)}  {_fit('AGENT', 14)}  {_fit('MODEL', model_w)}  "
            f"{_fit('ROLES', role_w)}  {_fit('DIRECT', probe_w)}  {_fit('CLAW', probe_w)}"
        )
        self._safe_addnstr(stdscr, y, 0, header.ljust(w), w, curses.A_BOLD)
        body_y = y + 1
        visible = max(0, h - 1)
        if self.model_selected < self.model_scroll:
            self.model_scroll = self.model_selected
        if self.model_selected >= self.model_scroll + visible:
            self.model_scroll = self.model_selected - visible + 1
        for i in range(visible):
            idx = self.model_scroll + i
            row_y = body_y + i
            if idx >= len(rows):
                self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
                continue
            row = rows[idx]
            selected = (idx == self.model_selected)
            row_attr = self._row_attr(self._model_health_class(row), selected=selected)
            segments: List[Tuple[str, int]] = [
                (_fit(row.overall_status.upper(), state_w), self._selected_attr(self._model_status_attr(row.overall_status), selected=selected)),
                ("  ", row_attr),
                (_fit(row.target.agent_label, 14), row_attr),
                ("  ", row_attr),
                (_fit(row.target.model_ref, model_w), row_attr),
                ("  ", row_attr),
                (_fit(",".join(row.target.roles), role_w), row_attr),
                ("  ", row_attr),
                (_fit(self._probe_metric(row.direct), probe_w), self._probe_cell_attr(row.direct, selected=selected)),
                ("  ", row_attr),
                (_fit(self._probe_metric(row.openclaw), probe_w), self._probe_cell_attr(row.openclaw, selected=selected)),
            ]
            self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
            self._safe_add_segments(stdscr, row_y, 0, segments, w, pad_attr=row_attr)

    def _draw_model_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, row: Optional[ModelRow]) -> None:
        for j in range(h):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)
        if not row:
            self._safe_addnstr(stdscr, y, x, "No model rows. Press [r] to probe configured models.", w)
            return

        lines: List[str] = [
            f"Model: {row.target.model_ref}",
            f"Label: {row.target.model_label or '-'}",
            f"Agent: {row.target.agent_label} ({row.target.agent_id})",
            f"Roles: {','.join(row.target.roles) or '-'}",
            f"API: {row.target.api_kind or '-'}",
            f"Base URL: {row.target.base_url or '-'}",
            f"Auth Source: {row.target.auth_source or '-'}",
            f"Overall: {row.overall_status.upper()}  Connection: {row.overall_connection}",
            f"Summary: {row.summary}",
            "",
            "Direct Probe:",
        ]
        lines.extend(self._probe_lines(row.direct, width=w))
        lines.append("")
        lines.append("OpenClaw Probe:")
        lines.extend(self._probe_lines(row.openclaw, width=w))

        for i in range(min(h, len(lines))):
            attr = 0
            if lines[i] in ("Direct Probe:", "OpenClaw Probe:"):
                attr = self._section_attr(lines[i])
            elif lines[i].startswith("Overall:"):
                attr = curses.A_BOLD | self._model_status_attr(row.overall_status)
            elif lines[i].startswith("Summary:"):
                attr = self._model_status_attr(row.overall_status)
            elif lines[i].startswith("  status="):
                status_text = lines[i].split("status=", 1)[1].split(None, 1)[0].strip()
                attr = self._probe_status_attr(status_text)
            elif lines[i].startswith("  detail:"):
                attr = self._color_idle if self._colors_enabled else 0
            elif lines[i].startswith("  reply:"):
                attr = self._color_magenta if self._colors_enabled else 0
            self._safe_addnstr(stdscr, y + i, x, _fit(lines[i], w), w, attr)

    def _draw_system_list(self, stdscr: "curses._CursesWindow", y: int, h: int, w: int, snapshot: Optional[SystemSnapshot]) -> None:
        if w < 28:
            fam_w = max(8, w - 9)
            risk_w = 6
            count_w = 0
            rss_w = 0
            cpu_w = 0
            z_w = 0
            orph_w = 0
            recl_w = 0
        elif w < 40:
            fam_w = max(10, w - 19)
            risk_w = 6
            count_w = 5
            rss_w = 0
            cpu_w = 0
            z_w = 0
            orph_w = 0
            recl_w = 0
        else:
            fam_w = max(12, min(22, int(w * 0.28)))
            risk_w = 6
            count_w = 5
            rss_w = 7
            cpu_w = 6
            z_w = 3
            orph_w = 4
            recl_w = 7
        show_cpu = cpu_w > 0 and w >= 58
        show_orph = orph_w > 0 and w >= 66
        header_parts = [
            _fit("FAMILY", fam_w),
            _fit("RISK", risk_w),
        ]
        if count_w > 0:
            header_parts.append(_fit("PROC", count_w))
        if rss_w > 0:
            header_parts.append(_fit("RSS", rss_w))
        if show_cpu:
            header_parts.append(_fit("CPU", cpu_w))
        if z_w > 0:
            header_parts.append(_fit("Z", z_w))
        if show_orph:
            header_parts.append(_fit("ORPH", orph_w))
        if recl_w > 0:
            header_parts.append(_fit("RECL", recl_w))
        self._safe_addnstr(stdscr, y, 0, "  ".join(header_parts).ljust(w), w, curses.A_BOLD)
        visible = max(0, h - 1)
        keys = self._system_row_keys(snapshot)
        if self.system_selected < self.system_scroll:
            self.system_scroll = self.system_selected
        if self.system_selected >= self.system_scroll + visible:
            self.system_scroll = self.system_selected - visible + 1
        for i in range(visible):
            row_y = y + 1 + i
            idx = self.system_scroll + i
            if idx >= len(keys):
                self._safe_addnstr(stdscr, row_y, 0, " ".ljust(w), w)
                continue
            if snapshot is None:
                text = "SERVICE" if idx == 0 else "-"
                attr = self._risk_attr("warn", selected=(idx == self.system_selected))
                self._safe_addnstr(stdscr, row_y, 0, _fit(text, w).ljust(w), w, attr)
                continue
            if idx == 0:
                row_risk = snapshot.service_risk
                parts = [
                    _fit("SERVICE", fam_w),
                    _fit(row_risk.upper(), risk_w),
                ]
                if count_w > 0:
                    parts.append(_fit(str(snapshot.cgroup_process_count), count_w))
                if rss_w > 0:
                    parts.append(_fit(_fmt_bytes_short(snapshot.service.memory_current_bytes), rss_w))
                if show_cpu:
                    parts.append(_fit("-", cpu_w))
                if z_w > 0:
                    parts.append(_fit(str(snapshot.zombie_count), z_w))
                if show_orph:
                    parts.append(_fit(str(snapshot.orphan_count), orph_w))
                if recl_w > 0:
                    parts.append(_fit(_fmt_kib_short(snapshot.reclaimable_kib), recl_w))
                attr = self._risk_attr(row_risk, selected=(idx == self.system_selected))
                self._safe_addnstr(stdscr, row_y, 0, "  ".join(parts).ljust(w), w, attr)
                continue
            fam = snapshot.families[idx - 1]
            parts = [
                _fit(fam.family, fam_w),
                _fit(fam.risk.upper(), risk_w),
            ]
            if count_w > 0:
                parts.append(_fit(str(fam.count), count_w))
            if rss_w > 0:
                parts.append(_fit(_fmt_kib_short(fam.rss_kib), rss_w))
            if show_cpu:
                parts.append(_fit(_fmt_pct_short(fam.cpu_pct), cpu_w))
            if z_w > 0:
                parts.append(_fit(str(fam.zombie_count), z_w))
            if show_orph:
                parts.append(_fit(str(fam.orphan_count), orph_w))
            if recl_w > 0:
                parts.append(_fit(_fmt_kib_short(fam.reclaimable_kib), recl_w))
            attr = self._risk_attr(fam.risk, selected=(idx == self.system_selected))
            self._safe_addnstr(stdscr, row_y, 0, "  ".join(parts).ljust(w), w, attr)

    def _draw_system_details(
        self,
        stdscr: "curses._CursesWindow",
        x: int,
        y: int,
        h: int,
        w: int,
        snapshot: Optional[SystemSnapshot],
        family: Optional[SystemFamilySummary],
    ) -> None:
        for j in range(h):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)
        if h <= 0:
            return
        if snapshot is None:
            attr = curses.A_BOLD | self._risk_attr("warn")
            self._safe_addnstr(stdscr, y, x, _fit("SYSTEM DETAIL | No snapshot yet. Press [r] or wait for auto refresh.", w), w, attr)
            if h > 1:
                self._safe_addnstr(stdscr, y + 1, x, _fit("[v] next view  [r] refresh  [z] panes  [Esc] back to sessions", w), w)
            return

        sample_age = "-"
        with self._system_lock:
            state = self._system_state
            if state.last_loaded_at is not None:
                sample_age = _fmt_age(int(max(0.0, time.time() - state.last_loaded_at))).strip()
        title = f"SYSTEM DETAIL  [o] ops note  [r] refresh  [z] panes={self._system_pane_zoom_label()}  [v] next view  [Esc] reset"
        self._safe_addnstr(
            stdscr,
            y,
            x,
            _fit(title, w),
            w,
            self._section_attr("SERVICE", risk=snapshot.service_risk, active=(state.load_state == "loading")),
        )
        recent_events = self._recent_monitor_events(limit=4)
        section_lines: List[Tuple[str, int]] = [
            ("SERVICE", self._section_attr("SERVICE", risk=snapshot.service_risk)),
            ("", 0),
            ("RESOURCES", self._section_attr("RESOURCES")),
            (
                f"MainPID={snapshot.service.main_pid or '-'}  TasksCurrent={snapshot.service.tasks_current or '-'}  "
                f"MemoryCurrent={_fmt_bytes_short(snapshot.service.memory_current_bytes)}  CPU(ns)={snapshot.service.cpu_usage_nsec or '-'}",
                0,
            ),
            (f"CGroup={snapshot.service.control_group or '-'}", 0),
            ("COUNTS", self._section_attr("COUNTS")),
            ("", 0),
            ("ISSUES", self._section_attr("ISSUES", risk=("alert" if snapshot.issues else None))),
            (
                (" | ".join(snapshot.issues[:4]) if snapshot.issues else "none"),
                curses.A_BOLD | self._risk_attr(snapshot.service_risk if snapshot.issues else "ok"),
            ),
            ("OPERATOR NOTE", self._section_attr("OPERATOR NOTE")),
            (
                f"Press [o] for English runbook guidance. A later clean restart may reclaim about {_fmt_kib_short(snapshot.reclaimable_kib)} if residual helpers are the main source.",
                self._attention_attr(self._system_reclaim_level(snapshot.reclaimable_kib)),
            ),
        ]
        section_lines.append(("MONITOR EVENTS", self._section_attr("MONITOR EVENTS")))
        if recent_events:
            for ev in recent_events:
                section_lines.append((self._format_monitor_event(ev), self._monitor_event_attr(ev.event)))
        else:
            section_lines.append(("no recent monitor-origin actions", 0))

        body_y = y + len(section_lines) + 1
        special_lines = {
            1: self._system_service_line_segments(snapshot, sample_age=sample_age),
            6: self._system_counts_line_segments(snapshot),
        }
        for idx, (line, attr) in enumerate(section_lines):
            if y + 1 + idx >= y + h:
                return
            if idx in special_lines:
                self._safe_add_segments(stdscr, y + 1 + idx, x, special_lines[idx], w)
            else:
                self._safe_addnstr(stdscr, y + 1 + idx, x, _fit(line, w), w, attr)

        if body_y >= y + h:
            return

        if family is None:
            heading = "PROCESS DETAIL | SERVICE SUMMARY"
            target = [proc for proc in snapshot.processes if proc.potentially_problematic]
            if not target:
                target = list(snapshot.processes[: min(8, len(snapshot.processes))])
            summary_line = (
                f"selected=SERVICE  families={len(snapshot.families)}  "
                f"showing={min(len(target), max(0, h - (body_y - y) - 2))}  "
                f"use [↑/↓] to pick a family on the left"
            )
        else:
            heading = f"PROCESS DETAIL | {family.family}  risk={family.risk.upper()}  count={family.count}  live={family.live_count}  zombies={family.zombie_count}"
            target = [proc for proc in snapshot.processes if proc.family == family.family]
            note = " | ".join(family.notes) if family.notes else "no extra notes"
            summary_line = (
                f"rss={_fmt_kib_short(family.rss_kib)}  cpu={_fmt_pct_short(family.cpu_pct)}  "
                f"reclaim~{_fmt_kib_short(family.reclaimable_kib)}  notes={note}"
            )
        self._safe_addnstr(stdscr, body_y, x, _fit(heading, w), w, self._section_attr("PROCESS DETAIL", risk=(family.risk if family else snapshot.service_risk)))
        if body_y + 1 >= y + h:
            return
        self._safe_addnstr(stdscr, body_y + 1, x, _fit(summary_line, w), w)
        table_y = body_y + 2
        if table_y >= y + h:
            return
        proc_header = f"{_fit('PID', 6)}  {_fit('STAT', 4)}  {_fit('RSS', 7)}  {_fit('CPU', 6)}  {_fit('REL', 8)}  CMD"
        self._safe_addnstr(stdscr, table_y, x, _fit(proc_header, w), w, self._section_attr("PROCESS DETAIL"))
        max_rows = max(0, h - (table_y - y) - 1)
        for idx, proc in enumerate(target[:max_rows]):
            row_y = table_y + 1 + idx
            if row_y >= y + h:
                break
            cmd = proc.args or proc.comm or "-"
            line = (
                f"{_fit(str(proc.pid), 6)}  {_fit(proc.stat, 4)}  {_fit(_fmt_kib_short(proc.rss_kib), 7)}  "
                f"{_fit(_fmt_pct_short(proc.cpu_pct), 6)}  {_fit(proc.relation, 8)}  {cmd}"
            )
            self._safe_addnstr(stdscr, row_y, x, _fit(line, w), w, self._risk_attr(proc.risk))

    def _probe_lines(self, probe: Optional[object], *, width: int) -> List[str]:
        if probe is None:
            return ["  - disabled"]
        status = getattr(probe, "status", "-")
        checked_at = getattr(probe, "checked_at", None)
        latency = getattr(probe, "latency_ms", None)
        efficiency = getattr(probe, "efficiency", None)
        efficiency_unit = getattr(probe, "efficiency_unit", None)
        detail = getattr(probe, "detail", "")
        reply_preview = getattr(probe, "reply_preview", "")
        line1 = f"  status={status} checked={_fmt_dt(checked_at)} latency={latency if latency is not None else '-'}ms"
        if latency is None:
            line1 = f"  status={status} checked={_fmt_dt(checked_at)}"
        lines = [line1]
        if efficiency is not None and efficiency_unit:
            lines.append(f"  efficiency={efficiency:.3f} {efficiency_unit}")
        if detail:
            lines.extend([f"  detail: {part}" for part in _wrap_lines(detail, max(8, width - 10), max_lines=3)])
        if reply_preview:
            lines.extend([f"  reply: {part}" for part in _wrap_lines(redact_text(reply_preview), max(8, width - 10), max_lines=3)])
        return lines

    def _history_events_for_view(self, sv: SessionView) -> Tuple[_HistoryPaneState, List[TaskHistoryEvent], bool]:
        state = self._history_state_for(sv.meta.key)
        result = state.result
        stale = False
        if result is not None:
            stale = history_is_stale(result)
        events = filter_history_events(result.events, days=self.history_range_days) if result else []
        return state, events, stale

    def _history_kind_attr(self, kind: str, *, selected_live: bool = False) -> int:
        attr = 0
        if self._colors_enabled:
            if kind == "done":
                attr |= self._color_ok
            elif kind == "working":
                attr |= self._color_idle
            elif kind == "blocked":
                attr |= self._color_alert
            elif kind == "started":
                attr |= self._color_working
        if selected_live:
            attr |= curses.A_BOLD | curses.A_REVERSE
        return attr

    def _history_scroll_limit(self, total_events: int, visible_events: int) -> int:
        return max(0, total_events - max(1, visible_events))

    def _move_history_scroll(self, sv: Optional[SessionView], delta: int, *, visible_events: int, end: Optional[bool] = None) -> None:
        if not sv:
            return
        _, events, _ = self._history_events_for_view(sv)
        limit = self._history_scroll_limit(len(events), visible_events)
        if end is not None:
            self._set_history_scroll(sv.meta.key, limit if end else 0)
            return
        cur = self._history_scroll_for(sv.meta.key)
        self._set_history_scroll(sv.meta.key, max(0, min(limit, cur + delta)))

    def _move_history_selection(self, sv: Optional[SessionView], delta: int, *, visible_events: int, end: Optional[bool] = None) -> None:
        if not sv:
            return
        _, events, _ = self._history_events_for_view(sv)
        if not events:
            return
        max_idx = len(events) - 1
        if end is not None:
            idx = max_idx if end else 0
        else:
            idx = max(0, min(max_idx, self._history_selected_for(sv.meta.key) + delta))
        self._set_history_selected(sv.meta.key, idx)
        scroll = self._history_scroll_for(sv.meta.key)
        if idx < scroll:
            self._set_history_scroll(sv.meta.key, idx)
        elif idx >= scroll + visible_events:
            self._set_history_scroll(sv.meta.key, max(0, idx - visible_events + 1))

    def _draw_history_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: SessionView) -> None:
        for j in range(h):
            self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)
        state, events, stale = self._history_events_for_view(sv)
        result = state.result
        live_now = sv.computed.state == WorkState.WORKING
        blocked_now = live_now and any(ev.kind == "blocked" for ev in events[:2])

        status = state.load_state.upper().replace("_", " ")
        cache_mode = "-"
        if result is not None:
            cache_mode = result.mode.upper()
        if stale and state.load_state == "ready":
            status = "READY (STALE)"

        elapsed = ""
        if state.load_state == "loading" and state.started_at is not None:
            elapsed = f"  elapsed={int(max(0.0, time.time() - state.started_at))}s"

        header_attr = curses.A_BOLD
        if blocked_now:
            header_attr |= self._color_alert if self._colors_enabled else 0
            header_attr |= curses.A_REVERSE
        elif state.load_state == "loading":
            header_attr |= self._color_idle if self._colors_enabled else 0
        elif state.load_state == "error":
            header_attr |= self._color_alert if self._colors_enabled else 0
        elif live_now:
            header_attr |= self._color_working if self._colors_enabled else 0
            header_attr |= curses.A_REVERSE
        else:
            header_attr |= self._color_ok if self._colors_enabled else 0

        title = "HISTORY DETAIL  [r] load/reload  [1/7] range  [Enter] detail  [z] panes  [Z] fullscreen"
        self._safe_addnstr(stdscr, y, x, _fit(title, w), w, header_attr)
        status_line = f"State={status}  Range={self.history_range_days}d  Cache={cache_mode}{elapsed}"
        if self.detail_fullscreen:
            status_line += "  |  FULLSCREEN DETAIL ACTIVE"
        self._safe_addnstr(stdscr, y + 1, x, _fit(status_line, w), w, curses.A_BOLD)
        path_text = str(sv.meta.session_file) if sv.meta.session_file else "-"
        self._safe_addnstr(stdscr, y + 2, x, _fit(f"Session: {sv.meta.key}", w), w)
        self._safe_addnstr(stdscr, y + 3, x, _fit(f"Transcript: {path_text}", w), w)
        source_line = "Derived from transcript | best-effort"
        if live_now:
            source_line += " | LIVE TASK HISTORY"
        elif stale:
            source_line += " | Press [r] to refresh"
        self._safe_addnstr(stdscr, y + 4, x, _fit(source_line, w), w)

        body_y = y + 5
        body_h = max(0, h - (body_y - y) - 1)
        if body_h <= 0:
            return

        if state.load_state == "not_loaded" and result is None:
            self._safe_addnstr(
                stdscr,
                body_y,
                x,
                _fit("HISTORY LIST | No history loaded yet. Press [r] to read this session transcript.", w),
                w,
                curses.A_BOLD | (self._color_idle if self._colors_enabled else 0),
            )
            if body_y + 2 < y + h:
                self._safe_addnstr(
                    stdscr,
                    body_y + 2,
                    x,
                    _fit("SELECTED EVENT | After loading, use [j/k] to move between tasks and read details below.", w),
                    w,
                    curses.A_BOLD,
                )
            return
        if state.load_state == "loading" and result is None:
            msg = state.progress_msg or "Reading session history..."
            self._safe_addnstr(
                stdscr,
                body_y,
                x,
                _fit(f"HISTORY LIST | LOADING... {msg}", w),
                w,
                curses.A_BOLD | (self._color_idle if self._colors_enabled else 0),
            )
            if body_y + 1 < y + h:
                self._safe_addnstr(
                    stdscr,
                    body_y + 1,
                    x,
                    _fit("Please wait. Large transcripts may take a moment on first read; later loads use cache.", w),
                    w,
                )
            return
        if state.load_state == "error" and result is None:
            err = state.error or "history load failed"
            self._safe_addnstr(
                stdscr,
                body_y,
                x,
                _fit(f"HISTORY LIST | ERROR: {err}", w),
                w,
                curses.A_BOLD | (self._color_alert if self._colors_enabled else 0),
            )
            return
        if not events:
            if state.load_state == "loading":
                text = state.progress_msg or "Reading session history..."
            elif state.load_state == "error" and state.error:
                text = f"History error: {state.error}"
            elif result and result.events:
                total_events = len(result.events)
                if self.history_range_days < 7:
                    text = f"No events in the current {self.history_range_days}d window. Press [7] to show 7d. Cached total={total_events}."
                else:
                    text = f"No events remain after filtering. Cached total={total_events}."
            else:
                text = "No derived task history found in this transcript."
            self._safe_addnstr(stdscr, body_y, x, _fit(f"HISTORY LIST | {text}", w), w, curses.A_BOLD)
            if body_y + 2 < y + h:
                self._safe_addnstr(
                    stdscr,
                    body_y + 2,
                    x,
                    _fit("SELECTED EVENT | Nothing to display yet. Press [r] to reload after the session changes.", w),
                    w,
                    curses.A_BOLD,
                )
            return

        selected_idx = min(len(events) - 1, self._history_selected_for(sv.meta.key))
        self._set_history_selected(sv.meta.key, selected_idx)
        expanded = self._history_expanded_for(sv.meta.key)
        detail_h = 0
        if body_h >= 8:
            detail_h = max(4, body_h // 2 if expanded else max(5, body_h // 3))
            detail_h = min(detail_h, max(4, body_h - 3))
        list_h = max(2, body_h - detail_h - (1 if detail_h else 0))
        visible_events = max(1, list_h - 1)
        scroll = self._history_scroll_for(sv.meta.key)
        limit = self._history_scroll_limit(len(events), visible_events)
        if scroll > limit:
            scroll = limit
            self._set_history_scroll(sv.meta.key, scroll)

        shown = events[scroll : scroll + visible_events]
        list_title = (
            f"HISTORY LIST | items={len(events)}  selected={selected_idx + 1}/{len(events)}  "
            f"window={self.history_range_days}d  [j/k] move  [PgUp/PgDn] page  [g/G] edge"
        )
        self._safe_addnstr(stdscr, body_y, x, _fit(list_title, w), w, curses.A_BOLD)
        for idx, event in enumerate(shown):
            row_y = body_y + 1 + idx
            if row_y >= y + h:
                break
            ts_text = _fmt_dt(event.ts)
            label = event.kind.upper()[:7]
            absolute_idx = scroll + idx
            prefix = ">" if absolute_idx == selected_idx else " "
            line = f"{prefix} {ts_text} | {label:<7} | {event.title}"
            live_attr = (live_now and absolute_idx == 0 and event.kind in ("working", "blocked")) or absolute_idx == selected_idx
            attr = self._history_kind_attr(event.kind, selected_live=live_attr)
            self._safe_addnstr(stdscr, row_y, x, _fit(line, w), w, attr)

        detail_y = body_y + list_h
        if detail_h > 0 and 0 <= selected_idx < len(events) and detail_y < y + h - 1:
            try:
                stdscr.hline(detail_y, x, curses.ACS_HLINE, max(0, w))
            except curses.error:
                pass
            selected = events[selected_idx]
            section_title = (
                f"SELECTED EVENT | {selected.kind.upper()} | "
                f"{'expanded' if expanded else 'summary'} | [Enter] {'collapse' if expanded else 'expand'}"
            )
            self._safe_addnstr(
                stdscr,
                detail_y + 1,
                x,
                _fit(section_title, w),
                w,
                curses.A_BOLD | self._history_kind_attr(selected.kind),
            )
            meta_line = (
                f"Time={_fmt_dt(selected.ts)} | Source={selected.source} | Confidence={selected.confidence} | Title={selected.title}"
            )
            self._safe_addnstr(stdscr, detail_y + 2, x, _fit(meta_line, w), w)
            max_detail_lines = max(1, min(detail_h - 3, y + h - detail_y - 4))
            summary_lines = _wrap_lines(selected.summary, max(8, w - 2), max_lines=max_detail_lines)
            for i, part in enumerate(summary_lines):
                line_y = detail_y + 3 + i
                if line_y >= y + h - 1:
                    break
                self._safe_addnstr(stdscr, line_y, x, _fit(f"  {part}", w), w)

        footer = (
            f"events={len(events)} sel={selected_idx + 1}/{len(events)} "
            f"scroll={scroll + 1}-{min(len(events), scroll + len(shown))}  "
            f"j/k select  Enter detail  PgUp/PgDn page  g/G edge"
        )
        self._safe_addnstr(stdscr, y + h - 1, x, _fit(footer, w), w)

    def _draw_details(self, stdscr: "curses._CursesWindow", x: int, y: int, h: int, w: int, sv: Optional[SessionView]) -> None:
        if not sv:
            for j in range(h):
                self._safe_addnstr(stdscr, y + j, x, " ".ljust(w), w)
            title = "HISTORY DETAIL" if self.session_detail_mode == "history" else "STATUS DETAIL"
            attr = curses.A_BOLD | (self._color_idle if self._colors_enabled else 0)
            self._safe_addnstr(stdscr, y, x, _fit(f"{title} | Select a session on the left to populate this pane.", w), w, attr)
            if h > 1:
                hint = "[↑/↓] choose session  [h] toggle status/history  [z] show split panes  [Z] fullscreen detail"
                self._safe_addnstr(stdscr, y + 1, x, _fit(hint, w), w)
            return
        if self.session_detail_mode == "history":
            self._draw_history_details(stdscr, x=x, y=y, h=h, w=w, sv=sv)
            return
        banner = "STATUS DETAIL  [h] history  [z] panes  [Z] fullscreen  [b] bottom"
        if self.detail_fullscreen:
            banner += "  |  FULLSCREEN DETAIL ACTIVE"
        hint_attr = curses.A_BOLD | (self._color_working if self._colors_enabled else 0)
        self._safe_addnstr(stdscr, y, x, _fit(banner, w), w, hint_attr)
        y += 1
        h = max(0, h - 1)
        if h <= 0:
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
            f"Agent: {self._agent_label(sv.meta.agent_id)}{mark_str}  Channel: {self._channel_surface_label(sv.meta.channel)}  Account: {sv.meta.account_id or '-'}"
        )
        target_line = self._target_display_text(sv.meta)
        if target_line:
            lines.append(target_line)
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
        lines.extend(self._session_usage_lines(sv))
        lines.extend(self._session_range_usage_lines(sv))
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
        status_lines = self._build_session_status_lines(sv, last_activity=last_activity, width=w)

        max_status_lines = max(0, status_h - 1)
        visible_status_lines = self._visible_status_lines(status_lines, max_status_lines)

        for i in range(min(max_status_lines, len(visible_status_lines))):
            ln = visible_status_lines[i]
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(ln, w), w, self._session_status_line_attr(ln))

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
        status_lines = self._build_session_status_lines(sv, last_activity=last_activity, width=w)
        visible_status_lines = self._visible_status_lines(status_lines, max(0, status_h - 1))
        for i in range(min(status_h - 1, len(visible_status_lines))):
            ln = visible_status_lines[i]
            self._safe_addnstr(stdscr, y_status + 1 + i, x, _fit(ln, w), w, self._session_status_line_attr(ln))

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

    def _text_overlay(self, stdscr: "curses._CursesWindow", *, title: str, lines: List[str]) -> None:
        h, w = stdscr.getmaxyx()
        win_h = min(max(12, min(len(lines) + 2, h - 4)), max(6, h - 4))
        win_w = min(100, max(30, w - 4))
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        win.timeout(-1)
        scroll = 0
        while True:
            win.clear()
            win.border()
            body_h = max(1, win_h - 3)
            max_scroll = max(0, len(lines) - body_h)
            hdr = f" {title}  {scroll + 1}-{min(len(lines), scroll + body_h)}/{len(lines)} "
            self._safe_addnstr(win, 0, 2, hdr, win_w - 4, self._section_attr(title))
            view = lines[scroll : scroll + body_h]
            for i, ln in enumerate(view):
                attr = 0
                low = ln.lower()
                if ln.endswith(":") and len(ln) < 40:
                    attr = self._section_attr(ln.rstrip(":"))
                elif ln.startswith("Important") or low.startswith("risk warning"):
                    attr = self._attention_attr("alert")
                elif low.startswith("practical rule") or low.startswith("why cleanup may help"):
                    attr = self._attention_attr("warn")
                elif ln.startswith("systemctl ") or ln.startswith("openclaw ") or ln.startswith("[Service]") or ln.startswith("KillMode="):
                    attr = self._semantic_attr("action")
                self._safe_addnstr(win, 1 + i, 2, _pad_right_cells(ln, win_w - 4), win_w - 4, attr)
            footer = " [j/k][↑/↓] scroll  [PgUp/PgDn][Space] page  [g/G] top/bottom  [q][Esc][o] close "
            self._safe_addnstr(win, win_h - 1, 2, _pad_right_cells(footer, win_w - 4), win_w - 4, curses.A_REVERSE)
            win.refresh()
            ch = win.getch()
            if ch in (-1, 27, ord("q"), 10, 13, ord("o")):
                return
            if ch in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                scroll = min(max_scroll, scroll + 1)
            elif ch == curses.KEY_PPAGE:
                scroll = max(0, scroll - max(1, body_h - 1))
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                scroll = min(max_scroll, scroll + max(1, body_h - 1))
            elif ch in (curses.KEY_HOME, ord("g")):
                scroll = 0
            elif ch in (curses.KEY_END, ord("G")):
                scroll = max_scroll

    def _help_overlay(self, stdscr: "curses._CursesWindow") -> None:
        compact_lines = self._compact_help_lines()
        full_only_lines = [
            "",
            "Project:",
            "  https://github.com/openclawq/clawmonitor",
            "  Many terminals let you open the URL directly with the mouse or Ctrl+click.",
            "",
            "Model view:",
            "  - Manual refresh only. Press [r] to run model probes.",
            "  - Banner shows WAITING / RUNNING / DONE / ERROR for the current probe state.",
            "  - Each row combines direct provider probing and probing through OpenClaw.",
            "  - DIRECT / CLAW columns show the last probe latency when available.",
            "",
            "System view:",
            "  - Separate from Sessions and Models; use [s] or [v] to enter it.",
            "  - Banner shows WAITING / LOADING / READY / ERROR for local system inspection.",
            "  - Uses systemctl + ps + /proc/cgroup to summarize the gateway service cgroup.",
            "  - Left list shows SERVICE plus process families (chrome/playwright, ssh-agent, qmd, node, ...).",
            "  - Left columns: PROC=count, RSS=live memory, Z=zombies, ORPH=ppid=1, RECL=estimated reclaimable memory.",
            "  - Right side is split into SERVICE / RESOURCES / COUNTS / ISSUES / PROCESS DETAIL.",
            "  - Colors: green=healthy, cyan=active/loading, yellow=warn/waiting, red=alert/error, magenta=actions, blue=section labels.",
            "  - RECL is an estimate of potentially reclaimable RSS, not a guaranteed cleanup result.",
            "  - In System view, [z] cycles pane widths: 10/90, 50/50, left100, right100.",
            "  - Press [o] for an English operator note based on the current snapshot and the vpsclaw runbook.",
            "  - The operator note is advisory only; ClawMonitor still does not execute cleanup commands.",
            "",
            "States (STATE column):",
            "  WORKING        Task is running (lock present or ACPX indicates running).",
            "  FINISHED       No lock and assistant is not behind user.",
            "  INTERRUPTED    AbortedLastRun + pending reply (usually a crash/kill).",
            "  NO_MESSAGE     No real inbound user message detected for this session key.",
            "",
            "Performance:",
            "  - Refresh runs asynchronously; footer shows refresh progress/errors.",
            "  - Footer line 2 uses badges so RUNNING / ERROR / READY / STALE stand out immediately.",
            "  - Related Logs are cached per session to keep ↑/↓ selection responsive.",
            "  - Token NOW reads local sessions.json only and is cheap.",
            "  - Token 1d/7d/30d uses Gateway sessions.usage and is loaded on demand with [r].",
            "",
            "Left list columns:",
            "  Frozen columns: NODE, STATE",
            "  Metric pages: activity(USER/ASST/RUN/FLAGS) or tokens(IN/OUT/CACHE/CTX)",
            "  Use ← / → to switch column pages.",
            "  Token page: IN=input, OUT=output, CACHE=cache read+write, CTX=prompt/context percent.",
            "  Range token page swaps CTX for TOT and shows selected window totals.",
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
            "Help Navigation:",
            "  ↑/↓ or j/k     Scroll/select depending on view",
            "  PgUp/PgDn      Scroll page by page",
            "  g / G          Jump to start / end",
            "",
            "Tip:",
            "  Press [?] again to return to COMPACT help. Press q or Esc to close.",
        ]
        full_lines = compact_lines[:-3] + full_only_lines
        show_full = False

        h, w = stdscr.getmaxyx()
        win_h = min(max(12, max(len(compact_lines), len(full_lines)) + 2), max(6, h - 4))
        win_w = min(92, max(20, w - 4))
        win_y = (h - win_h) // 2
        win_x = (w - win_w) // 2
        win = curses.newwin(win_h, win_w, win_y, win_x)
        win.keypad(True)
        win.timeout(-1)
        scroll = 0
        while True:
            lines = full_lines if show_full else compact_lines
            win.clear()
            win.border()
            try:
                mode = "FULL" if show_full else "COMPACT"
                title = f" Help {mode}  {scroll + 1}-{min(len(lines), scroll + (win_h - 2))}/{len(lines)} "
                self._safe_addnstr(win, 0, 2, title, win_w - 4)
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
            if ch in (-1, 27, ord("q"), 10, 13):
                return
            if ch == ord("?"):
                prev_show_full = show_full
                show_full = not show_full
                if prev_show_full != show_full:
                    scroll = 0
                continue
            if ch in (curses.KEY_UP, ord("k")):
                scroll = max(0, scroll - 1)
            elif ch in (curses.KEY_DOWN, ord("j")):
                scroll = min(max(0, len(lines) - (win_h - 2)), scroll + 1)
            elif ch == curses.KEY_PPAGE:
                scroll = max(0, scroll - max(1, win_h - 3))
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                scroll = min(max(0, len(lines) - (win_h - 2)), scroll + max(1, win_h - 3))
            elif ch in (curses.KEY_HOME, ord("g")):
                scroll = 0
            elif ch in (curses.KEY_END, ord("G")):
                scroll = max(0, len(lines) - (win_h - 2))

    def _compact_help_lines(self) -> List[str]:
        lines = [
            f"ClawMonitor Help | Current View: {self._view_label()}",
            "",
            "Current View Shortcuts:",
        ]
        if self.view_mode == "system":
            lines.extend(
                [
                    "  r              Refresh system snapshot now",
                    f"  z              Cycle panes ({self._system_pane_zoom_label()}: 10/90 -> 50/50 -> left100 -> right100)",
                    "  ↑/↓ j/k        Select SERVICE or a process family",
                    "  PgUp/PgDn      Move by page in the left list",
                    "  g / G          Jump to top / bottom",
                    "  o              Open English operator note / restart guidance",
                    "  v              Next top-level view",
                    "  Esc            Reset to default surface",
                    "",
                    "System Abbreviations:",
                    "  Svc   service state (active/running, failed, ...)",
                    "  PROC  process count in this row",
                    "  RSS   live resident memory",
                    "  Z     zombie count",
                    "  ORPH  orphan count (usually PPID=1)",
                    "  RECL  estimated reclaimable memory, not an exact promise",
                    "  Prob  potentially problematic process count",
                    "  Large Prob / Reclaim / orphan counts are highlighted more aggressively.",
                    "",
                    "Color Semantics:",
                    "  green healthy/ready, cyan active/loading, yellow waiting/warn",
                    "  red alert/error, magenta actions/history, blue section labels",
                    "",
                    "Right Panel Sections:",
                    "  SERVICE / RESOURCES / COUNTS / ISSUES / OPERATOR NOTE / MONITOR EVENTS / PROCESS DETAIL",
                ]
            )
        elif self.view_mode == "models":
            lines.extend(
                [
                    "  r              Run model probes now",
                    "  ↑/↓ j/k        Select model row",
                    "  PgUp/PgDn      Move by page",
                    "  g / G          Jump to top / bottom",
                    "  v              Next top-level view",
                    "  s              Jump directly to System",
                    "  Esc            Reset to default surface",
                    "",
                    "Model Columns:",
                    "  DIRECT         direct provider probe latency",
                    "  CLAW           probe through OpenClaw latency",
                    "  overallStatus  OK / DEGRADED / TIMEOUT / ERROR / ...",
                ]
            )
        else:
            lines.extend(
                [
                    "  r              Refresh now, or load history/usage in the active subview",
                    "  h              Toggle Status / History on the right",
                    f"  z / Z          Panes={self._pane_zoom_label()} / fullscreen detail={'on' if self.detail_fullscreen else 'off'}",
                    f"  ← / →          Switch list columns ({self._session_metric_page_label()})",
                    f"  u              Token window ({self._token_window_label()})",
                    "  ↑/↓ j/k        Select session, or history item in History mode",
                    "  PgUp/PgDn      Page through list or history",
                    "  g / G          Jump to top / bottom",
                    "  jj / kk        Jump by agent",
                    "  b              Toggle bottom related logs",
                    "  v              Next top-level view",
                    "  s              Jump directly to System",
                    "  Esc            Reset to default surface",
                    "",
                    "Session Abbreviations:",
                    "  USER  idle since last user send",
                    "  ASST  idle since last assistant reply",
                    "  RUN   active run duration",
                    "  CTX   prompt/context percent used",
                    "  TOT   total tokens in selected usage window",
                    "",
                    "Color Semantics:",
                    "  green healthy/ready, cyan active/loading, yellow waiting/warn",
                    "  red alert/error, magenta actions/history, blue section labels",
                ]
            )
        lines.extend(
            [
                "",
                "Project:",
                "  https://github.com/openclawq/clawmonitor",
                "",
                "Global:",
                "  ?              Press again inside help for the FULL manual",
                "  q              Quit immediately",
            ]
        )
        return lines

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
                curses.init_pair(6, curses.COLOR_BLUE, -1)
                self._colors_enabled = True
                self._color_ok = curses.color_pair(1)
                self._color_working = curses.color_pair(3)
                self._color_idle = curses.color_pair(2)
                self._color_alert = curses.color_pair(4)
                self._color_magenta = curses.color_pair(5)
                self._color_section = curses.color_pair(6)
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
        last_refresh_sig: Optional[Tuple[object, ...]] = None
        dirty = True
        while True:
            now = time.time()
            # Async refresh: keep UI responsive even if Gateway calls are slow.
            if self.view_mode == "sessions" and (self._last_refresh_at is None or (now - self._last_refresh_at >= self.refresh_seconds)):
                with self._refresh_lock:
                    in_prog = self._refresh_in_progress
                if not in_prog:
                    self._request_refresh()
                    dirty = True
            if self.view_mode == "system":
                self._maybe_request_system_refresh()

            sessions_all = self.model.sessions
            sessions = self._apply_session_filter(sessions_all)
            items = self._build_list_items(sessions)
            self._reconcile_selection(items)
            sv_for_sig = self._selected_session(items)
            model_rows = self.model_monitor.rows
            self._reconcile_model_selection(model_rows)
            system_snapshot = self._system_snapshot()
            self._reconcile_system_selection(system_snapshot)
            with self._refresh_lock:
                session_sig = (
                    self._refresh_in_progress,
                    self._refresh_progress_msg,
                    self._refresh_progress_step,
                    self._refresh_progress_total,
                    self._refresh_error,
                    self._last_refresh_at,
                )
            with self._model_refresh_lock:
                model_sig = (
                    self._model_refresh_in_progress,
                    self._model_refresh_progress_msg,
                    self._model_refresh_progress_step,
                    self._model_refresh_progress_total,
                    self._model_refresh_error,
                    self._model_last_refresh_at,
                )
            history_sig = None
            token_sig = None
            system_sig = None
            if self.view_mode == "sessions" and sv_for_sig:
                hs = self._history_state_for(sv_for_sig.meta.key)
                history_result = hs.result
                history_sig = (
                    self.session_detail_mode,
                    self.history_range_days,
                    hs.load_state,
                    hs.progress_msg,
                    hs.error,
                    hs.last_loaded_at,
                    getattr(history_result, "mode", None),
                    getattr(history_result, "file_size", None),
                    getattr(history_result, "file_mtime", None),
                )
            if self.view_mode == "sessions" and self.session_token_window_days > 0:
                tus = self._token_usage_state_for(self.session_token_window_days)
                token_result = tus.result
                token_sig = (
                    self.session_token_window_days,
                    tus.load_state,
                    tus.progress_msg,
                    tus.error,
                    tus.last_loaded_at,
                    getattr(token_result, "updated_at_ms", None),
                    len(token_result.sessions_by_key) if token_result else None,
                )
            if self.view_mode == "system":
                with self._system_lock:
                    st = self._system_state
                    snap = st.snapshot
                    system_sig = (
                        st.load_state,
                        st.progress_msg,
                        st.error,
                        st.last_loaded_at,
                        getattr(snap, "service_risk", None),
                        getattr(snap, "problematic_count", None),
                        getattr(snap, "reclaimable_kib", None),
                    )
            live_tick: Optional[int] = None
            if self.view_mode == "models" and model_sig[0]:
                live_tick = int(now)
            elif self.view_mode == "sessions" and session_sig and session_sig[0]:
                live_tick = int(now)
            elif self.view_mode == "sessions" and history_sig and history_sig[2] == "loading":
                live_tick = int(now)
            elif self.view_mode == "sessions" and token_sig and token_sig[1] == "loading":
                live_tick = int(now)
            elif self.view_mode == "system" and system_sig and system_sig[0] == "loading":
                live_tick = int(now)
            refresh_sig = (self.view_mode, session_sig, model_sig, history_sig, token_sig, system_sig, self.show_logs, live_tick)
            if refresh_sig != last_refresh_sig:
                dirty = True
                last_refresh_sig = refresh_sig

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
            h, _ = stdscr.getmaxyx()
            page_step = max(1, h - 6)
            history_step = max(1, h - 10)
            if ch == ord("q"):
                return
            if ch == 27:
                if self._surface_is_default():
                    return
                self._reset_surface_state()
                dirty = True
                continue
            if ch == curses.KEY_UP:
                if self.view_mode == "models":
                    self._move_model_selection(model_rows, -1)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, -1)
                else:
                    self._move_selection(items, -1)
                dirty = True
            elif ch == curses.KEY_LEFT and self.view_mode == "sessions":
                self._cycle_session_metric_page(-1)
                if self.session_metric_page == "tokens" and self.session_token_window_days > 0:
                    self._maybe_request_token_usage_load(self.session_token_window_days)
                dirty = True
            elif ch == curses.KEY_RIGHT and self.view_mode == "sessions":
                self._cycle_session_metric_page(1)
                if self.session_metric_page == "tokens" and self.session_token_window_days > 0:
                    self._maybe_request_token_usage_load(self.session_token_window_days)
                dirty = True
            elif ch == curses.KEY_DOWN:
                if self.view_mode == "models":
                    self._move_model_selection(model_rows, 1)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, 1)
                else:
                    self._move_selection(items, 1)
                dirty = True
            elif ch == ord("k"):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), -1, visible_events=history_step)
                elif self.view_mode == "models":
                    self._move_model_selection(model_rows, -1)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, -1)
                else:
                    if self._should_jump_agent("k"):
                        self._move_selection_agent(items, -1)
                    else:
                        self._move_selection(items, -1)
                dirty = True
            elif ch == ord("j"):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), 1, visible_events=history_step)
                elif self.view_mode == "models":
                    self._move_model_selection(model_rows, 1)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, 1)
                else:
                    if self._should_jump_agent("j"):
                        self._move_selection_agent(items, 1)
                    else:
                        self._move_selection(items, 1)
                dirty = True
            elif ch in (curses.KEY_PPAGE,):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), -history_step, visible_events=history_step)
                elif self.view_mode == "models":
                    self._move_model_selection(model_rows, -page_step)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, -page_step)
                else:
                    self._move_selection(items, -page_step)
                dirty = True
            elif ch in (curses.KEY_NPAGE, ord(" ")):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), history_step, visible_events=history_step)
                elif self.view_mode == "models":
                    self._move_model_selection(model_rows, page_step)
                elif self.view_mode == "system":
                    self._move_system_selection(system_snapshot, page_step)
                else:
                    self._move_selection(items, page_step)
                dirty = True
            elif ch == curses.KEY_HOME or (ch == ord("g") and self.view_mode == "sessions" and self.session_detail_mode == "history"):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), 0, visible_events=history_step, end=False)
                elif self.view_mode == "models":
                    self._move_model_to_edge(model_rows, end=False)
                elif self.view_mode == "system":
                    self._move_system_to_edge(system_snapshot, end=False)
                else:
                    self._move_selection_to_edge(items, end=False)
                dirty = True
            elif ch == curses.KEY_END or (ch == ord("G") and self.view_mode == "sessions" and self.session_detail_mode == "history"):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    self._move_history_selection(self._selected_session(items), 0, visible_events=history_step, end=True)
                elif self.view_mode == "models":
                    self._move_model_to_edge(model_rows, end=True)
                elif self.view_mode == "system":
                    self._move_system_to_edge(system_snapshot, end=True)
                else:
                    self._move_selection_to_edge(items, end=True)
                dirty = True
            elif ch == ord("g"):
                if self.view_mode == "models":
                    self._move_model_to_edge(model_rows, end=False)
                elif self.view_mode == "system":
                    self._move_system_to_edge(system_snapshot, end=False)
                else:
                    self._move_selection_to_edge(items, end=False)
                dirty = True
            elif ch == ord("G"):
                if self.view_mode == "models":
                    self._move_model_to_edge(model_rows, end=True)
                elif self.view_mode == "system":
                    self._move_system_to_edge(system_snapshot, end=True)
                else:
                    self._move_selection_to_edge(items, end=True)
                dirty = True
            elif ch == ord("v"):
                self._cycle_view_mode()
                dirty = True
            elif ch == ord("s"):
                self._switch_view_mode("system")
                dirty = True
            elif ch == ord("o") and self.view_mode == "system":
                self._text_overlay(
                    stdscr,
                    title="Operator Note",
                    lines=self._system_operator_note_lines(system_snapshot),
                )
                dirty = True
            elif ch == ord("r"):
                if self.view_mode == "sessions" and self.session_detail_mode == "history":
                    sv = self._selected_session(items)
                    if sv:
                        self._request_history_load(sv)
                elif self.view_mode == "sessions" and self.session_metric_page == "tokens" and self.session_token_window_days > 0:
                    self._request_token_usage_load(self.session_token_window_days)
                elif self.view_mode == "models":
                    self._request_model_refresh()
                elif self.view_mode == "system":
                    self._request_system_refresh()
                else:
                    self._request_refresh()
                    last_refresh = time.time()
                dirty = True
            elif ch == ord("b") and self.view_mode == "sessions":
                self.show_logs = not self.show_logs
                dirty = True
            elif ch == ord("h") and self.view_mode == "sessions":
                self.session_detail_mode = "history" if self.session_detail_mode == "status" else "status"
                if self.session_detail_mode == "history" and not self.detail_fullscreen and self.pane_zoom_mode == "sessions":
                    self.pane_zoom_mode = "detail"
                dirty = True
            elif ch == ord("z"):
                if self.view_mode == "sessions":
                    self._cycle_pane_zoom_mode()
                    dirty = True
                elif self.view_mode == "system":
                    self._cycle_system_pane_zoom_mode()
                    dirty = True
            elif ch == ord("Z") and self.view_mode == "sessions":
                self.detail_fullscreen = not self.detail_fullscreen
                dirty = True
            elif ch == ord("1") and self.view_mode == "sessions":
                if self.session_detail_mode == "history":
                    self.history_range_days = 1
                elif self.session_metric_page == "tokens":
                    self.session_token_window_days = 1
                    self._maybe_request_token_usage_load(self.session_token_window_days)
                dirty = True
            elif ch == ord("3") and self.view_mode == "sessions":
                if self.session_metric_page == "tokens":
                    self.session_token_window_days = 30
                    self._maybe_request_token_usage_load(self.session_token_window_days)
                    dirty = True
            elif ch == ord("7") and self.view_mode == "sessions":
                if self.session_detail_mode == "history":
                    self.history_range_days = 7
                elif self.session_metric_page == "tokens":
                    self.session_token_window_days = 7
                    self._maybe_request_token_usage_load(self.session_token_window_days)
                dirty = True
            elif ch == ord("0") and self.view_mode == "sessions":
                if self.session_metric_page == "tokens":
                    self.session_token_window_days = 0
                    dirty = True
            elif ch == ord("u") and self.view_mode == "sessions":
                if self.session_metric_page == "tokens":
                    self._cycle_session_token_window()
                    if self.session_token_window_days > 0:
                        self._maybe_request_token_usage_load(self.session_token_window_days)
                    dirty = True
            elif ch == ord("d") and self.view_mode == "sessions":
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
            elif ch == ord("t") and self.view_mode == "sessions":
                self.tree_view = not self.tree_view
                self.scroll = 0
                dirty = True
            elif ch == ord("c") and self.view_mode == "sessions":
                self.show_cron = not self.show_cron
                self.scroll = 0
                dirty = True
            elif ch == ord("n") and self.view_mode == "sessions":
                self.node_show_session_label = not self.node_show_session_label
                dirty = True
            elif ch == ord("x") and self.view_mode == "sessions":
                self.focus_mode = not self.focus_mode
                self.scroll = 0
                dirty = True
            elif ch == ord("R") and self.view_mode == "sessions":
                sv = self._selected_session(items)
                if sv:
                    self._rename_selected(stdscr, sv)
                dirty = True
            elif ch == ord("?"):
                self._help_overlay(stdscr)
                dirty = True
            elif ch in (ord("e"), 10, 13):
                if self.view_mode == "sessions":
                    sv = self._selected_session(items)
                    if ch in (10, 13) and self.session_detail_mode == "history" and sv:
                        self._toggle_history_expanded(sv.meta.key)
                        dirty = True
                    elif ch == ord("e") and sv:
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
            model_rows = self.model_monitor.rows
            self._reconcile_model_selection(model_rows)
            system_snapshot = self._system_snapshot()
            self._reconcile_system_selection(system_snapshot)

            stdscr.erase()
            h, w = stdscr.getmaxyx()
            self._draw_header(stdscr, w)

            content_y = 2
            footer_h = self._footer_height(h)
            list_h = h - content_y - footer_h
            top_sep_y: Optional[int] = None
            if self.view_mode == "models":
                banner, banner_attr = self._model_banner()
                self._safe_addnstr(stdscr, 2, 0, banner.ljust(w), w, banner_attr)
                content_y = 3
                top_sep_y = 3
                list_h = h - content_y - footer_h
            elif self.view_mode == "system":
                banner, banner_attr = self._system_banner()
                self._safe_addnstr(stdscr, 2, 0, banner.ljust(w), w, banner_attr)
                self._safe_add_segments(stdscr, 3, 0, self._system_subbanner_segments(), w, pad_attr=curses.A_BOLD)
                content_y = 4
                top_sep_y = 4
                list_h = h - content_y - footer_h
            elif self.view_mode == "sessions":
                token_banner = self._token_banner()
                if token_banner is not None:
                    banner, banner_attr = token_banner
                    self._safe_addnstr(stdscr, 2, 0, banner.ljust(w), w, banner_attr)
                    content_y = 3
                    top_sep_y = 3
                    list_h = h - content_y - footer_h
            if top_sep_y is not None and top_sep_y < h - footer_h:
                try:
                    stdscr.hline(top_sep_y, 0, curses.ACS_HLINE, max(0, w))
                except curses.error:
                    pass
                content_y += 1
                list_h = h - content_y - footer_h
            if self.view_mode == "sessions":
                if self.detail_fullscreen:
                    list_w = 0
                elif self.pane_zoom_mode == "sessions":
                    list_w = w
                elif self.pane_zoom_mode == "even":
                    list_w = max(36, min(max(36, int(w * 0.50)), max(0, w - 26)))
                elif self.pane_zoom_mode == "detail":
                    if self.session_detail_mode == "history":
                        list_w = max(22, min(30, max(22, int(w * 0.24))))
                    else:
                        list_w = max(24, min(36, max(24, int(w * 0.28))))
                elif self.pane_zoom_mode == "list":
                    list_w = max(56, min(max(56, int(w * 0.64)), max(0, w - 24)))
                else:
                    list_w = max(40, min(max(40, int(w * 0.46)), max(0, w - 28)))
            elif self.view_mode == "models":
                list_w = max(52, min(max(52, int(w * 0.55)), max(0, w - 24)))
            else:
                if self.system_pane_zoom_mode == "detail90":
                    list_w = max(18, min(max(18, int(w * 0.10)), max(0, w - 36)))
                elif self.system_pane_zoom_mode == "even":
                    list_w = max(34, min(max(34, int(w * 0.50)), max(0, w - 28)))
                elif self.system_pane_zoom_mode == "left100":
                    list_w = w
                elif self.system_pane_zoom_mode == "detail100":
                    list_w = 0
                else:
                    list_w = max(20, min(max(20, int(w * 0.10)), max(0, w - 36)))
            detail_w = w if (self.view_mode == "sessions" and self.detail_fullscreen) else (w - list_w - 1)
            warning_y = max(content_y, h - footer_h - 1)
            if self.view_mode == "models":
                self._draw_model_list(stdscr, y=content_y, h=list_h, w=list_w, rows=model_rows)
                model_row = self._selected_model(model_rows)
                if detail_w >= 24 and list_w < w - 1:
                    try:
                        stdscr.vline(content_y, list_w, curses.ACS_VLINE, max(0, h - content_y - 1))
                    except curses.error:
                        pass
                    self._draw_model_details(stdscr, x=list_w + 1, y=content_y, h=h - content_y - 1, w=detail_w, row=model_row)
                else:
                    self._safe_addnstr(
                        stdscr,
                        warning_y,
                        0,
                        "Terminal too narrow for model details. Widen window or use `clawmonitor models`.".ljust(w),
                        w,
                        )
                sv = None
            elif self.view_mode == "system":
                family = self._selected_system_family(system_snapshot)
                if self.system_pane_zoom_mode == "detail100":
                    self._draw_system_details(stdscr, x=0, y=content_y, h=h - content_y - footer_h, w=w, snapshot=system_snapshot, family=family)
                    self._safe_addnstr(
                        stdscr,
                        warning_y,
                        0,
                        f"RIGHT DETAIL ACTIVE  [z]={self._system_pane_zoom_label()}  Use [↑/↓] to change left selection even when the list is hidden.".ljust(w),
                        w,
                        curses.A_BOLD | (self._color_working if self._colors_enabled else 0),
                    )
                elif self.system_pane_zoom_mode == "left100":
                    self._draw_system_list(stdscr, y=content_y, h=list_h, w=w, snapshot=system_snapshot)
                    self._safe_addnstr(
                        stdscr,
                        warning_y,
                        0,
                        f"LEFT LIST ACTIVE  [z]={self._system_pane_zoom_label()}  PROC=count RSS=live memory Z=zombies ORPH=ppid=1 RECL=estimated reclaimable.".ljust(w),
                        w,
                        curses.A_BOLD | (self._color_working if self._colors_enabled else 0),
                    )
                else:
                    self._draw_system_list(stdscr, y=content_y, h=list_h, w=list_w, snapshot=system_snapshot)
                    if detail_w >= 28 and list_w < w - 1:
                        try:
                            stdscr.vline(content_y, list_w, curses.ACS_VLINE, max(0, h - content_y - footer_h))
                        except curses.error:
                            pass
                        self._draw_system_details(stdscr, x=list_w + 1, y=content_y, h=h - content_y - footer_h, w=detail_w, snapshot=system_snapshot, family=family)
                    else:
                        self._safe_addnstr(
                            stdscr,
                            warning_y,
                            0,
                            "Terminal too narrow for system details. Press [z] for right100 or widen the terminal.".ljust(w),
                            w,
                        )
                sv = None
            else:
                sv = self._selected_session(items)
                if self.detail_fullscreen:
                    self._draw_details(stdscr, x=0, y=content_y, h=h - content_y - footer_h, w=w, sv=sv)
                elif self.pane_zoom_mode == "sessions":
                    self._draw_list(stdscr, y=content_y, h=list_h, w=w, items=items)
                    self._safe_addnstr(
                        stdscr,
                        warning_y,
                        0,
                        f"LEFT LIST FOCUS ACTIVE  Cols={self._session_metric_page_label()}  Press [←/→] to switch columns, [z] for 50/50/detail/left80, or [Z] for fullscreen detail.".ljust(w),
                        w,
                        curses.A_BOLD | (self._color_working if self._colors_enabled else 0),
                    )
                else:
                    self._draw_list(stdscr, y=content_y, h=list_h, w=list_w, items=items)
                    if detail_w >= 24 and list_w < w - 1:
                        try:
                            stdscr.vline(content_y, list_w, curses.ACS_VLINE, max(0, h - content_y - footer_h))
                        except curses.error:
                            pass
                        self._draw_details(stdscr, x=list_w + 1, y=content_y, h=h - content_y - footer_h, w=detail_w, sv=sv)
                    else:
                        self._safe_addnstr(
                            stdscr,
                            warning_y,
                            0,
                            "Terminal too narrow for details panel. Widen window or use `clawmonitor status`.".ljust(w),
                            w,
                        )

            refresh_age = "-"
            if self.view_mode == "models":
                if self._model_last_refresh_at is not None:
                    refresh_age = _fmt_age(int(time.time() - self._model_last_refresh_at))
                with self._model_refresh_lock:
                    in_prog = self._model_refresh_in_progress
                    prog_msg = self._model_refresh_progress_msg
                    prog_step = self._model_refresh_progress_step
                    prog_total = self._model_refresh_progress_total
                    err = self._model_refresh_error
                sel_total = len(model_rows)
                sel_pos = self.model_selected + 1 if model_rows else 0
            elif self.view_mode == "system":
                with self._system_lock:
                    sys_state = self._system_state
                if sys_state.last_loaded_at is not None:
                    refresh_age = _fmt_age(int(time.time() - sys_state.last_loaded_at))
                in_prog = sys_state.load_state == "loading"
                prog_msg = sys_state.progress_msg
                prog_step = 0
                prog_total = 0
                err = sys_state.error if sys_state.load_state == "error" else None
                sel_total = len(self._system_row_keys(system_snapshot))
                sel_pos = self.system_selected + 1 if sel_total else 0
            else:
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
            history_note = ""
            if self.view_mode == "sessions" and self.session_detail_mode == "history" and sv:
                hs, _, stale = self._history_events_for_view(sv)
                label = hs.load_state.upper().replace("_", " ")
                if stale and hs.load_state == "ready":
                    label = "READY/STALE"
                history_note = f" history={label} range={self.history_range_days}d"
                if hs.result is not None:
                    history_note += f" cache={hs.result.mode}"
                if hs.load_state == "loading" and hs.started_at is not None:
                    history_note += f" elapsed={int(max(0.0, time.time() - hs.started_at))}s"
            token_note = ""
            if self.view_mode == "sessions" and self.session_metric_page == "tokens":
                if self.session_token_window_days <= 0:
                    token_note = " usage=NOW(snapshot)"
                else:
                    tus = self._token_usage_state_for(self.session_token_window_days)
                    label = "WAITING" if tus.load_state == "not_loaded" else tus.load_state.upper().replace("_", " ")
                    if tus.load_state == "ready" and history_usage_is_stale(tus.last_loaded_at):
                        label = "READY/STALE"
                    token_note = f" usage={label} range={self.session_token_window_days}d"
                    if tus.load_state == "loading" and tus.started_at is not None:
                        token_note += f" elapsed={int(max(0.0, time.time() - tus.started_at))}s"
            refresh_label = "refresh"
            if self.view_mode == "sessions" and self.session_detail_mode == "history":
                refresh_label = "loadHist"
            elif self.view_mode == "sessions" and self.session_metric_page == "tokens" and self.session_token_window_days > 0:
                refresh_label = "loadUsage"
            elif self.view_mode == "models":
                refresh_label = "probe"
            elif self.view_mode == "system":
                refresh_label = "system"
            footer = (
                f"[q]quit [?]help [v]view={self.view_mode} [↑↓]select [PgUp/PgDn]page [g/G]edge [r]{refresh_label} "
                + (
                    (
                        (
                            f"[←/→]cols "
                            f"[u]tokenWindow={self._token_window_label()} "
                            f"[f]interval={int(self.refresh_seconds)}s "
                            f"[h]{self.session_detail_mode} "
                            f"[cols]{self._session_metric_page_label()} "
                            f"[z]{self._pane_zoom_label()} "
                            f"[Z]{'full' if self.detail_fullscreen else 'pane'} "
                            f"[t]{'tree' if self.tree_view else 'flat'} [c]{'cron' if self.show_cron else 'nocron'} "
                            f"[x]{'focus' if self.focus_mode else 'all'} "
                            f"[n]{'node:label' if self.node_show_session_label else 'node:plain'} "
                            f"[1/7]historyRange={self.history_range_days}d "
                            f"[R]rename [Enter]detail [e]export [b]bottom"
                        )
                        if self.session_detail_mode == "history"
                        else
                        f"[←/→]cols "
                        f"[u]tokenWindow={self._token_window_label()} "
                        f"[f]interval={int(self.refresh_seconds)}s "
                        f"[h]{self.session_detail_mode} "
                        f"[cols]{self._session_metric_page_label()} "
                        f"[z]{self._pane_zoom_label()} "
                        f"[Z]{'full' if self.detail_fullscreen else 'pane'} "
                        f"[t]{'tree' if self.tree_view else 'flat'} [c]{'cron' if self.show_cron else 'nocron'} "
                        f"[x]{'focus' if self.focus_mode else 'all'} "
                        f"[n]{'node:label' if self.node_show_session_label else 'node:plain'} "
                        f"[1/7]historyRange={self.history_range_days}d "
                        f"[R]rename [Enter]nudge [e]export [b]bottom"
                    )
                    if self.view_mode == "sessions"
                    else (
                        f"[z]{self._system_pane_zoom_label()} [o]ops-note manual/auto refresh "
                        f"sel={sel_pos}/{sel_total} rows={max(0, sel_total - 1)}"
                    )
                    if self.view_mode == "system"
                    else (
                        f"manual-model-probe [f]interval={int(self.refresh_seconds)} "
                        f"sel={sel_pos}/{sel_total} rows={len(model_rows)}"
                    )
                )
            )
            footer_lines: List[Union[str, List[Tuple[str, int]]]] = [footer]
            if self.view_mode == "sessions":
                segs = self._session_footer_status_segments(
                    refresh_age=refresh_age,
                    in_prog=in_prog,
                    err=err,
                    prog_step=prog_step,
                    prog_total=prog_total,
                    prog_msg=prog_msg,
                    sel_pos=sel_pos,
                    sel_total=sel_total,
                    sv=sv,
                )
                segs.append(("  ", 0))
                segs.append((f"sessions={self._last_shown_sessions}/{self._last_total_sessions}  ", curses.A_BOLD))
                if self.session_detail_mode == "history":
                    segs.append(("[j/k]history [Enter]detail [r]reload [Esc]reset [jj/kk]agent", self._semantic_attr("action")))
                else:
                    segs.append(("[u]window [r]load [h]history [Esc]reset [jj/kk]agent", self._semantic_attr("action")))
                footer_lines.append(segs)
            else:
                if self.view_mode == "system":
                    segs = self._system_footer_status_segments(
                        refresh_age=refresh_age,
                        in_prog=in_prog,
                        err=err,
                        prog_msg=prog_msg,
                        sel_pos=sel_pos,
                        sel_total=sel_total,
                        snapshot=system_snapshot,
                    )
                    if system_snapshot is not None:
                        segs.append(("  ", 0))
                        segs.append((f"families={len(system_snapshot.families)}  ", curses.A_BOLD))
                    segs.append(("[z]10/90-50/50-left100-right100 [o]ops [r]refresh [?]help [Esc]reset", self._semantic_attr("action")))
                    footer_lines.append(segs)
                else:
                    segs = self._model_footer_status_segments(
                        refresh_age=refresh_age,
                        in_prog=in_prog,
                        err=err,
                        prog_step=prog_step,
                        prog_total=prog_total,
                        prog_msg=prog_msg,
                        sel_pos=sel_pos,
                        sel_total=sel_total,
                        row_count=len(model_rows),
                    )
                    segs.append(("  ", 0))
                    segs.append(("[r]probe [v]next-view [s]system [Esc]reset", self._semantic_attr("action")))
                    footer_lines.append(segs)
            self._draw_footer(stdscr, w, footer_lines)

            stdscr.refresh()
            dirty = False
