# Contributing

Thanks for your interest in improving this plugin.

## What this project is

A single-file [Open WebUI](https://openwebui.com) **filter** plugin. There is no build step and
no install: the product is `usage_display.py`, which users paste into **Admin → Functions** in
Open WebUI. Everything else in this repo is the test harness, docs, and CI around that one file.

## Requirements

- Python 3.11+
- `make`

## Setup

```bash
python -m venv .venv
make install-dev
```

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
