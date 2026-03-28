from __future__ import annotations

import json
from pathlib import Path

from clawmonitor.transcript_tail import tail_transcript


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _msg(role: str, timestamp: str, text: str) -> dict:
    return {
        "type": "message",
        "timestamp": timestamp,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }


def test_tail_transcript_skips_system_timestamp_reminder_for_last_user_send(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    _write_jsonl(
        transcript,
        [
            {"type": "session", "version": 3, "id": "sess-1", "timestamp": "2026-03-28T00:59:00.000Z", "cwd": "/tmp"},
            _msg(
                "user",
                "2026-03-28T01:00:00.000Z",
                "System: [2026-03-28 09:00:00 GMT+8] Reminder: It's 09:00 (Beijing). Send the daily report now.",
            ),
            _msg("assistant", "2026-03-28T01:00:01.000Z", "NO_REPLY"),
            _msg("user", "2026-03-28T01:05:00.000Z", "Actual user message"),
        ],
    )

    tail = tail_transcript(transcript)

    assert tail.last_trigger is not None
    assert tail.last_trigger.preview.startswith("System: [2026-03-28 09:00:00 GMT+8]")
    assert tail.last_user_send is not None
    assert tail.last_user_send.preview == "Actual user message"
