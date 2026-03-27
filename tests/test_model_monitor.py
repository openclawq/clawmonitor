from __future__ import annotations

import json
from pathlib import Path

from clawmonitor.model_monitor import (
    _classify_error,
    _extract_model_chain,
    _extract_model_api_kind,
    _parse_sse_payload,
    _resolve_secret,
    discover_model_targets,
)


def test_extract_model_chain_merges_primary_and_fallbacks() -> None:
    chain = _extract_model_chain(
        {
            "primary": "tabcode/gpt-5.3-codex",
            "fallbacks": ["tabcode/gpt-5.2", "zai/glm-4.7"],
            "secondary": "wlai/gpt-5.2",
        }
    )
    assert chain == [
        ("primary", "tabcode/gpt-5.3-codex"),
        ("fallback1", "tabcode/gpt-5.2"),
        ("fallback2", "zai/glm-4.7"),
        ("secondary", "wlai/gpt-5.2"),
    ]


def test_resolve_secret_supports_env(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_MONITOR_TEST_KEY", "secret-value")
    assert _resolve_secret("MODEL_MONITOR_TEST_KEY") == ("secret-value", "env:MODEL_MONITOR_TEST_KEY")
    assert _resolve_secret("env:MODEL_MONITOR_TEST_KEY") == ("secret-value", "env:MODEL_MONITOR_TEST_KEY")
    assert _resolve_secret("secretref-env:MODEL_MONITOR_TEST_KEY") == ("secret-value", "env:MODEL_MONITOR_TEST_KEY")
    assert _resolve_secret("${MODEL_MONITOR_TEST_KEY}") == ("secret-value", "env:MODEL_MONITOR_TEST_KEY")


def test_resolve_secret_reads_openclaw_dotenv(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MODEL_MONITOR_FILE_KEY", raising=False)
    openclaw_root = tmp_path / ".openclaw"
    agent_dir = openclaw_root / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    (openclaw_root / "openclaw.json").write_text("{}", encoding="utf-8")
    (openclaw_root / ".env").write_text(
        'MODEL_MONITOR_FILE_KEY="secret-from-dotenv"\n',
        encoding="utf-8",
    )

    assert _resolve_secret("${MODEL_MONITOR_FILE_KEY}", agent_dir=agent_dir) == (
        "secret-from-dotenv",
        "env:MODEL_MONITOR_FILE_KEY",
    )


def test_classify_error_distinguishes_billing_rate_and_network() -> None:
    assert _classify_error("insufficient balance", http_status=429) == "billing"
    assert _classify_error("too many requests", http_status=429) == "rate_limit"
    assert _classify_error("temporary failure in name resolution", network_error=True) == "network"
    assert _classify_error("当前订阅套餐暂未开放GLM-5权限", http_status=429) == "billing"


def test_parse_sse_payload_collects_output_text() -> None:
    raw = "\n".join(
        [
            'data: {"type":"response.output_text.delta","delta":"O"}',
            'data: {"type":"response.output_text.delta","delta":"K"}',
            'data: {"type":"response.completed","response":{"id":"resp_1","usage":{"output_tokens":2}}}',
            "data: [DONE]",
        ]
    )
    doc = _parse_sse_payload(raw)
    assert doc["output_text"] == "OK"
    assert doc["usage"]["output_tokens"] == 2


def test_extract_model_api_kind_prefers_model_level_api() -> None:
    provider_conf = {
        "api": "openai-completions",
        "models": [
            {"id": "gpt-5.4", "api": "openai-responses"},
            {"id": "legacy", "api": "openai-completions"},
        ],
    }

    assert _extract_model_api_kind("codex/gpt-5.4", provider_conf) == "openai-responses"
    assert _extract_model_api_kind("codex/legacy", provider_conf) == "openai-completions"
    assert _extract_model_api_kind("codex/missing", provider_conf) is None


def test_discover_model_targets_reads_agents_and_auth_profiles(tmp_path: Path, monkeypatch) -> None:
    openclaw_root = tmp_path / ".openclaw"
    (openclaw_root / "agents" / "main" / "agent").mkdir(parents=True)
    (openclaw_root / "agents" / "worker" / "agent").mkdir(parents=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "IDENTITY.md").write_text("- **Name:** Main Agent\n", encoding="utf-8")

    config = {
        "models": {
            "providers": {
                "tabcode": {
                    "baseUrl": "https://example.test/openai",
                    "api": "openai-responses",
                    "models": [{"id": "gpt-5.3-codex", "name": "GPT 5.3 Codex"}],
                },
                "zai": {
                    "baseUrl": "https://example.test/v1",
                    "api": "openai-completions",
                    "apiKey": "env:ZAI_API_KEY",
                    "models": [{"id": "glm-4.7", "name": "GLM 4.7"}],
                },
            }
        },
        "agents": {
            "defaults": {
                "workspace": str(workspace),
                "model": {
                    "primary": "tabcode/gpt-5.3-codex",
                    "fallbacks": ["zai/glm-4.7"],
                },
                "models": {"zai/glm-4.7": {"alias": "GLM Alias"}},
            },
            "list": [
                {"id": "main"},
                {"id": "worker", "model": "zai/glm-4.7"},
            ],
        },
    }
    (openclaw_root / "openclaw.json").write_text(json.dumps(config), encoding="utf-8")
    auth_profiles = {
        "version": 1,
        "profiles": {
            "zai:default": {
                "provider": "zai",
                "key": "env:ZAI_API_KEY",
            }
        },
        "lastGood": {"zai": "zai:default"},
    }
    (openclaw_root / "agents" / "main" / "agent" / "auth-profiles.json").write_text(json.dumps(auth_profiles), encoding="utf-8")
    (openclaw_root / "agents" / "worker" / "agent" / "auth-profiles.json").write_text(json.dumps(auth_profiles), encoding="utf-8")

    monkeypatch.setenv("ZAI_API_KEY", "secret-zai")
    targets = discover_model_targets(openclaw_root)

    assert [target.model_ref for target in targets] == [
        "tabcode/gpt-5.3-codex",
        "zai/glm-4.7",
        "zai/glm-4.7",
    ]
    assert targets[0].agent_label == "Main Agent(main)"
    assert targets[1].model_label == "GLM Alias"
    assert targets[1].auth_source == "profile:zai:default/env:ZAI_API_KEY"


def test_discover_model_targets_uses_dotenv_and_model_level_api(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    openclaw_root = tmp_path / ".openclaw"
    agent_dir = openclaw_root / "agents" / "main" / "agent"
    agent_dir.mkdir(parents=True)
    (openclaw_root / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "codex": {
                            "baseUrl": "https://example.test/v1",
                            "api": "openai-completions",
                            "apiKey": "${CODEX_API_KEY}",
                            "models": [
                                {
                                    "id": "gpt-5.4",
                                    "api": "openai-responses",
                                    "name": "GPT 5.4",
                                }
                            ],
                        }
                    }
                },
                "agents": {
                    "defaults": {
                        "model": "codex/gpt-5.4",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (openclaw_root / ".env").write_text("CODEX_API_KEY=dotenv-secret\n", encoding="utf-8")

    targets = discover_model_targets(openclaw_root)

    assert len(targets) == 1
    assert targets[0].api_kind == "openai-responses"
    assert targets[0].auth_source == "provider:codex/env:CODEX_API_KEY"
    assert targets[0].headers.get("Authorization") == "Bearer dotenv-secret"
