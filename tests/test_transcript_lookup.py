from __future__ import annotations

from pathlib import Path

from clawmonitor.session_store import SessionMeta
from clawmonitor.transcript_lookup import find_related_transcript_candidates, resolve_transcript_candidate


def _meta(tmp_path: Path, *, session_id: str, session_file: Path | None = None) -> SessionMeta:
    return SessionMeta(
        agent_id="main",
        key="agent:main:feishu:default:direct:ou_testuser",
        session_id=session_id,
        updated_at_ms=None,
        session_file=session_file,
        aborted_last_run=False,
        system_sent=False,
        chat_type="direct",
        kind="direct",
        channel="feishu",
        account_id="default",
        to="user:ou_testuser",
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


def test_resolve_transcript_candidate_prefers_reset_archive(tmp_path: Path) -> None:
    root = tmp_path / "openclaw"
    sessions_dir = root / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    archive = sessions_dir / "sess-1.jsonl.reset.2026-03-27T00-00-00.000Z"
    archive.write_text('{"type":"session","id":"sess-1"}\n', encoding="utf-8")

    meta = _meta(tmp_path, session_id="sess-1", session_file=sessions_dir / "sess-1.jsonl")
    cand = resolve_transcript_candidate(root, meta)

    assert cand is not None
    assert cand.source == "reset"
    assert cand.path == archive
    assert cand.session_id_hint == "sess-1"


def test_find_related_transcript_candidates_matches_target_marker(tmp_path: Path) -> None:
    root = tmp_path / "openclaw"
    sessions_dir = root / "agents" / "main" / "sessions"
    sessions_dir.mkdir(parents=True)
    old = sessions_dir / "old-1.jsonl.reset.2026-03-27T00-00-00.000Z"
    old.write_text(
        '{"type":"session","id":"old-1"}\n'
        '{"type":"message","message":{"role":"user","content":[{"type":"text","text":"user:ou_testuser hello"}]}}\n',
        encoding="utf-8",
    )
    newer = sessions_dir / "newer-1.jsonl"
    newer.write_text('{"type":"session","id":"newer-1"}\n', encoding="utf-8")

    meta = _meta(tmp_path, session_id="sess-2", session_file=sessions_dir / "sess-2.jsonl")
    rows = find_related_transcript_candidates(root, meta, limit=3, search_limit=10)

    assert len(rows) == 1
    assert rows[0].source == "related"
    assert rows[0].path == old
    assert rows[0].session_id_hint == "old-1"
