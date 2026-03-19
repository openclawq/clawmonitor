from pathlib import Path
import json

from clawmonitor.session_store import list_sessions


def test_list_sessions_parses_usage_fields(tmp_path: Path) -> None:
    root = tmp_path / "openclaw"
    sessions_dir = root / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    session_file = sessions_dir / "abc.jsonl"
    session_file.write_text('{"type":"session"}\n', encoding="utf-8")
    doc = {
        "agent:main:test": {
            "sessionId": "abc",
            "updatedAt": 1234567890,
            "sessionFile": str(session_file),
            "abortedLastRun": False,
            "systemSent": True,
            "chatType": "direct",
            "lastTo": "ou_x",
            "inputTokens": 1200,
            "outputTokens": 345,
            "totalTokens": 1600,
            "contextTokens": 4000,
            "totalTokensFresh": True,
            "cacheRead": 50,
            "cacheWrite": 25,
            "modelProvider": "openai",
            "model": "gpt-test",
            "deliveryContext": {
                "channel": "feishu",
                "accountId": "acct-1",
            },
        }
    }
    (sessions_dir / "sessions.json").write_text(json.dumps(doc), encoding="utf-8")

    rows = list_sessions(root)

    assert len(rows) == 1
    row = rows[0]
    assert row.key == "agent:main:test"
    assert row.channel == "feishu"
    assert row.account_id == "acct-1"
    assert row.input_tokens == 1200
    assert row.output_tokens == 345
    assert row.total_tokens == 1600
    assert row.context_tokens == 4000
    assert row.total_tokens_fresh is True
    assert row.cache_read_tokens == 50
    assert row.cache_write_tokens == 25
    assert row.model_provider == "openai"
    assert row.model_name == "gpt-test"
