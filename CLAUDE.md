# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

A **single-file Open WebUI filter plugin**. The deliverable is `usage_display.py` (~2000 lines),
which a user pastes into Open WebUI **Admin → Functions**. There is **no build step and no
install** — everything else in the repo (tests, docs, CI) is scaffolding around that one file. It
is published on the community store (<https://openwebui.com/posts/token_usage_display_a94ea72f>)
and listed in the official Open WebUI docs Community Plugins catalog
(<https://docs.openwebui.com/features/extensibility/community/>).

## Critical rules

- **Keep it one copy-pasteable file with zero runtime dependencies.** Never split the plugin into
  modules. Its only optional imports (`tiktoken`, `aiohttp`) are **soft-imported** so the plugin
  loads without them.
- **Never add a `requirements:` line to the docstring frontmatter.** It triggers a per-plugin
  `pip install` inside OWUI that fails in uvx/LXC deployments and blocks the plugin from loading
  (real user bug reports) — soft imports exist precisely to avoid it.
- **Do not reflow the docstring frontmatter.** OWUI parses strictly one `key: value` per line
  (`utils/plugin.py:extract_frontmatter`); wrapping a long line truncates the metadata, and the
  first line must stay bare `"""`. This is why the closing `"""` carries
  `# noqa: D205, D212, D415, E501` (ruff reads a multi-line string's noqa from its last line).
- **Verify every claim about OWUI behavior against real OWUI source, not memory or docs.** The
  data model shifts between versions. A read-only local clone path and freshness-check commands
  live in `CLAUDE.local.md`; the verified map is in the imported `docs/owui-map.md`. If the clone
  is stale relative to the latest OWUI release, say so — the user updates it himself.

## Deep-dive references (imported, always loaded)

- Plugin internals — pipeline, valves, resolution chains, token/cache semantics, debug payload,
  edge-case catalog with covering tests: @docs/plugin-internals.md
- Open WebUI source map — filter machinery, request lifecycle, usage normalization, persistence,
  events, frontend constraints (verified against v0.11.1, diff-checked through v0.12.0):
  @docs/owui-map.md

## Commands

Dev tooling is managed by **uv**: the `dev` dependency group in `pyproject.toml` plus the committed
`uv.lock` (exact versions). The `Makefile` runs every tool as `uv run --locked …`, which fails if the
lock is stale, so local runs and CI use identical ruff/mypy/pytest versions. There is no
`requirements-dev.txt` any more.

```bash
make install-dev                           # one-time setup: uv sync --locked (creates .venv)
make pre-commit                            # the full gate: lint + typecheck + test-cov
make bump PART=minor  # next version from the latest release tag, written everywhere (major|minor|patch)
make release-check    # the version rules CI enforces (also part of the test suite)
make lock        # re-lock after editing [dependency-groups] (never for a version bump)
make upgrade     # bump every locked tool to its latest allowed version (then run the gate)
make lint        # ruff check + ruff format --check
make format      # ruff format + ruff check --fix (auto-fix)
make typecheck   # mypy --strict
make test        # pytest
make test-cov    # pytest with coverage (term-missing + HTML in htmlcov/)
```

`make pre-commit` must be green before any change is done. It enforces **mypy strict** and a
**coverage floor of 95%** (`pyproject.toml` `fail_under = 95`). CI (`.github/workflows/ci.yml`:
a `gate` job, then a `release` job on `main` — see "Releasing a change") runs the same gate across
Python 3.11–3.14 on pushes to `main` and all PRs, installing tools with `astral-sh/setup-uv` (pinned
by commit SHA — it publishes no floating major tag) and `uv sync --locked`.

Run a single test: `uv run --locked pytest tests/test_usage_display.py::test_render_input`
(or `-k <substring>`). The suite has no network and no Open WebUI runtime — it is fast.

## Releasing a change

**Every merge to `main` that changes `usage_display.py` is a release** (users paste the file from
`main`, so `main` must always equal a published version). The number is never a judgment call:

- **Pick the part, not the number:** `make bump PART=major|minor|patch` counts from the latest release
  tag and writes the version into all four places — the docstring `version:` (the single source of
  truth), `pyproject.toml`, the `uv.lock` project line, and a new top `## [X.Y.Z]` in `CHANGELOG.md`
  (an unreleased top section is renumbered instead). Rerunning it is safe. Never bump with `make lock`:
  a newer uv also rewrites the lock's `revision`, which Dependabot's older uv may not read.
- **Semver part:** `major` breaks an install (raises `required_open_webui_version`, removes a metric or
  valve, changes a default incompatibly); `minor` adds a metric, valve or capability; `patch` is a fix or
  any other edit inside `usage_display.py`, comments included. A change that leaves the file untouched
  (docs, CI, tests, Dependabot) is not a release and keeps the version.
- **CI enforces it** (`scripts/release.py check`, run by `tests/test_release.py` in the gate): the four
  places agree, the CHANGELOG section is not empty, and a plugin that differs from its tagged release
  carries a version above every release tag. A forgotten bump, or a number another branch released
  first, fails before merge — fix it with `make bump`.
- **CI publishes it:** the `release` job in `ci.yml` (after the whole gate, on pushes to `main`) creates
  tag `vX.Y.Z` on that exact commit and a GitHub release whose text is the CHANGELOG section followed by
  GitHub's list of merged PRs. Never create tags or releases by hand. The bare `2.7.0` tag (made by hand
  in the GitHub UI) is tolerated; every later one is `vX.Y.Z`.
- **A number exists only once it is tagged**, and a tagged number is never reused. (History: `v2.6.0`
  was re-cut on 2026-09-15 over deleted `v2.6.0`/`v2.6.1` builds — the confusion this flow exists to
  prevent.) Rationale and rejected alternatives: `CONTRIBUTING.md` → "Release flow decisions".
- `required_open_webui_version: 0.9.0` in the docstring is the compat floor; native provider cost needs
  0.10.0+.
- **Never narrow a valve.** Since OWUI 0.12.0, saving new function code validates the admin's stored
  valves against the new `Valves` and refuses the save (HTTP 400) on any error but "missing" — so a
  narrowed `Literal`, a changed field type, a tighter constraint or `extra="forbid"` would stop every
  existing install from updating. Adding or removing a valve is safe. The same check guards a rollback
  (restoring or pasting older code), so a value a newer release newly allows blocks going back once an admin
  picks it. `tests/test_valves_compat.py` checks every release tag against the current `Valves`; rationale in
  `docs/plugin-internals.md` → Valves.
- The community-store post text is maintained in `docs/community-post.md`; publishing to the store
  post (URL above) is a manual copy-paste done by the user.

## Architecture (summary — details live in the imports above)

- `class Filter` implements the OWUI filter contract: async `inlet` (stashes wall-clock start and
  a `num_ctx` hint) and async `outlet` (the orchestrator: gates → extract tokens → timing →
  context → cost → build stats → emit a `status` event). Both resolver calls are individually
  exception-guarded — the plugin must never break the response pipeline.
- Config is two Pydantic models: `Valves` (admin — all toggles, ordering, cost/context maps and
  URLs) and `UserValves` (per-user `enabled` kill-switch only; per-user customization was
  deliberately rolled back).
- The stats line is a dispatch table (`_STATS_RENDERERS`, 15 metric keys, every renderer takes one
  frozen `_Stats` object). Key rule: **`show_*` valves gate visibility; `display_order` only sorts.**
- Context, price and model identity resolve through priority-ordered fallback chains
  (short-circuit on first hit); table lookups use longest-key case-insensitive substring match.
  Workspace/"agent" models resolve via **`base_model_id`**.
- The **NOTE block** at `usage_display.py:14-56` is the source-verified contract with OWUI's data
  model. Reconcile any token/cost/context change against it — and re-verify it against real OWUI
  source for the targeted version.
- Live network access (models.dev context/prices, llama.cpp probe) is opt-in via valves and
  TTL-cached in module-level dicts; all fetch failures degrade silently.
- `debug_mode` emits a sanitized diagnostic payload as a copyable `citation` event + stdout — the
  primary support tool. Sanitization is whitelist-based; never include raw `__model__`/
  `__metadata__` dicts (they carry the prompt and user/session ids).

## Testing approach

OWUI plugins are not importable packages, so `tests/conftest.py` loads `usage_display.py` via
`SourceFileLoader` and exposes it through the session-scoped **`usage_display_module`** fixture;
tests (231 collected from `tests/test_usage_display.py`) call the module's functions directly. There is **no
OWUI runtime and no network** — `tiktoken`/`aiohttp` and all provider payloads are faked (fakes
and `make_*` builders live at the top of the test file). The only outside state any test reads is git:
`tests/test_valves_compat.py` (4 tests) loads every release tag's `usage_display.py`, and
`tests/test_release.py` (8 tests) checks the release rules of `scripts/release.py` against the tags and
on a throwaway repository — which is why CI checks out with `fetch-depth: 0`. Both get the tags from the
`release_tags` fixture in `tests/conftest.py`: without tags it fails in CI and skips locally — run
`git fetch --tags` in a clone that lacks the newest release tag. `pydantic` is pinned (`==`) in the `dev`
dependency group to the version OWUI ships (`2.13.4` for OWUI 0.10.2 through 0.12.0), so the plugin
is tested against the same pydantic it runs on in production — keep that pin tracking OWUI's, and
don't merge a Dependabot bump that drifts it (Dependabot runs weekly for the `uv` and
`github-actions` ecosystems).

## Conventions

- Line length 120; ruff runs with **`select = ["ALL"]`** (Google docstring convention). Every
  exclusion in `pyproject.toml` carries its reason; `usage_display.py` excludes only `ANN401`
  (parsed provider JSON). Fix the cause first (split a complex function, name a magic number,
  narrow an `except`); a justified one-off gets a per-line `# noqa: RULE - reason`, not a wider
  ignore list. The intentional `except Exception` guards (the plugin must never break the
  response) are exactly the `# noqa: BLE001` lines.
- `_STATIC_CONTEXT_SIZES` and `_STATIC_PRICES` are hand-maintained offline seed tables (from
  models.dev, seeded 2026-07) — refresh them periodically; keep the `llama` family out of
  `_STATIC_PRICES` on purpose (free on Meta's API, paid on Groq/Together — a hardcoded $0 would
  lie).
- Issue templates ask reporters for the sanitized `debug_mode` payload; the PR template requires a
  version bump + CHANGELOG entry for user-facing changes and naming the OWUI version verified
  against.
