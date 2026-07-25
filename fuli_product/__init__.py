"""Fuli Memory product top-level package.

This module re-exports the public API so that other parts of Hermes can
import ``from fuli_product import ...`` without reaching into submodules.
"""

from fuli_product.config import (
    ConfigRepository,
    FuliProductConfig,
    load_fuli_product_config,
    migrate_config,
    migrate_config_in_memory,
)
from fuli_product.lifecycle import FuliProductLifecycle
from fuli_product.health import classify_health, ProviderHealth
from fuli_product.compatibility import check_compatibility, get_hermes_version, get_fuli_version
from fuli_product.reporting import Reporter
from fuli_product.sampling import should_sample, classify_query_type
from fuli_product.evaluation import default_evaluation_corpus, Adjudication, StratifiedSampler

__all__ = [
    "ConfigRepository",
    "FuliProductConfig",
    "load_fuli_product_config",
    "migrate_config",
    "migrate_config_in_memory",
    "FuliProductLifecycle",
    "classify_health",
    "ProviderHealth",
    "check_compatibility",
    "get_hermes_version",
    "get_fuli_version",
    "Reporter",
    "should_sample",
    "classify_query_type",
    "default_evaluation_corpus",
    "Adjudication",
    "StratifiedSampler",
]

__version__ = "0.1.0-rc1"
