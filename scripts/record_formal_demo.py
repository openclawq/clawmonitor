#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import sys
import time

import pexpect


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CAST = ROOT / "docs" / "clawmonitor-formal-demo-20260321.cast"


def _sleep(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _send(child: pexpect.spawn, keys: str, *, pause: float = 0.8) -> None:
    child.send(keys)
    _sleep(pause)


def _wait(child: pexpect.spawn, needle: str, *, timeout: float = 45.0, optional: bool = False) -> bool:
    try:
        child.expect(needle, timeout=timeout)
        return True
    except pexpect.TIMEOUT:
        if optional:
            return False
        raise


def build_record_command(output: Path, *, cols: int, rows: int, title: str) -> str:
    intro = (
        "clear\n"
        "printf 'ClawMonitor formal TUI walkthrough\\r\\n'\n"
        "printf 'Sessions -> History -> Models -> System -> Operator Note -> Help\\r\\n'\n"
        "printf 'This recording uses fixed terminal dimensions for stable rendering.\\r\\n'\n"
        "sleep 1.2\n"
        "exec env TERM=xterm-256color clawmonitor tui\n"
    )
    wrapped = f"bash -lc {shlex.quote(intro)}"
    return (
        f"asciinema rec --overwrite --yes --idle-time-limit 1.0 "
        f"--cols {cols} --rows {rows} "
        f"-t {shlex.quote(title)} "
        f"{shlex.quote(str(output))} "
        f"-c {shlex.quote(wrapped)}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Record a formal ClawMonitor TUI asciinema walkthrough.")
    parser.add_argument("--output", default=str(DEFAULT_CAST), help="Output .cast path")
    parser.add_argument("--cols", type=int, default=140)
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--title", default="ClawMonitor Formal Demo 2026-03-21")
    args = parser.parse_args()

    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["TERM"] = "xterm-256color"

    cmd = build_record_command(output, cols=args.cols, rows=args.rows, title=args.title)
    print(f"[demo] recording to {output}")
    child = pexpect.spawn("/usr/bin/env", ["bash", "-lc", cmd], env=env, encoding="utf-8", timeout=120)
    child.logfile_read = sys.stdout
    child.setwinsize(args.rows, args.cols)

    try:
        _wait(child, "View: Sessions", timeout=45)
        _sleep(1.0)

        # Sessions view: show pane width changes, fullscreen detail, history and help.
        for _ in range(4):
            _send(child, "z", pause=0.9)
        _send(child, "Z", pause=1.0)
        _send(child, "Z", pause=0.8)
        _send(child, "h", pause=0.8)
        _send(child, "r", pause=0.8)
        _sleep(2.8)
        _send(child, "\x1b[6~", pause=1.0)  # PgDn
        _send(child, "?", pause=0.8)
        _send(child, "\x1b[6~", pause=0.8)
        _send(child, "q", pause=0.8)
        _send(child, "h", pause=0.8)

        # Models view: start a probe, let elapsed update, then jump to System while it runs.
        _send(child, "v", pause=0.8)
        _wait(child, "MODEL PROBE:", timeout=15)
        _sleep(0.8)
        _send(child, "r", pause=0.8)
        _wait(child, "MODEL PROBE: RUNNING", timeout=20, optional=True)
        _sleep(4.2)
        _send(child, "s", pause=0.8)

        # System view: wait for READY, cycle z layouts, pick a family, open operator note and help.
        _wait(child, "SYSTEM SNAPSHOT:", timeout=15)
        _wait(child, "SYSTEM SNAPSHOT: READY", timeout=25, optional=True)
        _sleep(1.0)
        for _ in range(4):
            _send(child, "z", pause=1.0)
        _send(child, "j", pause=1.0)
        _send(child, "o", pause=1.0)
        _wait(child, "Operator Note", timeout=10)
        _send(child, "\x1b[6~", pause=1.0)  # PgDn
        _send(child, "G", pause=1.0)
        _send(child, "g", pause=0.8)
        _send(child, "q", pause=0.8)
        _send(child, "?", pause=0.8)
        _send(child, "?", pause=0.8)
        _send(child, "\x1b[6~", pause=1.0)  # PgDn
        _send(child, "q", pause=0.8)

        # Return to default surface, then quit.
        _send(child, "\x1b", pause=0.8)  # Esc reset
        _wait(child, "View: Sessions", timeout=15, optional=True)
        _sleep(1.0)
        _send(child, "q", pause=0.4)
        child.expect(pexpect.EOF, timeout=20)
    finally:
        if child.isalive():
            try:
                child.send("q")
            except Exception:
                pass
            try:
                child.close(force=True)
            except Exception:
                pass

    print(f"[demo] finished: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
