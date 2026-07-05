# Releasing & distributing mongo-sync-agent

This document describes how `mongo-sync-agent` is versioned, branched, built,
and delivered to testers and clients. The process is fully tag-driven and
runs in GitHub Actions — see
[`.github/workflows/release.yml`](../.github/workflows/release.yml).

## 1. Distribution model

There are two audiences, served by the **same build artefact**:

| Audience | Environment | How they install |
|---|---|---|
| **Clients** | Locked-down Windows Alteryx Server hosts, **no Python, no `uv`** | Download the release zip, unzip, run the self-contained `msa.exe` |
| **Dev / testing** | Internal, `uv`/Python available | Either the same zip, or `uv tool install` / `pip install` from source |

The client-facing artefact is a **PyInstaller one-file `msa.exe`** — a single
executable with the Python runtime and all dependencies (`pyarrow`, `boto3`,
`pymongo`, `psutil`) baked in. Nothing needs to be installed on the host.

Everything ships through **GitHub Releases**. We do **not** publish to PyPI —
this is an internal/client tool tied to Alteryx infrastructure, so a private,
Release-based distribution is the right fit.

> **Note on code signing.** The one-file exe is currently **unsigned**.
> Enterprise antivirus / SmartScreen may quarantine unsigned one-file
> executables. If distributing to external clients, plan to sign `msa.exe`
> (Authenticode) before wide rollout — see [`CODE_SIGNING.md`](CODE_SIGNING.md)
> (§9) for the full process and options.

## 2. Branching strategy — one branch, two tag patterns

**Everything lives on `master`.** We deliberately do **not** keep a long-lived
`beta` branch. The beta and release channels are distinguished by the **tag
shape**, not by a branch:

| Tag example | Channel | Published as |
|---|---|---|
| `v1.2.0-beta.1`, `v1.2.0-beta.2` | Beta | GitHub **pre-release** (not marked "Latest") |
| `v1.2.0-rc.1` | Release candidate | GitHub **pre-release** |
| `v1.2.0` | Release | GitHub release (marked "Latest") |

Both tags point at commits on `master`. Promoting a good beta to a release
means **tagging again** (`v1.2.0` on the proven commit) — there is no branch to
merge.

**Why not a `beta` branch?** A dev → beta → master (GitFlow) layout exists to
*stabilise a release while new work continues*. It costs continuous
merge/cherry-pick drift and "which branch is this fix on?" confusion. At this
project's scale, where clients take whole versioned releases, tags off `master`
cover every case with zero merge overhead.

### The one exception: hotfix branches

If a client is running `v1.1.0` in production, hits a bug, but `master` has
already moved on to unfinished `v1.2` work, cut a **short-lived** branch on
demand:

```
git switch -c release/1.1 v1.1.0
# commit the fix
git tag v1.1.1 && git push origin v1.1.1
# delete the branch once released
```

This is an on-demand maintenance branch, not a permanent beta branch — don't
create it until you actually need it.

## 3. Versioning

- Versions follow [SemVer](https://semver.org/): `MAJOR.MINOR.PATCH`, with
  `-beta.N` / `-rc.N` pre-release suffixes.
- The version lives in a **single file**:
  [`src/mongo_sync_agent/_version.py`](src/mongo_sync_agent/_version.py).
  `pyproject.toml` declares `dynamic = ["version"]` and reads it from there, so
  packaging metadata (`pip show`, `importlib.metadata`) and `msa --version`
  always agree — there is no second place to keep in sync.
- The release pipeline **overwrites `_version.py` with the exact tag** at build
  time, so a packaged `msa.exe` (and its `pip`-built metadata) always report the
  tag they were built from.
- The value committed to `_version.py` is the current in-development version;
  bump it when you start work on a new version (see the checklist below).

## 4. How the pipeline works

Pushing a matching tag triggers `release.yml` on a `windows-latest` runner,
which:

1. Derives the version and channel from the tag (a `-beta`/`-rc`/`-alpha`
   suffix ⇒ pre-release).
2. Stamps the version into `_version.py`.
3. `pip install .` then builds the one-file exe with PyInstaller
   (`--collect-all` for the data-file-heavy native deps).
4. Smoke-tests the frozen binary (`msa.exe --version` must match the tag).
5. Assembles the zip: `msa.exe`, `config/`, `sql/`,
   `deploy/Register-MongoSyncTask.ps1`, `README.md`, `RELEASING.md`.
6. Publishes a GitHub Release with the zip attached, marked **pre-release**
   for beta/rc tags.

## 5. Cutting a beta (the first beta test)

From a clean, green `master`:

```bash
# 1. Make sure src/mongo_sync_agent/_version.py reflects the target, e.g. 0.1.0
#    (the pipeline stamps the exact tag at build time regardless, but keeping
#    the committed value current avoids a confusing dev-vs-release mismatch).
# 2. Tag the beta and push the tag:
git tag v0.1.0-beta.1
git push origin v0.1.0-beta.1
```

That's it — the workflow builds the exe bundle and publishes a **pre-release**
named `mongo-sync-agent v0.1.0-beta.1`. Download the zip from the Releases page
into the dev/testing area to validate.

Need another beta after fixes? Land them on `master` and bump the suffix:

```bash
git tag v0.1.0-beta.2 && git push origin v0.1.0-beta.2
```

## 6. Promoting to a release

Once a beta is validated, tag the proven commit with the final version:

```bash
git tag v0.1.0
git push origin v0.1.0
```

The same bundle is published as a normal (non-pre-release) GitHub Release that
clients can download.

## 7. Release checklist

- [ ] `master` is green (tests pass: `pytest`).
- [ ] `src/mongo_sync_agent/_version.py` matches the intended `MAJOR.MINOR.PATCH`.
- [ ] `README.md` reflects any behaviour/config changes.
- [ ] Tag pushed (`v…-beta.N` for beta, `v…` for release).
- [ ] Workflow succeeded and the Release/pre-release appears with the zip.
- [ ] (Beta) validated in the dev/testing area before promoting.

## 8. Scheduling the packaged exe on a client host

`deploy/Register-MongoSyncTask.ps1` supports both invocation modes. For the
exe-based client install, pass `-ExePath` (no Python required on the host):

```powershell
.\deploy\Register-MongoSyncTask.ps1 `
    -ExePath "C:\ProgramData\mongo-sync-agent\msa.exe" `
    -ConfigPath "C:\ProgramData\mongo-sync-agent\config\config.toml" `
    -WorkingDir "C:\ProgramData\mongo-sync-agent"
```

`-ExePath` takes precedence; omit it and pass `-PythonPath` to register a
source/venv install instead. Preview either with `-WhatIf`.

## 9. Signing the executable (recommended before external rollout)

The one-file `msa.exe` is currently **unsigned**. Authenticode signing is
strongly recommended before distributing to external clients so enterprise
AV/SmartScreen does not quarantine it. The full process, options, costs, and
the CI step to add are documented in **[`CODE_SIGNING.md`](CODE_SIGNING.md)** —
it is deferred until the first external client rollout and is not needed for
the internal beta.
