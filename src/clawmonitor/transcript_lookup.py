from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from .session_store import SessionMeta


@dataclass(frozen=True)
class TranscriptCandidate:
    path: Path
    source: str  # live | derived | reset | deleted | related
    session_id_hint: Optional[str]


def _sessions_dir(openclaw_root: Path, agent_id: str) -> Path:
    return openclaw_root / "agents" / agent_id / "sessions"


def _current_transcript_path(openclaw_root: Path, meta: SessionMeta) -> Optional[Path]:
    if meta.session_file:
        return meta.session_file
    if meta.session_id:
        return _sessions_dir(openclaw_root, meta.agent_id) / f"{meta.session_id}.jsonl"
    return None


def _derived_transcript_path(openclaw_root: Path, meta: SessionMeta) -> Optional[Path]:
    if not meta.session_id:
        return None
    return _sessions_dir(openclaw_root, meta.agent_id) / f"{meta.session_id}.jsonl"


def _archive_candidates(base_path: Path, reason: str) -> List[Path]:
    if not base_path.name:
        return []
    prefix = f"{base_path.name}.{reason}."
    try:
        matches = [p for p in base_path.parent.iterdir() if p.is_file() and p.name.startswith(prefix)]
    except Exception:
        return []
    matches.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)
    return matches


def _candidate_from_path(path: Path, source: str) -> TranscriptCandidate:
    name = path.name
    if name.endswith(".jsonl"):
        session_id_hint = name[:-6]
    else:
        session_id_hint = name.split(".jsonl", 1)[0] if ".jsonl" in name else None
    return TranscriptCandidate(path=path, source=source, session_id_hint=session_id_hint or None)


def resolve_transcript_candidate(openclaw_root: Path, meta: SessionMeta) -> Optional[TranscriptCandidate]:
    current = _current_transcript_path(openclaw_root, meta)
    derived = _derived_transcript_path(openclaw_root, meta)
    checked: set[Path] = set()

    for path, source in ((current, "live"), (derived, "derived")):
        if path is None or path in checked:
            continue
        checked.add(path)
        if path.exists():
            return _candidate_from_path(path, source)

    for path in list(checked):
        for archived in _archive_candidates(path, "reset"):
            return _candidate_from_path(archived, "reset")
        for archived in _archive_candidates(path, "deleted"):
            return _candidate_from_path(archived, "deleted")
    return None


def _scan_text_markers(path: Path, markers: Iterable[str], *, chunk_bytes: int = 131072) -> bool:
    needles = [m for m in markers if m]
    if not needles:
        return False
    try:
        size = path.stat().st_size
    except Exception:
        return False
    try:
        if size <= chunk_bytes * 2:
            text = path.read_text(encoding="utf-8", errors="ignore")
        else:
            with path.open("rb") as fh:
                head = fh.read(chunk_bytes)
                fh.seek(max(0, size - chunk_bytes))
                tail = fh.read(chunk_bytes)
            text = head.decode("utf-8", errors="ignore") + "\n" + tail.decode("utf-8", errors="ignore")
    except Exception:
        return False
    return any(needle in text for needle in needles)


def find_related_transcript_candidates(
    openclaw_root: Path,
    meta: SessionMeta,
    *,
    limit: int = 5,
    search_limit: int = 80,
) -> List[TranscriptCandidate]:
    sessions_dir = _sessions_dir(openclaw_root, meta.agent_id)
    if not sessions_dir.exists():
        return []

    current = _current_transcript_path(openclaw_root, meta)
    derived = _derived_transcript_path(openclaw_root, meta)
    exclude = {p for p in (current, derived) if p is not None}
    markers: List[str] = []
    if meta.to:
        markers.append(meta.to)
        tail = meta.to.split(":")[-1].strip()
        if len(tail) >= 8:
            markers.append(tail)

    files = [p for p in sessions_dir.glob("*.jsonl*") if p.is_file() and p not in exclude]
    files.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0.0, reverse=True)

    out: List[TranscriptCandidate] = []
    for path in files[: max(1, search_limit)]:
        if not _scan_text_markers(path, markers):
            continue
        out.append(_candidate_from_path(path, "related"))
        if len(out) >= limit:
            break
    return out
