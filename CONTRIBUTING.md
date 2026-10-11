# Contributing

Thanks for your interest in improving this plugin.

## What this project is

A single-file [Open WebUI](https://openwebui.com) **filter** plugin. There is no build step and
no install: the product is `usage_display.py`, which users paste into **Admin → Functions** in
Open WebUI. Everything else in this repo is the test harness, docs, and CI around that one file.

## Requirements

- [uv](https://docs.astral.sh/uv/) 0.12.7+ (it provides Python 3.11+ itself if needed)
- `make`

## Setup

```bash
make install-dev   # uv sync --locked: creates .venv with the exact tool versions from uv.lock
```

Dev tools (ruff, mypy, pytest, pytest-cov, pydantic) are declared in the `dev` dependency group of
`pyproject.toml` and pinned exactly in the committed `uv.lock`. Every `make` target runs them through
`uv run --locked`, so an out-of-date lock fails loudly instead of silently using different versions.
After editing the dependency group run `make lock`; to take newer tool versions run `make upgrade`
and then `make pre-commit`.

## The quality gate

```bash
make pre-commit
```

runs the whole gate:

- `ruff check` + `ruff format --check` — lint and formatting
- `mypy` — strict type-checking
- `pytest --cov` — the test suite with a coverage floor of 95%

Individual targets: `make lint`, `make format`, `make typecheck`, `make test`, `make test-cov`.
CI (`.github/workflows/ci.yml`) runs the same gate across Python 3.11–3.14 on every push and PR.

ruff runs with every rule enabled (`select = ["ALL"]`). When it flags something, fix the cause
(split the function, name the constant, narrow the `except`). If a case is genuinely justified, add
a per-line `# noqa: RULE - reason`; widen the ignore lists in `pyproject.toml` only for a rule that
does not fit the project as a whole, with the reason next to it.

## Tooling decisions

Recorded so they are not re-litigated without new information (2026-09):

- **uv + `uv.lock` instead of `requirements-dev.txt` with `>=` bounds.** The `>=` bounds let CI
  install whatever was newest while contributors kept older versions; ruff 0.16 then stabilized
  `PLR0917` and every Dependabot PR failed CI at the lint step while local runs passed. A committed
  lock makes CI and local tooling identical; alternatives considered: `==` pins in the requirements
  file (no transitive pinning) and pip-tools (an extra tool with no advantage over uv here).
  Dependabot updates the lock through its `uv` ecosystem.
- **`select = ["ALL"]` with reasoned exclusions instead of a hand-picked rule list.** New rules show
  up only with a deliberate ruff bump in the lock, where CI reports them before merge.
- **Renderers take one `_Stats` object.** The former shared 6-positional-argument signature needed
  `PLR0913`/`PLR0917` suppressions and left each renderer with unused arguments (`ARG001`); a
  parameter object removes the cause instead of silencing it.
- **`except Exception` stays only where the plugin must not fail** (loading optional imports,
  resolver guards in `outlet`, optional network fetches, unguarded debug output), each marked with
  `# noqa: BLE001`; JSON parsing of valve maps catches `ValueError` only.
- **pydantic stays `==`-pinned to Open WebUI's own pin**, even though it is a dev dependency: the
  plugin runs on OWUI's pydantic in production. Do not merge a bump that drifts from it.

## How the tests load the plugin

Open WebUI plugins are not importable packages, so `tests/conftest.py` loads `usage_display.py`
as a module via `SourceFileLoader` and exposes it through the session-scoped `usage_display_module`
fixture. Tests request that fixture and exercise the module's functions directly — no Open WebUI
runtime and no network (provider payloads and the `tiktoken`/`aiohttp` probes are faked).

`tiktoken` and `aiohttp` are optional at runtime (soft-imported) and are **faked** in the tests, so
they are not dev dependencies. `pydantic` is pinned to the version Open WebUI ships, so the plugin is
tested against the same pydantic it runs on in production.

## Releasing a change

Every merge to `main` that changes `usage_display.py` is a release; CI tags and publishes it.

1. Make the change with a test.
2. If it touches `usage_display.py`, run `make bump PART=<part>`:
   - `major` — breaks an install: raises `required_open_webui_version`, removes a metric or valve, or
     changes a default incompatibly;
   - `minor` — adds a metric, a valve or a capability;
   - `patch` — a fix, or any other edit inside `usage_display.py` (comments included).

   It counts the next number from the latest release tag and writes it into the docstring `version:`
   (the single source of truth), `pyproject.toml`, the `uv.lock` project line and a new top
   `## [X.Y.Z]` section of `CHANGELOG.md`. Rerunning it is safe.
3. Describe the change for users under that `CHANGELOG.md` heading: it becomes the release text.
4. `make pre-commit` must be green — it includes the release check (`make release-check`).
5. Merge. The `release` job in `ci.yml` creates tag `vX.Y.Z` on the merge commit and a GitHub release
   (the CHANGELOG section, then GitHub's list of merged PRs). Do not create tags or releases by hand.

Changes that leave `usage_display.py` alone (docs, CI, tests, dependency bumps) keep the version and
release nothing. If another branch released your number first, the check fails after you rebase:
run `make bump` again — it renumbers your unreleased CHANGELOG section and keeps its text.

### Release flow decisions

Recorded 2026-10-11, after the version number had to be argued about on several releases in a row.

- **Root cause:** choosing the number, committing it and publishing it were three separate manual
  steps that drifted apart. Feature branches each "cut" a version, so numbers sat in `main` without a
  release (`2.7.0`, `2.8.0` on 2026-09-15); `v2.6.0` was re-cut over deleted builds, so two different
  files carried `2.6.0`; `2.7.0` was published from the GitHub UI with a bare tag, auto-generated notes
  and a target of `main` that put the tag on a later commit.
- **Merge = release** (owner's choice), because users install by pasting the file from `main`: any
  unreleased code there under an old number recreates the "two builds, one version" problem.
  Rejected: an `Unreleased` CHANGELOG section with a separate release command (main would carry new
  code under the last number between releases, and the release step could be forgotten), and
  release-please (it generates a commit-list changelog that clashes with the hand-written one, and the
  version sits in Open WebUI's strict one-line frontmatter).
- **The number is derived, not chosen:** semver part from the kind of change, counted from the latest
  tag by `make bump`; a number exists only once tagged. Owner's choice over "always minor", which would
  stop the number telling a fix from a feature.
- **The check needs no base branch:** the plugin either equals the file under its version's tag, or
  carries a version above every tag. That catches a forgotten bump and a number another branch released
  first, and lets Dependabot and docs changes through untouched. It compares file contents, not commits,
  so the `2.7.0` tag sitting on a later commit is harmless.
- **Published from `ci.yml` after the whole gate** (`needs: gate`), tagged on `$GITHUB_SHA` rather than
  `main`, idempotent (an existing tag means nothing to do), with notes = the CHANGELOG section plus
  GitHub's merged-PR list (owner's choice; Dependabot PRs show up in that list).
- **`scripts/release.py` is stdlib-only:** the release job runs it with the runner's Python, outside the
  project venv.
- **`uv.lock` gets only its version line edited:** `uv lock` from a newer uv (0.12.24) raised the lock's
  `revision` from 3 to 5, which Dependabot's bundled uv (0.12.7) may not read.

## Verifying against Open WebUI

Behavior claims about Open WebUI (the shape of `usage`, `output`, events, filter contract) should be
checked against the real Open WebUI source, which changes between versions — not assumed. Note the
Open WebUI version you tested against in your PR.
