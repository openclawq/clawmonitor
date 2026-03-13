from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CompactionConfig:
    mode: Optional[str]


@dataclass(frozen=True)
class OpenClawConfigSnapshot:
    compaction_by_agent: Dict[str, CompactionConfig]
    configured_agent_ids: Dict[str, bool]
    agent_names: Dict[str, str]
    agent_identity_names: Dict[str, str]

    def agent_label(self, agent_id: str) -> str:
        """
        User-facing label for an agent.

        Prefer identity name (from IDENTITY.md), otherwise configured agent name.
        If it differs from its id (e.g. identity "jack" vs id "agentd"), format
        as "jack(agentd)".
        """
        aid = (agent_id or "").strip() or "-"
        name = (self.agent_identity_names.get(aid) or self.agent_names.get(aid) or "").strip()
        if name and name != aid:
            return f"{name}({aid})"
        return aid


def _get(d: Any, *path: str) -> Any:
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(p)
    return cur


def _safe_load_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


_IDENTITY_NAME_RE = re.compile(r"(?i)\bname\b\s*:\s*(.+)$")


def _clean_identity_name(name: str) -> str:
    """
    Keep identity names user-friendly.

    Some users keep explanatory text in parentheses (e.g. "大虾（这是我的名字）").
    For display, drop common "this is my name" suffixes while keeping the core
    name.
    """
    s = (name or "").strip()
    if not s:
        return s
    s = re.sub(r"\s*[（(][^）)]*(这是我的名字|this is my name)[^）)]*[)）]\s*$", "", s, flags=re.IGNORECASE)
    return s.strip()


def _read_identity_name(workspace_dir: Path) -> Optional[str]:
    path = workspace_dir / "IDENTITY.md"
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # Typical: "- **Name:** TMNT"
        line_clean = line.replace("*", "")
        m = _IDENTITY_NAME_RE.search(line_clean)
        if not m:
            continue
        val = (m.group(1) or "").strip()
        # Strip trailing Markdown emphasis artifacts / punctuation.
        val = val.strip().strip("_").strip()
        val = _clean_identity_name(val)
        if val:
            return val
    return None


def read_openclaw_config_snapshot(openclaw_root: Path) -> OpenClawConfigSnapshot:
    cfg_path = openclaw_root / "openclaw.json"
    doc = _safe_load_json(cfg_path) or {}

    defaults_mode = _get(doc, "agents", "defaults", "compaction", "mode")
    defaults = CompactionConfig(mode=str(defaults_mode) if isinstance(defaults_mode, str) else None)

    compaction_by_agent: Dict[str, CompactionConfig] = {}
    configured_agent_ids: Dict[str, bool] = {}
    agent_names: Dict[str, str] = {}
    agent_identity_names: Dict[str, str] = {}

    defaults_ws = _get(doc, "agents", "defaults", "workspace")
    defaults_workspace = Path(str(defaults_ws)).expanduser() if isinstance(defaults_ws, str) and defaults_ws else (openclaw_root / "workspace")

    agents_list = _get(doc, "agents", "list")
    if isinstance(agents_list, list):
        for ent in agents_list:
            if not isinstance(ent, dict):
                continue
            agent_id = ent.get("id")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            configured_agent_ids[agent_id] = True
            nm = ent.get("name") or ent.get("displayName") or ent.get("title")
            if isinstance(nm, str) and nm.strip():
                agent_names[agent_id] = nm.strip()
            ws = ent.get("workspace")
            workspace = defaults_workspace
            if isinstance(ws, str) and ws.strip():
                try:
                    workspace = Path(ws).expanduser()
                except Exception:
                    workspace = defaults_workspace
            ident_name = _read_identity_name(workspace)
            if ident_name:
                agent_identity_names[agent_id] = ident_name
            mode = _get(ent, "compaction", "mode")
            if isinstance(mode, str):
                compaction_by_agent[agent_id] = CompactionConfig(mode=mode)
            else:
                compaction_by_agent[agent_id] = defaults
    else:
        compaction_by_agent["main"] = defaults
        configured_agent_ids["main"] = True
        ident_name = _read_identity_name(defaults_workspace)
        if ident_name:
            agent_identity_names["main"] = ident_name

    return OpenClawConfigSnapshot(
        compaction_by_agent=compaction_by_agent,
        configured_agent_ids=configured_agent_ids,
        agent_names=agent_names,
        agent_identity_names=agent_identity_names,
    )
