"""Adapters for running the Fuli product against Hermes infrastructure.

The adapters deliberately isolate the product core from Hermes internals:

- ``ConfigRepository`` / ``InMemoryConfigRepository`` / ``HermesConfigRepository``:
  config persistence protocol and implementations.
- ``FuliComparisonRuntime``: wraps the qualified ``pilot.comparison_executor``
  without duplicating its threadpool or SQLite code.
"""

from fuli_product.adapters.hermes_config import HermesConfigRepository, InMemoryConfigRepository
from fuli_product.adapters.pilot_executor import FuliComparisonRuntime

__all__ = ["HermesConfigRepository", "InMemoryConfigRepository", "FuliComparisonRuntime"]
