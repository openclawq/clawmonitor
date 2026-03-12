from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class CompactionConfig:
    mode: Optional[str]


@dataclass(frozen=True)
class OpenClawConfigSnapshot:
    compaction_by_agent: Dict[str, CompactionConfig]


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


def read_openclaw_config_snapshot(openclaw_root: Path) -> OpenClawConfigSnapshot:
    cfg_path = openclaw_root / "openclaw.json"
    doc = _safe_load_json(cfg_path) or {}

    defaults_mode = _get(doc, "agents", "defaults", "compaction", "mode")
    defaults = CompactionConfig(mode=str(defaults_mode) if isinstance(defaults_mode, str) else None)

    compaction_by_agent: Dict[str, CompactionConfig] = {}
    agents_list = _get(doc, "agents", "list")
    if isinstance(agents_list, list):
        for ent in agents_list:
            if not isinstance(ent, dict):
                continue
            agent_id = ent.get("id")
            if not isinstance(agent_id, str) or not agent_id:
                continue
            mode = _get(ent, "compaction", "mode")
            if isinstance(mode, str):
                compaction_by_agent[agent_id] = CompactionConfig(mode=mode)
            else:
                compaction_by_agent[agent_id] = defaults
    else:
        compaction_by_agent["main"] = defaults

    return OpenClawConfigSnapshot(compaction_by_agent=compaction_by_agent)

