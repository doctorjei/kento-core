# Releasing kento

kento ships as a single PyPI package (`kento`). It has **no container images**,
so unlike the sibling repos (gemet / kanibako / droste) there is no
skopeo/imagetools "promote" step — the release tag rebuilds the artifacts at the
final version and publishes those to PyPI.

Releases are driven entirely by **git tags** via
`.github/workflows/release.yml`, following the ecosystem's **rc-then-promote**
shape.

## Release flow

1. **Pre-flight (recommended).** On the commit you intend to tag, run the
   workflow manually via **`workflow_dispatch`** (Actions → release → Run
   workflow). This runs the full `build` job — tag-shape check is skipped (no
   tag), unit + integration tests, `python -m build`, and `twine check` — but
   creates **no** GitHub release and publishes **nothing**. Rule: *never tag
   without a green dispatch run on the same commit.*

2. **Cut a release candidate.** Tag `v<ver>-rcN` (e.g. `v1.3.0-rc1`) and push
   the tag. CI's `build` job:
   - validates the tag shape (`^v[0-9]+\.[0-9]+\.[0-9]+(-rcN)?$`),
   - runs `make test` (unit, tier 1) and `make test-integration` (tier 2;
     root-only steps self-skip on the runner),
   - builds the sdist + wheel and `twine check`s them,
   - uploads `dist/*` as a workflow artifact, and
   - creates a **draft GitHub prerelease** for the rc tag with the artifacts
     attached. Nothing is pushed to PyPI for an rc.

3. **Verify the rc run is green** (and inspect the draft prerelease / artifacts
   if desired). If anything is wrong, fix it and cut `v<ver>-rc2`, etc. rc tags
   stay mutable (see rulesets below).

4. **Cut the final release.** Tag `v<ver>` (e.g. `v1.3.0`) and push. CI runs
   `build` again as a gate, then the `publish` job:
   - **hard-fails** unless a matching `v<ver>-rc*` tag already exists (the
     rc-then-promote guarantee),
   - **rebuilds** the sdist + wheel at the final version (PyPI filenames embed
     the version, so we cannot byte-copy the rc artifact),
   - **publishes to PyPI** via OIDC trusted publishing
     (`pypa/gh-action-pypi-publish`) — no API token involved, and
   - publishes the **final GitHub release** for `v<ver>` with the artifacts
     attached, then best-effort deletes the rc draft prerelease(s).

### Tag-shape gate

Only tags matching `^v[0-9]+\.[0-9]+\.[0-9]+(-rc[0-9]+)?$` are valid. A
non-matching `v*.*.*` tag push fails fast in `build`.

## One-time setup (owner / human — NOT done by the agent)

These are outside the agent's GitHub access and must be done once by the repo
owner before the first publish:

1. **PyPI Trusted Publisher.** On <https://pypi.org> for the `kento` project
   (Manage → Publishing), add a **GitHub Actions** trusted publisher:
   - Owner: `doctorjei`
   - Repository: `kento`
   - Workflow filename: `release.yml`
   - Environment name: `pypi`

   (For the very first publish, configure it as a *pending* publisher if the
   project does not yet exist on PyPI.)

2. **GitHub `pypi` environment.** In the repo: Settings → Environments → create
   an environment named **`pypi`**. The `publish` job binds to it
   (`environment: pypi`) and the trusted-publisher config above matches on it.
   Optionally add required reviewers / a deployment branch-or-tag restriction.

3. **GitHub rulesets** (from the ecosystem broadcast), created via
   `gh api repos/doctorjei/kento/rulesets`:
   - **`protect-main`** — branch ruleset on `refs/heads/main`, rules:
     `deletion` + `non_fast_forward`, admin bypass allowed.
   - **`protect-release-tags`** — tag ruleset on `refs/tags/v*` but **excluding
     `refs/tags/v*-rc*`** (so rc tags stay mutable / re-cuttable), same rules
     (`deletion` + `non_fast_forward`) + admin bypass.

4. **Fine-grained PAT for the agent** (only if an agent drives tagging):
   Contents RW, Workflows RW, Actions RW, Metadata R, and **NO** Administration.

## Notes

- **No GHCR / container images.** kento is PyPI-only; there is no
  skopeo/imagetools promote step. The release tag rebuilds at the final version
  and OIDC-publishes those artifacts.
- **OIDC, not tokens.** Publishing uses GitHub OIDC + PyPI trusted publishing
  (`id-token: write` on the `publish` job). No PyPI API token is stored as a
  secret.
- **Top-level `permissions: {}`.** The workflow grants least privilege per job:
  `build` gets `contents: write` (rc draft), `publish` gets
  `id-token: write` + `contents: write`.
