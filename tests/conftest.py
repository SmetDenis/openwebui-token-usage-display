"""Load usage_display.py and scripts/release.py as importable modules for the test suite."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parent.parent
USAGE_DISPLAY_PATH = REPO_ROOT / "usage_display.py"
RELEASE_SCRIPT_PATH = REPO_ROOT / "scripts" / "release.py"


def _load(name: str, path: Path) -> ModuleType:
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def usage_display_module() -> ModuleType:
    """Load usage_display.py as importable module `usage_display`."""
    return _load("usage_display", USAGE_DISPLAY_PATH)


@pytest.fixture(scope="session")
def release_module() -> ModuleType:
    """Load scripts/release.py (the release rules CI applies) as module `release`."""
    return _load("release", RELEASE_SCRIPT_PATH)


@pytest.fixture
def release_tags(release_module: ModuleType) -> dict[str, str]:
    """Released version -> tag. No tags fails in CI (it must fetch them) and skips locally."""
    tags: dict[str, str] = release_module.release_tags()
    if not tags:
        reason = "no release tags in this checkout (run `git fetch --tags`)"
        if os.environ.get("CI"):
            pytest.fail(f"{reason}: CI must check out with fetch-depth: 0")
        pytest.skip(reason)
    return tags
