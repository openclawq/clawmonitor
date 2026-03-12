from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence
import re

from .gateway_logs import GatewayLogLine
from .redact import redact_text


@dataclass(frozen=True)
class Evidence:
    ts: Optional[str]
    text: str


@dataclass(frozen=True)
class Finding:
    id: str
    severity: str  # info|warn|critical
    summary: str
    evidence: List[Evidence]
    next_steps: List[str]


def _line_text(line: GatewayLogLine) -> str:
    return line.text or line.raw


def _match_latest(lines: Sequence[GatewayLogLine], pattern: re.Pattern[str]) -> Optional[GatewayLogLine]:
    for ln in reversed(lines):
        if pattern.search(_line_text(ln)):
            return ln
    return None


def related_logs(
    all_lines: Sequence[GatewayLogLine],
    session_key: Optional[str],
    channel: Optional[str],
    account_id: Optional[str],
    limit: int = 200,
) -> List[GatewayLogLine]:
    if not all_lines:
        return []
    hits: List[GatewayLogLine] = []
    session_key = session_key or ""
    chan = (channel or "").lower()
    acct = (account_id or "").lower()
    for ln in reversed(all_lines):
        txt = _line_text(ln)
        if session_key and session_key in txt:
            hits.append(ln)
        elif chan and ln.subsystem and chan in ln.subsystem.lower():
            if acct and f"[{acct}]" in txt.lower():
                hits.append(ln)
            elif acct and f"{chan}[{acct}]" in txt.lower():
                hits.append(ln)
            elif not acct:
                hits.append(ln)
        if len(hits) >= limit:
            break
    hits.reverse()
    return hits


def diagnose(
    session_key: str,
    channel: Optional[str],
    account_id: Optional[str],
    delivery_failed: bool,
    no_feedback: bool,
    is_working: bool,
    gateway_lines: Sequence[GatewayLogLine],
) -> List[Finding]:
    findings: List[Finding] = []
    rel = related_logs(gateway_lines, session_key=session_key, channel=channel, account_id=account_id, limit=400)

    if delivery_failed:
        findings.append(
            Finding(
                id="delivery_failed",
                severity="warn",
                summary="Delivery queue has failed sends for this session (result may exist but was not delivered).",
                evidence=[],
                next_steps=[
                    "Inspect ~/.openclaw/delivery-queue/failed/ entries for mirror.sessionKey matches.",
                    "Check channel credentials and outbound API errors in Gateway logs.",
                ],
            )
        )

    # Feishu: health-monitor restarting during long runs
    ln = _match_latest(rel, re.compile(r"health-monitor:\s+restarting.*stale-socket", re.IGNORECASE))
    if ln:
        findings.append(
            Finding(
                id="feishu_stale_socket_restart",
                severity="critical",
                summary="Feishu channel health monitor restarted (stale-socket) which can abort long runs and drop replies.",
                evidence=[Evidence(ts=str(ln.ts) if ln.ts else None, text=_line_text(ln)[:500])],
                next_steps=[
                    "Run: openclaw gateway call channels.status --json",
                    "Check feishu account fields: busy/activeRuns/lastRunActivityAt/connected/lastEventAt.",
                    "If this occurs during long tasks, review your health-check settings and Feishu run-state/restart-recovery patches.",
                ],
            )
        )

    # Feishu: queuedFinal=false fallback class
    ln = _match_latest(rel, re.compile(r"queuedFinal=false|replies=0", re.IGNORECASE))
    if ln:
        findings.append(
            Finding(
                id="no_queued_final_reply",
                severity="warn",
                summary="Run completed without queuing a final reply (queuedFinal=false / replies=0). Often paired with upstream errors/rate limits.",
                evidence=[Evidence(ts=str(ln.ts) if ln.ts else None, text=_line_text(ln)[:500])],
                next_steps=[
                    "Inspect the session transcript tail for toolResult isError / upstream error messages.",
                    "Check Gateway logs for 429/rate limit/billing/safety blocks around the same time.",
                    "Consider enabling/keeping a fallback reply policy if your build supports it.",
                ],
            )
        )

    # Telegram polling stall
    ln = _match_latest(rel, re.compile(r"Polling stall detected|no getUpdates", re.IGNORECASE))
    if ln:
        findings.append(
            Finding(
                id="telegram_polling_stall",
                severity="critical",
                summary="Telegram polling stall detected (gateway not receiving updates). Often proxy/NO_PROXY/egress or multi-instance issue.",
                evidence=[Evidence(ts=str(ln.ts) if ln.ts else None, text=_line_text(ln)[:500])],
                next_steps=[
                    "Check systemd env for gateway: HTTP_PROXY/HTTPS_PROXY/ALL_PROXY/NO_PROXY (do not bypass api.telegram.org in NO_PROXY).",
                    "Ensure only one instance is consuming getUpdates for this bot token.",
                    "Run: openclaw gateway call channels.status --json (check telegram lastInboundAt).",
                ],
            )
        )

    # Telegram commands limit
    ln = _match_latest(rel, re.compile(r"BOT_COMMANDS_TOO_MUCH", re.IGNORECASE))
    if ln:
        findings.append(
            Finding(
                id="telegram_bot_commands_limit",
                severity="info",
                summary="Telegram BOT_COMMANDS_TOO_MUCH seen (usually not the root cause of no replies).",
                evidence=[Evidence(ts=str(ln.ts) if ln.ts else None, text=_line_text(ln)[:300])],
                next_steps=["Treat as non-fatal unless accompanied by polling stall or send failures."],
            )
        )

    # Generic: SIGTERM / aborted
    ln = _match_latest(rel, re.compile(r"\bSIGTERM\b|shutdown|aborted", re.IGNORECASE))
    if ln:
        findings.append(
            Finding(
                id="possible_restart_interruption",
                severity="warn",
                summary="Logs suggest a restart/abort near this session; replies may be dropped if not replayed.",
                evidence=[Evidence(ts=str(ln.ts) if ln.ts else None, text=_line_text(ln)[:500])],
                next_steps=[
                    "Check whether your build includes restart recovery / pending-final replay for the affected channel.",
                    "Correlate lock createdAt and log timestamps to confirm mid-run interruption.",
                ],
            )
        )

    if no_feedback and not findings and not is_working:
        findings.append(
            Finding(
                id="no_feedback_unknown",
                severity="warn",
                summary="User message is newer than assistant message, but no specific log signature matched. Likely gate, upstream error, or delivery failure.",
                evidence=[],
                next_steps=[
                    "Open Related Logs panel and look for dispatch/send errors around the last user message time.",
                    "Check channels.status for policy gates (dmPolicy/groupPolicy/allowFrom) and lastInboundAt/lastOutboundAt.",
                ],
            )
        )

    # Ensure all summaries are redacted-safe.
    safe_findings: List[Finding] = []
    for f in findings:
        safe_findings.append(
            Finding(
                id=f.id,
                severity=f.severity,
                summary=redact_text(f.summary),
                evidence=[Evidence(ts=e.ts, text=redact_text(e.text)) for e in f.evidence],
                next_steps=[redact_text(s) for s in f.next_steps],
            )
        )
    return safe_findings

