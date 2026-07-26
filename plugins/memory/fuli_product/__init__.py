"""Fuli product plugin for Hermes memory provider system.

This plugin registers the product lifecycle and CLI commands. It does not
replace the Fuli MemoryProvider; it orchestrates the shadow mode when the user
has configured ``memory.provider`` to use the shadow or Fuli provider.
"""

from __future__ import annotations

from typing import Any


def register(ctx: Any) -> None:
    """Register the Fuli product manager."""
    # No MemoryProvider is registered here; the existing Fuli and Shadow
    # providers are loaded independently. This plugin is only a hook for future
    # CLI extensions or dashboard registration.
    pass


def is_available() -> bool:
    """Return True if the Fuli product package is importable."""
    try:
        import fuli_product  # noqa: F401
        return True
    except Exception:
        return False
