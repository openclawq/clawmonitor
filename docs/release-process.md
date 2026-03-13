# Release Process (PyPI via GitHub Actions)

This repo is configured for **automatic PyPI publishing** using PyPI “Trusted Publishing” (OIDC) on **git tags**.

## One-time setup (already done)

- PyPI project: `clawmonitor`
- PyPI Pending/Trusted Publisher:
  - GitHub owner: `openclawq`
  - GitHub repo: `clawmonitor`
  - Workflow filename: `pypi-publish.yml`
  - Environment name: `pypi`
- GitHub Actions workflow: `.github/workflows/pypi-publish.yml`

## Standard release flow

Suggested workflow:

- Do day-to-day development on `dev`.
- Merge to `main` only when ready to release.
- Publish to PyPI by tagging on `main`.

1) Update version + changelog

- Bump `pyproject.toml` → `[project].version`
- Update `CHANGELOG.md`

2) Commit and push to `main`

```bash
git add pyproject.toml CHANGELOG.md
git commit -m "Release X.Y.Z"
git push
```

3) Tag and push the tag

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

This triggers GitHub Actions:

- builds `sdist` + `wheel`
- publishes to PyPI

4) Verify publish succeeded

- GitHub Actions run: should be `success` for workflow **Publish to PyPI**
- PyPI JSON endpoint:

```bash
python3 - <<'PY'
import json, urllib.request
pkg = "clawmonitor"
data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{pkg}/json", timeout=30))
print("name:", data["info"]["name"])
print("version:", data["info"]["version"])
PY
```

5) Create / update GitHub Release notes (optional but recommended)

```bash
gh release create vX.Y.Z -R openclawq/clawmonitor -t "ClawMonitor vX.Y.Z" -n "..."
```

## Manual publishing (avoid unless needed)

The recommended path is Trusted Publishing (no API tokens in the repo).
If GitHub Actions is down and you must ship urgently, you can publish manually with `twine` and a PyPI token,
but keep tokens out of the repository and out of logs.

## Troubleshooting: `invalid-publisher`

If the publish job fails with `invalid-publisher`, it means PyPI did not find a Trusted/Pending Publisher
matching the workflow’s OIDC claims.

Check PyPI publisher settings match exactly:

- Project name: `clawmonitor`
- Repository: `openclawq/clawmonitor`
- Workflow filename: `pypi-publish.yml`
- Environment name: `pypi`
