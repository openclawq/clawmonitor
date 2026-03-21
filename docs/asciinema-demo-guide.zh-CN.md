# ClawMonitor Asciinema Demo Guide

Updated: 2026-03-21

This note records the current formal demo pipeline for `clawmonitor`, including automated recording, rendered outputs, and replay commands.

## Final artifacts

- Formal cast:
  - `/home/qagent/program/clawmonitor/docs/clawmonitor-formal-demo-20260321.cast`
- Rendered SVG:
  - `/home/qagent/program/clawmonitor/docs/clawmonitor-formal-demo-20260321.svg`
- Rendered GIF:
  - `/home/qagent/program/clawmonitor/docs/clawmonitor-formal-demo-20260321.gif`
- Rendered MP4:
  - `/home/qagent/program/clawmonitor/docs/clawmonitor-formal-demo-20260321.mp4`
- Earlier short cast:
  - `/home/qagent/program/clawmonitor/docs/clawmonitor-demo-20260321.cast`

Current rendered output summary:

- MP4 duration:
  - about `25.25s`
- GIF duration:
  - about `27.39s`
- Terminal size:
  - `140 x 40`

## What the formal demo covers

The formal cast is a full TUI walkthrough with stable timing and fixed terminal geometry:

1. Enter `Sessions`
2. Cycle `z` layout modes
3. Toggle `Z` fullscreen detail on and off
4. Open `History`
5. Trigger history load with `r`
6. Show paging in the overlay
7. Open contextual help with `?`
8. Switch to `Models`
9. Trigger model probe with `r`
10. Keep the screen on `MODEL PROBE: RUNNING` long enough for viewers to see active state and elapsed time moving
11. Switch to `System`
12. Wait for `SYSTEM SNAPSHOT`
13. Cycle `z` layout modes again
14. Open English `Operator Note`
15. Show paging with `PgDn`, `G`, `g`
16. Open contextual help again
17. Reset with `Esc`
18. Quit cleanly

## Why long refreshes are handled this way

Some actions are intentionally slow or variable in real usage, especially model probing.

For the demo, the goal is not to wait for every remote check to finish. The script therefore:

- starts the model probe
- waits until `RUNNING` is visible when possible
- keeps the view open for a few seconds so viewers can see the live state
- moves on to the next feature without blocking the whole walkthrough

This keeps the recording readable while still showing that refresh is asynchronous and visibly in progress.

## Tools installed in this environment

- `asciinema 2.4.0`
- `svg-term`
- `agg`
- `ffmpeg`
- `pexpect`

## Recording scripts

- Automated recorder:
  - `/home/qagent/program/clawmonitor/scripts/record_formal_demo.py`
- Renderer:
  - `/home/qagent/program/clawmonitor/scripts/render_formal_demo.sh`

## How to replay the final demo

Replay the cast in terminal:

```bash
cd /home/qagent/program/clawmonitor
asciinema play docs/clawmonitor-formal-demo-20260321.cast
```

Open the rendered files directly if you want assets for sharing:

```bash
xdg-open docs/clawmonitor-formal-demo-20260321.mp4
xdg-open docs/clawmonitor-formal-demo-20260321.gif
xdg-open docs/clawmonitor-formal-demo-20260321.svg
```

## How to regenerate everything

Record a fresh formal cast:

```bash
cd /home/qagent/program/clawmonitor
python3 scripts/record_formal_demo.py
```

Render the cast to `svg`, `gif`, and `mp4`:

```bash
cd /home/qagent/program/clawmonitor
bash scripts/render_formal_demo.sh
```

If you want to target another cast path:

```bash
cd /home/qagent/program/clawmonitor
python3 scripts/record_formal_demo.py --output docs/my-demo.cast
bash scripts/render_formal_demo.sh docs/my-demo.cast
```

## Automated walkthrough details

The recorder uses `pexpect` to drive `clawmonitor tui` inside `asciinema rec`.

Current scripted path:

1. Print a short intro in shell before the TUI starts
2. Wait for `View: Sessions`
3. Cycle `z` four times
4. Toggle `Z` on and off
5. Open `History`
6. Trigger history read with `r`
7. Wait briefly for loading and content
8. Show overlay paging
9. Open and close help
10. Switch to `Models`
11. Start probe with `r`
12. Wait a few seconds for the running banner and elapsed movement
13. Switch to `System`
14. Wait for system snapshot banner
15. Cycle `z` four times
16. Select a family row
17. Open `Operator Note`
18. Scroll in the note
19. Open contextual help
20. Reset to the default surface with `Esc`
21. Quit

## Overlay paging keys shown in the demo

- `PgUp / PgDn`
  - page scroll
- `g / G`
  - top / bottom
- `q`
  - close current overlay
- `Esc`
  - return to the default surface

## Notes for future re-recording

- Keep the terminal fixed at `140 x 40` for consistent layout.
- Do not wait for every model probe to complete; showing `RUNNING` is enough for a product walkthrough.
- If a banner string changes in the TUI, update the `pexpect` waits in `scripts/record_formal_demo.py`.
- The short cast remains useful as a compact teaser; the formal cast is the full product demo.
