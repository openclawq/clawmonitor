from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import state_dir
from .diagnostics import Finding
from .gateway_logs import GatewayLogLine
from .redact import redact_lines, redact_text


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _safe_slug(s: str, max_len: int = 32) -> str:
    out = []
    for ch in s:
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        else:
            out.append("_")
    slug = "".join(out).strip("_")
    return slug[:max_len] if slug else "session"


@dataclass(frozen=True)
class Report:
    session_key: str
    created_at: str
    summary: Dict[str, Any]
    findings: List[Dict[str, Any]]
    related_logs: List[str]

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def build_report(
    session_key: str,
    summary: Dict[str, Any],
    findings: List[Finding],
    related_logs: List[GatewayLogLine],
    max_log_lines: int,
) -> Report:
    safe_logs = [ln.text or ln.raw for ln in related_logs][-max_log_lines:]
    safe_logs = redact_lines(safe_logs)
    return Report(
        session_key=session_key,
        created_at=datetime.now(timezone.utc).isoformat(),
        summary={k: (redact_text(v) if isinstance(v, str) else v) for k, v in summary.items()},
        findings=[asdict(f) for f in findings],
        related_logs=safe_logs,
    )


def _render_md(rep: Report) -> str:
    lines: List[str] = []
    lines.append("# ClawMonitor Report")
    lines.append("")
    lines.append(f"- createdAt: `{rep.created_at}`")
    lines.append(f"- sessionKey: `{rep.session_key}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append("| key | value |")
    lines.append("| --- | --- |")
    for k, v in rep.summary.items():
        if isinstance(v, (dict, list)):
            vv = json.dumps(v, ensure_ascii=False)
        else:
            vv = str(v)
        vv = vv.replace("\n", " ").strip()
        lines.append(f"| `{k}` | {vv} |")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if rep.findings:
        for f in rep.findings:
            sev = f.get("severity") or "info"
            fid = f.get("id") or "unknown"
            summary = (f.get("summary") or "").strip()
            lines.append(f"- **{sev}** `{fid}`: {summary}")
            evidence = f.get("evidence") or []
            if isinstance(evidence, list) and evidence:
                for e in evidence[:3]:
                    if not isinstance(e, dict):
                        continue
                    ts = e.get("ts") or "-"
                    txt = (e.get("text") or "").strip()
                    if txt:
                        lines.append(f"  - evidence `{ts}`: {txt}")
            next_steps = f.get("next_steps") or []
            if isinstance(next_steps, list) and next_steps:
                lines.append("  - nextSteps:")
                for s in next_steps[:6]:
                    if isinstance(s, str) and s.strip():
                        lines.append(f"    - {s.strip()}")
    else:
        lines.append("- (none)")
    lines.append("")
    lines.append("## Related Logs (tail)")
    lines.append("")
    if rep.related_logs:
        lines.append("```")
        lines.extend(rep.related_logs)
        lines.append("```")
    else:
        lines.append("_none_")
    lines.append("")
    lines.append("> Note: ClawMonitor redacts token-like strings, but you should still review before sharing.")
    lines.append("")
    return "\n".join(lines)


def write_report(
    session_key: str,
    summary: Dict[str, Any],
    findings: List[Finding],
    related_logs: List[GatewayLogLine],
    max_log_lines: int,
) -> Path:
    # Back-compat: write JSON report only.
    paths = write_report_files(
        session_key=session_key,
        summary=summary,
        findings=findings,
        related_logs=related_logs,
        max_log_lines=max_log_lines,
        formats=["json"],
    )
    return paths["json"]


def write_report_files(
    session_key: str,
    summary: Dict[str, Any],
    findings: List[Finding],
    related_logs: List[GatewayLogLine],
    max_log_lines: int,
    *,
    formats: List[str],
    out_dir: Optional[Path] = None,
) -> Dict[str, Path]:
    base = out_dir or (state_dir() / "reports")
    base.mkdir(parents=True, exist_ok=True)
    stamp = _now_stamp()
    rep = build_report(
        session_key=session_key,
        summary=summary,
        findings=findings,
        related_logs=related_logs,
        max_log_lines=max_log_lines,
    )
    stem = f"clawmonitor_report_{stamp}_{_safe_slug(session_key)}"
    out: Dict[str, Path] = {}
    for fmt in formats:
        if fmt == "json":
            p = base / f"{stem}.json"
            p.write_text(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
            out["json"] = p
        elif fmt == "md":
            p = base / f"{stem}.md"
            p.write_text(_render_md(rep), encoding="utf-8")
            out["md"] = p
        else:
            raise ValueError(f"Unknown report format: {fmt}")
    return out
