"""Load usage_display.py as an importable module for the test suite."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
USAGE_DISPLAY_PATH = REPO_ROOT / "usage_display.py"


@pytest.fixture(scope="session")
def usage_display_module() -> ModuleType:
    """Load usage_display.py as importable module `usage_display`."""
    loader = importlib.machinery.SourceFileLoader("usage_display", str(USAGE_DISPLAY_PATH))
    spec = importlib.util.spec_from_loader("usage_display", loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["usage_display"] = module
    loader.exec_module(module)
    return module
