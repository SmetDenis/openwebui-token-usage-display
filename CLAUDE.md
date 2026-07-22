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
  (`utils/plugin.py:extract_frontmatter`); wrapping a long line truncates the metadata. This is
  why `E501` is ignored for `usage_display.py`.
- **Verify every claim about OWUI behavior against real OWUI source, not memory or docs.** The
  data model shifts between versions. A read-only local clone path and freshness-check commands
  live in `CLAUDE.local.md`; the verified map is in the imported `docs/owui-map.md`. If the clone
  is stale relative to the latest OWUI release, say so — the user updates it himself.

## Deep-dive references (imported, always loaded)

- Plugin internals — pipeline, valves, resolution chains, token/cache semantics, debug payload,
  edge-case catalog with covering tests: @docs/plugin-internals.md
- Open WebUI source map — filter machinery, request lifecycle, usage normalization, persistence,
  events, frontend constraints (verified against v0.10.2): @docs/owui-map.md

## Commands

The dev environment lives in `.venv`; the `Makefile` drives everything through `.venv/bin/python`.

```bash
python -m venv .venv && make install-dev   # one-time setup
make pre-commit                            # the full gate: lint + typecheck + test-cov
make lint        # ruff check + ruff format --check
make format      # ruff format + ruff check --fix (auto-fix)
make typecheck   # mypy --strict
make test        # pytest
make test-cov    # pytest with coverage (term-missing + HTML in htmlcov/)
```

`make pre-commit` must be green before any change is done. It enforces **mypy strict** and a
**coverage floor of 95%** (`pyproject.toml` `fail_under = 95`). CI (`.github/workflows/ci.yml`,
single `gate` job) runs the same gate across Python 3.11–3.14 on pushes to `main` and all PRs.

Run a single test: `.venv/bin/python -m pytest tests/test_usage_display.py::test_render_input`
(or `-k <substring>`). The suite has no network and no Open WebUI runtime — it is fast.

## Releasing a change

- The version has a **single source of truth**: the `version:` field in the `usage_display.py`
  module docstring. When bumping it, mirror it in `pyproject.toml` `version` and add a matching
  top entry in `CHANGELOG.md`. (`required_open_webui_version: 0.9.0` in the same docstring is the
  compat floor; native provider cost needs 0.10.0+.)
- **Tag each release `vX.Y.Z`** matching the CHANGELOG entry (existing convention: `v2.0.0` …
  `v2.5.1`); there is no automated release workflow — tagging is manual.
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
- The stats line is a dispatch table (`_STATS_RENDERERS`, 13 metric keys, fixed 6-arg renderer
  signature). Key rule: **`show_*` valves gate visibility; `display_order` only sorts.**
- Context, price and model identity resolve through priority-ordered fallback chains
  (short-circuit on first hit); table lookups use longest-key case-insensitive substring match.
  Workspace/"agent" models resolve via **`base_model_id`**.
- The **NOTE block** at `usage_display.py:14-38` is the source-verified contract with OWUI's data
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
tests (172 collected from `tests/test_usage_display.py`) call the module's functions directly. There is **no
OWUI runtime and no network** — `tiktoken`/`aiohttp` and all provider payloads are faked (fakes
and `make_*` builders live at the top of the test file). `pydantic` is pinned in
`requirements-dev.txt` to the version OWUI ships (`2.13.4` for OWUI 0.10.2), so the plugin is
tested against the same pydantic it runs on in production — keep that pin tracking OWUI's, and
don't let a Dependabot bump drift it silently (Dependabot runs weekly for pip and GitHub Actions).

## Conventions

- Line length 120; ruff lint is broad (see `pyproject.toml` `[tool.ruff.lint]`). The per-file
  ignores on `usage_display.py` (`E501`, `PLR0913`, `PLR0912`, `PLR2004`, `SIM117`) are
  intentional and documented inline.
- Non-ASCII glyphs (`·`, emoji icons, `≈`, `Σ`) in strings are intentional (`RUF001/2/3` ignored).
- `_STATIC_CONTEXT_SIZES` and `_STATIC_PRICES` are hand-maintained offline seed tables (from
  models.dev, seeded 2026-07) — refresh them periodically; keep the `llama` family out of
  `_STATIC_PRICES` on purpose (free on Meta's API, paid on Groq/Together — a hardcoded $0 would
  lie).
- Issue templates ask reporters for the sanitized `debug_mode` payload; the PR template requires a
  version bump + CHANGELOG entry for user-facing changes and naming the OWUI version verified
  against.
