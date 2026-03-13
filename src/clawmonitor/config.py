from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional
import os


def _expanduser(path_str: str) -> str:
    return os.path.expanduser(path_str)


def _xdg_dir(env_key: str, default_rel: str) -> Path:
    value = os.environ.get(env_key)
    if value:
        return Path(value)
    return Path.home() / default_rel


def default_config_path() -> Path:
    return _xdg_dir("XDG_CONFIG_HOME", ".config") / "clawmonitor" / "config.toml"


def state_dir() -> Path:
    return _xdg_dir("XDG_STATE_HOME", ".local/state") / "clawmonitor"


def cache_dir() -> Path:
    return _xdg_dir("XDG_CACHE_HOME", ".cache") / "clawmonitor"


@dataclass(frozen=True)
class Config:
    openclaw_root: Path
    openclaw_bin: str
    ui_seconds: float
    gateway_log_poll_seconds: float
    channels_status_poll_seconds: float
    delivery_queue_poll_seconds: float
    transcript_tail_bytes: int
    gateway_log_ring_lines: int
    report_max_log_lines: int
    hide_system_sessions: bool
    labels: Dict[str, str]


def _load_toml(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    data = path.read_bytes()
    try:
        import tomllib  # type: ignore

        return tomllib.loads(data.decode("utf-8"))
    except Exception:
        # Python 3.10 fallback
        import tomli  # type: ignore

        return tomli.loads(data.decode("utf-8"))


def load_config(path: Optional[Path] = None) -> Config:
    cfg_path = path or default_config_path()
    doc = _load_toml(cfg_path)

    openclaw = doc.get("openclaw", {}) if isinstance(doc, dict) else {}
    refresh = doc.get("refresh", {}) if isinstance(doc, dict) else {}
    limits = doc.get("limits", {}) if isinstance(doc, dict) else {}
    ui = doc.get("ui", {}) if isinstance(doc, dict) else {}
    labels = doc.get("labels", {}) if isinstance(doc, dict) else {}

    openclaw_root = Path(_expanduser(str(openclaw.get("root", "~/.openclaw"))))
    openclaw_bin = str(openclaw.get("openclaw_bin", "openclaw"))

    ui_seconds = float(refresh.get("ui_seconds", 5.0))
    gateway_log_poll_seconds = float(refresh.get("gateway_log_poll_seconds", 2.0))
    channels_status_poll_seconds = float(refresh.get("channels_status_poll_seconds", 5.0))
    delivery_queue_poll_seconds = float(refresh.get("delivery_queue_poll_seconds", 30.0))

    transcript_tail_bytes = int(limits.get("transcript_tail_bytes", 65536))
    gateway_log_ring_lines = int(limits.get("gateway_log_ring_lines", 5000))
    report_max_log_lines = int(limits.get("report_max_log_lines", 200))

    hide_system_sessions = bool(ui.get("hide_system_sessions", False))
    label_map: Dict[str, str] = {}
    if isinstance(labels, dict):
        for k, v in labels.items():
            if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                label_map[k.strip()] = v.strip()

    return Config(
        openclaw_root=openclaw_root,
        openclaw_bin=openclaw_bin,
        ui_seconds=ui_seconds,
        gateway_log_poll_seconds=gateway_log_poll_seconds,
        channels_status_poll_seconds=channels_status_poll_seconds,
        delivery_queue_poll_seconds=delivery_queue_poll_seconds,
        transcript_tail_bytes=transcript_tail_bytes,
        gateway_log_ring_lines=gateway_log_ring_lines,
        report_max_log_lines=report_max_log_lines,
        hide_system_sessions=hide_system_sessions,
        labels=label_map,
    )


def _toml_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace("\"", "\\\"")


def write_labels(config_path: Path, labels: Dict[str, str]) -> None:
    """
    Update (or append) a `[labels]` section in config.toml.

    This keeps edits localized so user config stays readable and shareable.
    """
    config_path = config_path.expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        text = config_path.read_text(encoding="utf-8")
    except Exception:
        text = ""
    lines = text.splitlines()

    section_lines = ["[labels]"]
    for k in sorted(labels.keys()):
        v = labels[k]
        if not k or not v:
            continue
        section_lines.append(f"\"{_toml_escape(k)}\" = \"{_toml_escape(v)}\"")

    def is_header(ln: str) -> bool:
        s = ln.strip()
        return s.startswith("[") and s.endswith("]") and len(s) >= 3

    start = None
    for i, ln in enumerate(lines):
        if ln.strip() == "[labels]":
            start = i
            break
    if start is None:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append("")
        lines.extend(section_lines)
    else:
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if is_header(lines[j]):
                end = j
                break
        # Replace the whole section block.
        # Preserve a blank line before the next section if present.
        suffix = lines[end:]
        if suffix and suffix[0].strip():
            section_lines = section_lines + [""]
        lines = lines[:start] + section_lines + suffix

    out = "\n".join(lines).rstrip() + "\n"
    config_path.write_text(out, encoding="utf-8")
