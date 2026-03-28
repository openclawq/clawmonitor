from __future__ import annotations

from datetime import datetime, timezone

from clawmonitor.session_store import SessionMeta
from clawmonitor.tui import _channel_session_display_label, _message_preview_lines, _missing_message_lines


def _session_meta(*, key: str, session_id: str, account_id: str, to: str) -> SessionMeta:
    return SessionMeta(
        agent_id="main",
        key=key,
        session_id=session_id,
        updated_at_ms=1774522275353,
        session_file=None,
        aborted_last_run=False,
        system_sent=False,
        chat_type="direct",
        kind="direct",
        channel="feishu",
        account_id=account_id,
        to=to,
        origin_label=None,
        parent_session_key=None,
        acp_state=None,
        acpx_session_id=None,
        acp_agent=None,
        acp_identity_state=None,
        input_tokens=None,
        output_tokens=None,
        total_tokens=None,
        context_tokens=None,
        total_tokens_fresh=None,
        cache_read_tokens=None,
        cache_write_tokens=None,
        model_provider=None,
        model_name=None,
    )


def test_channel_session_display_label_adds_account_hint_for_collisions() -> None:
    labels = {"target:feishu:user:ou_4a2c2c233c31cda49573d6848482920e": "ClawQ"}
    meta = _session_meta(
        key="agent:main:feishu:default:direct:ou_4a2c2c233c31cda49573d6848482920e",
        session_id="b3e991e9-8eb8-476f-a604-084c7680b751",
        account_id="default",
        to="user:ou_4a2c2c233c31cda49573d6848482920e",
    )

    text = _channel_session_display_label(
        labels,
        meta,
        label_counts={("feishu", "ClawQ"): 2},
        label_account_counts={
            ("feishu", "ClawQ", "default"): 1,
            ("feishu", "ClawQ", "main"): 1,
        },
    )

    assert text == "ClawQ@default"


def test_missing_message_lines_uses_proxy_timestamp_when_available() -> None:
    proxy_at = datetime(2026, 3, 27, 10, 20, 30, tzinfo=timezone.utc)

    lines = _missing_message_lines("user", transcript_missing=True, proxy_at=proxy_at, updated_at=None)

    assert lines == [
        "proxy @ 2026-03-27 18:20:30",
        "channel inbound only",
        "payload unavailable",
    ]


def test_missing_message_lines_uses_session_updated_when_no_proxy() -> None:
    updated_at = datetime(2026, 3, 27, 1, 2, 3, tzinfo=timezone.utc)

    lines = _missing_message_lines("claw", transcript_missing=True, proxy_at=None, updated_at=updated_at)

    assert lines == [
        "unavailable",
        "transcript missing",
        "session @ 2026-03-27 09:02:03",
    ]


def test_message_preview_lines_uses_second_line_for_preview_when_height_is_tight() -> None:
    ts = datetime(2026, 3, 16, 7, 24, 22, tzinfo=timezone.utc)

    lines = _message_preview_lines(
        ts=ts,
        preview="Actual user message",
        width=30,
        max_lines=2,
    )

    assert lines == [
        "@ 2026-03-16 15:24:22",
        "Actual user message",
    ]
