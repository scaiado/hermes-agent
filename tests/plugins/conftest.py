"""Skip Fuli-package-dependent tests when the Fuli sidecar is not installed."""
from __future__ import annotations

import importlib.util

import pytest


FULI_AVAILABLE = importlib.util.find_spec("fuli") is not None


def pytest_collection_modifyitems(config, items):
    if FULI_AVAILABLE:
        return
    skip_marker = pytest.mark.skip(reason="Fuli-Memory-Core package not installed")
    for item in items:
        if "test_fuli_provider" in str(item.fspath):
            item.add_marker(skip_marker)
