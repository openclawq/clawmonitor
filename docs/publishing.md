# Publishing / Sharing

ClawMonitor is designed to be shareable:

- The repository contains **code + docs only**.
- Runtime data is stored under XDG state/cache:
  - `~/.local/state/clawmonitor/` (events + reports)
  - `~/.cache/clawmonitor/`
- Configuration is read from `~/.config/clawmonitor/config.toml` by default.

## Versioning

Use SemVer-ish tags:

- `0.1.x`: alpha, breaking changes may still happen
- bump `version` in `pyproject.toml`
- update `CHANGELOG.md`

## Before publishing

1) Ensure you did not copy any of these into the repo:

- `~/.openclaw/openclaw.json`
- `~/.openclaw/agents/**/sessions/*.jsonl`
- `~/.openclaw/delivery-queue/**`

2) Keep diagnostics reports redacted (ClawMonitor does this by default).

3) If you paste logs into issues, still treat them as sensitive and remove:

- tokens / bot tokens
- phone numbers / user ids (when required)
- internal hostnames / paths (if relevant)

## Report sharing

- Prefer exporting a redacted report: `clawmonitor report --session-key ... --format md` (or `both`).
- Reports are written to `~/.local/state/clawmonitor/reports/` (XDG state dir), not into the repo.

## PyPI (pip)

If you publish to PyPI:

- `pip install clawmonitor`

Suggested release flow:

1) Bump `pyproject.toml` version + `CHANGELOG.md`
2) Create a git tag `vX.Y.Z`
3) Create a GitHub Release for that tag
4) Publish wheels/sdist to PyPI (manual `twine`, or GitHub Actions “trusted publishing”)

## GitHub Actions (recommended)

You can set up CI to:

- run `python -m compileall -q src`
- build `sdist` + `wheel`
- (optional) publish to PyPI on tags via “Trusted Publisher” (no secrets committed)

### PyPI “Trusted Publishing” authorization (OIDC)

This repo already includes a workflow: `.github/workflows/pypi-publish.yml` (runs on tags like `v0.1.1`).

To authorize it on PyPI (no secrets in the repo):

1) Create a PyPI account (if you don’t have one).
2) Create the project on PyPI (first publish), OR create a **Pending Publisher** for a new project.
   - Project name: `clawmonitor`
3) In the PyPI project settings (or “Publishing” → Pending Publishers), add a “Trusted Publisher”:
   - Provider: GitHub
   - Owner: `openclawq`
   - Repository: `clawmonitor`
   - Workflow filename: `pypi-publish.yml`
   - Environment: `pypi` (this repo’s workflow uses a GitHub Environment named `pypi`)
4) After that, pushing a tag `vX.Y.Z` will publish automatically.

If you prefer manual publishing instead, you can use a PyPI API token + `twine`, but trusted publishing is recommended.

## Packaging as an OpenClaw skill (wrapper)

ClawMonitor itself is a standalone CLI. If you want it as an OpenClaw “skill”, the simplest approach is a wrapper skill that:

- documents how to install `clawmonitor` (pip)
- provides small scripts like `clawmonitor_status.sh` / `clawmonitor_report.sh` that run:
  - `clawmonitor status --format md`
  - `clawmonitor report --session-key ... --format md`

This keeps the skill lightweight and avoids bundling your `~/.openclaw` state.

## Upstreaming to OpenClaw “official”

Pragmatic path (lowest friction):

1) Keep `clawmonitor` as its own public repo (this one).
2) Open an issue/PR to the OpenClaw official repo to add:
   - a link under a “Community tools” section in docs
   - a small `tools/` entry that documents installation and usage

If OpenClaw prefers vendoring tools, propose adding a `tools/clawmonitor/README.md` + install instructions,
but keep the Python package in this repo to avoid forcing OpenClaw’s release cadence onto ClawMonitor.
