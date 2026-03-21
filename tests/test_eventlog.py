from __future__ import annotations

from pathlib import Path

from clawmonitor.eventlog import EventLog, read_recent_events


def test_read_recent_events_returns_latest_first(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    elog = EventLog(path)
    elog.write("alpha", value=1)
    elog.write("beta", value=2)
    elog.write("gamma", value=3)

    events = read_recent_events(path, limit=2)
    assert [event.event for event in events] == ["gamma", "beta"]
    assert events[0].data["value"] == 3
