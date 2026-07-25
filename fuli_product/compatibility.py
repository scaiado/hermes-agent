"""Compatibility matrix between Hermes, Fuli, and the product runtime.

All checks are local and cheap. They do not require network access or Fuli
initialization.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Tuple

logger = logging.getLogger(__name__)

# Supported Hermes versions for the v0.1.0 product release.
# Tuples: (major, minor, patch) inclusive ranges.
SUPPORTED_HERMES_RANGES: List[Tuple[Tuple[int, int, int], Tuple[int, int, int]]] = [
    ((0, 18, 2), (0, 19, 0)),
]

# Minimum Fuli package version that implements the required provider API.
MIN_FULI_VERSION = (0, 0, 0)  # placeholder until Fuli publishes a stable semver
MAX_FULI_VERSION = (999, 0, 0)

# Pin commit from the Fuli-Memory-Core P0 baseline. Product refuses to operate
# with a Fuli core whose version info does not match this pin unless the
# ``disable_fuli_pin_check`` override is set in config.
FULI_PIN_COMMIT = "727ce92603707619e0155a6c0ca1a01f5f2e07c4"


def _parse_version(version: str) -> Tuple[int, int, int]:
    """Parse a dotted version string into a (major, minor, patch) tuple."""
    version = version.strip().lstrip("v")
    parts = version.split(".", 2)
    try:
        return (int(parts[0]), int(parts[1] or 0), int(parts[2] or 0))
    except (ValueError, IndexError):
        return (0, 0, 0)


def _version_in_range(ver: Tuple[int, int, int], low: Tuple[int, int, int], high: Tuple[int, int, int]) -> bool:
    return low <= ver <= high


def get_hermes_version() -> str:
    """Return the version of the running Hermes installation."""
    try:
        import importlib.metadata
        return importlib.metadata.version("hermes-agent")
    except Exception:
        try:
            from hermes_cli import __version__ as hermes_version
            return str(hermes_version)
        except Exception:
            return "0.0.0"


def get_fuli_version() -> Dict[str, Any]:
    """Return Fuli version metadata without importing heavy internals."""
    try:
        import fuli
        version = getattr(fuli, "__version__", "0.0.0")
        commit = getattr(fuli, "__commit__", "")
        return {
            "available": True,
            "version": str(version),
            "commit": str(commit) if commit else "",
            "module": str(getattr(fuli, "__file__", "")),
        }
    except Exception as exc:
        return {"available": False, "version": "", "commit": "", "module": "", "error": str(exc)}


def check_fuli_version(fuli_info: Dict[str, Any]) -> List[str]:
    """Return compatibility errors for the installed Fuli version."""
    errors: List[str] = []
    if not fuli_info.get("available"):
        errors.append("Fuli package is not installed or not importable")
        return errors

    ver = _parse_version(fuli_info.get("version", "0.0.0"))
    if not _version_in_range(ver, MIN_FULI_VERSION, MAX_FULI_VERSION):
        errors.append(
            f"Fuli version {fuli_info.get('version')!r} is outside supported range "
            f"{'.'.join(map(str, MIN_FULI_VERSION))}-{'.'.join(map(str, MAX_FULI_VERSION))}"
        )

    commit = fuli_info.get("commit", "")
    if commit and commit != FULI_PIN_COMMIT:
        errors.append(
            f"Fuli commit {commit[:16]!r} does not match pinned commit {FULI_PIN_COMMIT[:16]!r}. "
            f"Use disable_fuli_pin_check=true to override."
        )

    return errors


def check_hermes_version(hermes_version: str) -> List[str]:
    """Return compatibility errors for the running Hermes version."""
    errors: List[str] = []
    ver = _parse_version(hermes_version)
    if not any(_version_in_range(ver, low, high) for low, high in SUPPORTED_HERMES_RANGES):
        ranges = [f"{'.'.join(map(str, low))}-{'.'.join(map(str, high))}" for low, high in SUPPORTED_HERMES_RANGES]
        errors.append(f"Hermes version {hermes_version!r} is not in supported ranges: {', '.join(ranges)}")
    return errors


def check_compatibility(
    *,
    hermes_version: str = "",
    fuli_info: Dict[str, Any] | None = None,
    disable_fuli_pin_check: bool = False,
) -> Dict[str, Any]:
    """Run all compatibility checks and return a structured report.

    The report is the input to the compatibility matrix JSON artifact.
    """
    hermes_version = hermes_version or get_hermes_version()
    fuli_info = fuli_info if fuli_info is not None else get_fuli_version()

    reasons: List[str] = []
    reasons.extend(check_hermes_version(hermes_version))
    reasons.extend(check_fuli_version(fuli_info))

    if disable_fuli_pin_check and any("commit" in r for r in reasons):
        reasons = [r for r in reasons if "does not match pinned commit" not in r]

    return {
        "hermes_version": hermes_version,
        "fuli_info": fuli_info,
        "incompatible": bool(reasons),
        "reasons": reasons,
        "supported_hermes_ranges": [
            {"min": ".".join(map(str, low)), "max": ".".join(map(str, high))}
            for low, high in SUPPORTED_HERMES_RANGES
        ],
        "fuli_pin_commit": FULI_PIN_COMMIT,
    }


def compatibility_matrix(
    hermes_versions: List[str] | None = None,
    fuli_versions: List[str] | None = None,
) -> Dict[str, Any]:
    """Generate a compatibility matrix for documentation and CI.

    ``hermes_versions`` and ``fuli_versions`` are lists of dotted version strings.
    If omitted, the matrix uses the default supported ranges and a set of Fuli
    versions.
    """
    hermes_versions = hermes_versions or ["0.18.2", "0.19.0"]
    fuli_versions = fuli_versions or ["0.0.0"]

    rows = []
    for hv in hermes_versions:
        hermes_errors = check_hermes_version(hv)
        for fv in fuli_versions:
            fuli_info = {"available": True, "version": fv, "commit": FULI_PIN_COMMIT}
            fuli_errors = check_fuli_version(fuli_info)
            rows.append({
                "hermes": hv,
                "fuli": fv,
                "compatible": not (hermes_errors or fuli_errors),
                "errors": hermes_errors + fuli_errors,
            })

    return {
        "generated_at": __import__("time").strftime("%Y-%m-%dT%H:%M:%SZ", __import__("time").gmtime()),
        "rows": rows,
    }


def write_compatibility_matrix(path: Path) -> Path:
    """Write the compatibility matrix JSON to the reports directory."""
    import json
    matrix = compatibility_matrix()
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(matrix, indent=2) + "\n", encoding="utf-8")
    return path
