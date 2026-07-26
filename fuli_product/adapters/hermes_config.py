"""Hermes-specific ConfigRepository implementation.

Imports ``hermes_cli.config`` only inside this module, so the rest of
``fuli_product`` can be imported without the full Hermes runtime.
"""

from __future__ import annotations

import copy
import hashlib
import logging
import shutil
import time
from pathlib import Path
from typing import Any, Dict, Optional

from fuli_product.config import ConfigRepository

logger = logging.getLogger(__name__)


class InMemoryConfigRepository(ConfigRepository):
    """In-memory config repository for unit tests."""

    def __init__(self, config: Optional[Dict[str, Any]] = None, *, path: Optional[Path] = None) -> None:
        self._config = copy.deepcopy(config) or {}
        self.config_path = path or Path("memory://in-memory-config")
        self.backups: list[Path] = []
        self.last_reason: str = ""

    def read_dict(self) -> Dict[str, Any]:
        return copy.deepcopy(self._config)

    def write_dict(
        self, data: Dict[str, Any], *, backup: bool = True, reason: str = ""
    ) -> Dict[str, Any]:
        self.last_reason = reason
        if backup and str(self.config_path) != "memory://in-memory-config":
            ts = time.strftime("%Y%m%d-%H%M%S")
            suffix = reason.replace(" ", "_").replace("/", "_")[:40] if reason else "backup"
            backup_path = self.config_path.with_name(f"{self.config_path.name}.{suffix}.{ts}.bak")
            try:
                if self.config_path.exists():
                    shutil.copy2(self.config_path, backup_path)
                self.backups.append(backup_path)
            except Exception as exc:
                return {"errors": [f"backup failed: {exc}"], "backup_path": None, "written": False}
        self._config = copy.deepcopy(data)
        return {"errors": [], "backup_path": str(self.backups[-1]) if self.backups else None, "written": True}


class HermesConfigRepository(ConfigRepository):
    """Read/write Hermes YAML config with atomic updates and SHA tracking."""

    def __init__(self, config_path: Optional[Path] = None) -> None:
        if config_path is None:
            config_path = Path.home() / ".hermes" / "config.yaml"
        self.config_path = Path(config_path)

    def read_dict(self) -> Dict[str, Any]:
        """Read the YAML config at ``config_path``.

        We avoid the global ``hermes_cli.config.load_config()`` here because its
        signature and cache key behavior differ across Hermes versions. Direct
        YAML parsing keeps the adapter stable.
        """
        if not self.config_path.exists():
            return {}
        try:
            import yaml
        except ImportError:
            return {}
        try:
            with self.config_path.open("r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception as exc:
            logger.warning("Failed to read config %s: %s", self.config_path, exc)
            return {}
        return data or {}

    def write_dict(self, data: Dict[str, Any], *, backup: bool = True, reason: str = "") -> Dict[str, Any]:
        from hermes_cli.config import atomic_config_write
        summary: Dict[str, Any] = {"backup_path": None, "new_sha256": None, "errors": []}
        if backup:
            ts = time.strftime("%Y%m%d-%H%M%S")
            suffix = reason.replace(" ", "_").replace("/", "_")[:40] if reason else "backup"
            backup_path = self.config_path.with_name(f"{self.config_path.name}.{suffix}.{ts}.bak")
            try:
                shutil.copy2(self.config_path, backup_path)
                summary["backup_path"] = str(backup_path)
            except Exception as exc:
                summary["errors"].append(f"Backup failed: {exc}")
                return summary
        try:
            atomic_config_write(self.config_path, data)
            new_text = self.config_path.read_text(encoding="utf-8")
            summary["new_sha256"] = hashlib.sha256(new_text.encode("utf-8")).hexdigest()
        except Exception as exc:
            summary["errors"].append(f"Config write failed: {exc}")
        return summary
