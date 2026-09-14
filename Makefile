# Every tool runs from the uv-managed .venv at the exact versions in uv.lock (--locked fails if
# the lockfile is out of date with pyproject.toml), so `make` and CI use identical tooling.
RUN := uv run --locked
PYTEST := $(RUN) pytest
RUFF := $(RUN) ruff
MYPY := $(RUN) mypy

HASH := \#
PY_LINT_TARGETS := usage_display.py tests

.DEFAULT_GOAL := help

.PHONY: help install-dev lock upgrade test test-cov lint format typecheck pre-commit clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?$(HASH)$(HASH) .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?$(HASH)$(HASH) "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install-dev:  ## Create/sync .venv with the exact dev tools from uv.lock
	uv sync --locked

lock:  ## Re-lock after editing [dependency-groups] in pyproject.toml
	uv lock

upgrade:  ## Upgrade every locked dev tool to its latest allowed version
	uv lock --upgrade

test:  ## Run pytest suite
	$(PYTEST) tests

test-cov:  ## Run pytest with coverage (term-missing + HTML report)
	$(PYTEST) tests --cov --cov-report=term-missing --cov-report=html

lint:  ## Lint and format-check Python via ruff
	$(RUFF) check $(PY_LINT_TARGETS)
	$(RUFF) format --check $(PY_LINT_TARGETS)

format:  ## Auto-format Python via ruff
	$(RUFF) format $(PY_LINT_TARGETS)
	$(RUFF) check --fix $(PY_LINT_TARGETS)

typecheck:  ## Run mypy strict type-check
	$(MYPY)

pre-commit: lint typecheck test-cov  ## Full pre-commit gate (lint + typecheck + tests)

clean:  ## Remove caches and coverage artifacts
	rm -rf .coverage coverage.xml htmlcov .pytest_cache .mypy_cache .ruff_cache
