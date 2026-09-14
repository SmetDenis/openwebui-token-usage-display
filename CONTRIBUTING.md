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

1. Make the change with a test.
2. Bump the `version:` field in the `usage_display.py` docstring — it is the single source of truth
   for the version (also mirrored in `pyproject.toml`).
3. Add a matching entry at the top of `CHANGELOG.md`.
4. `make pre-commit` must be green.

## Verifying against Open WebUI

Behavior claims about Open WebUI (the shape of `usage`, `output`, events, filter contract) should be
checked against the real Open WebUI source, which changes between versions — not assumed. Note the
Open WebUI version you tested against in your PR.
