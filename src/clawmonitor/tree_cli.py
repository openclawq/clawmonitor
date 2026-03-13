from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List, Tuple

from .status_cli import StatusRow


def _indent_for(row: StatusRow) -> int:
    if row.session_kind == "subagent":
        depth = max(1, (row.key or "").split(":").count("subagent"))
        return 1 + depth
    if row.session_kind == "acp":
        return 2
    return 1


def format_tree(rows: List[StatusRow], *, include_task: bool = True) -> str:
    """
    Group status rows by agent and show an indented view that highlights
    sub-agent / ACP sessions.
    """
    by_agent: Dict[Tuple[str, str], List[StatusRow]] = defaultdict(list)
    for r in rows:
        by_agent[(r.agent_id, r.agent_kind)].append(r)

    lines: List[str] = []
    for (agent_id, agent_kind) in sorted(by_agent.keys(), key=lambda x: (x[1] != "configured", x[0])):
        lines.append(f"{agent_id} ({agent_kind})")
        agent_rows = by_agent[(agent_id, agent_kind)]
        for r in agent_rows:
            indent = "  " * _indent_for(r)
            flags = ",".join(r.flags) if r.flags else "-"
            task = ""
            if include_task and r.task_preview and r.task_preview != "-":
                task = f"  task={r.task_preview}"
            lines.append(f"{indent}- {r.state:<11} [{flags}] {r.key}{task}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
