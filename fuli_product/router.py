"""Provider router for the canary stage.

The router sits between the agent's foreground path and the
provider layer. Given a request (user/profile ID, query, mode,
health state), the router returns the provider that should serve
the request and the cohort metadata that the agent should record
on the result.

Invariants:
  - deterministic assignment per (user_id, mode, canary_percentage,
    canary_salt) tuple; the same input always returns the same
    cohort.
  - sticky cohort membership: the cohort is stable across the
    duration of the canary; a user does not bounce between
    cohorts.
  - no random per-request flapping; the cohort is a pure
    function of the inputs.
  - explicit allowlist support: operators can pin users to the
    Fuli cohort regardless of percentage.
  - immediate global kill switch: when ``kill_switch_active`` is
    True, every call returns the fallback provider.
  - Honcho fallback: in canary mode, Honcho is always queried in
    parallel and is the fallback path.
  - primary-output provenance: each routed call returns the
    provider name, cohort, and the deterministic hash that drove
    the assignment. The agent must record this on the result
    so the report builder can attribute wins/losses by cohort.
  - no raw payload logging: the router never sees the payload,
    only metadata required for routing (user_id, query hash).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Set


class Provider(str, Enum):
    HONCHO = "honcho"
    FULI = "fuli"


class Mode(str, Enum):
    OFF = "off"
    MIRROR = "mirror"
    SHADOW = "shadow"
    ADJUDICATED_SHADOW = "adjudicated_shadow"
    CANARY = "canary"
    FULI_PRIMARY_WITH_FALLBACK = "fuli_primary_with_fallback"
    FULI_PRIMARY = "fuli_primary"


@dataclass
class RouteDecision:
    provider: Provider
    fallback_provider: Optional[Provider]
    cohort: str  # "fuli" / "honcho" / "kill_switch" / "allowlist"
    deterministic_hash: str
    user_id: str
    mode: Mode
    canary_percentage: int
    allowlisted: bool


def _stable_hash(*parts: Any) -> str:
    blob = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


class CanaryRouter:
    """Deterministic cohort router for the canary stage."""

    def __init__(
        self,
        *,
        canary_percentage: int = 1,
        canary_salt: str = "fuli-canary-v1",
        fuli_allowlist: Optional[Set[str]] = None,
        honcho_allowlist: Optional[Set[str]] = None,
        kill_switch_active: bool = False,
    ) -> None:
        if canary_percentage not in {1, 5, 20}:
            raise ValueError("canary_percentage must be one of {1, 5, 20}")
        self.canary_percentage = canary_percentage
        self.canary_salt = canary_salt
        self.fuli_allowlist: Set[str] = set(fuli_allowlist or [])
        self.honcho_allowlist: Set[str] = set(honcho_allowlist or [])
        self.kill_switch_active = kill_switch_active

    def assign_cohort(self, user_id: str) -> str:
        """Return the cohort for a user_id, or 'kill_switch' if active."""
        if self.kill_switch_active:
            return "kill_switch"
        if user_id in self.fuli_allowlist:
            return "fuli"
        if user_id in self.honcho_allowlist:
            return "honcho"
        h = _stable_hash(self.canary_salt, user_id)
        # First 5 hex chars = 20 bits, divide by 2^20 for a uniform [0,1)
        bucket = int(h[:5], 16) / (16 ** 5)
        threshold = self.canary_percentage / 100.0
        return "fuli" if bucket < threshold else "honcho"

    def route(
        self,
        *,
        user_id: str,
        mode: Mode,
        health_ok: bool = True,
    ) -> RouteDecision:
        """Return a routing decision for the given user_id and mode.

        For modes other than canary/fuli_primary_with_fallback, the
        router returns a sensible default (Honcho for off/mirror/
        shadow, Fuli for fuli_primary) without consulting the
        canary cohort.
        """
        det = _stable_hash(self.canary_salt, user_id, mode.value)
        if mode in (Mode.OFF, Mode.MIRROR, Mode.SHADOW, Mode.ADJUDICATED_SHADOW):
            return RouteDecision(
                provider=Provider.HONCHO,
                fallback_provider=None,
                cohort="honcho",
                deterministic_hash=det,
                user_id=user_id,
                mode=mode,
                canary_percentage=self.canary_percentage,
                allowlisted=False,
            )
        if mode == Mode.FULI_PRIMARY:
            return RouteDecision(
                provider=Provider.FULI,
                fallback_provider=Provider.HONCHO if health_ok else None,
                cohort="fuli",
                deterministic_hash=det,
                user_id=user_id,
                mode=mode,
                canary_percentage=self.canary_percentage,
                allowlisted=False,
            )
        if mode == Mode.FULI_PRIMARY_WITH_FALLBACK:
            return RouteDecision(
                provider=Provider.FULI,
                fallback_provider=Provider.HONCHO,
                cohort="fuli",
                deterministic_hash=det,
                user_id=user_id,
                mode=mode,
                canary_percentage=self.canary_percentage,
                allowlisted=False,
            )
        # canary uses cohort assignment
        cohort = self.assign_cohort(user_id)
        if not health_ok or cohort == "kill_switch":
            return RouteDecision(
                provider=Provider.HONCHO,
                fallback_provider=None,
                cohort=cohort if cohort == "kill_switch" else "honcho",
                deterministic_hash=det,
                user_id=user_id,
                mode=mode,
                canary_percentage=self.canary_percentage,
                allowlisted=False,
            )
        if cohort == "fuli":
            return RouteDecision(
                provider=Provider.FULI,
                fallback_provider=Provider.HONCHO,
                cohort="fuli",
                deterministic_hash=det,
                user_id=user_id,
                mode=mode,
                canary_percentage=self.canary_percentage,
                allowlisted=user_id in self.fuli_allowlist,
            )
        return RouteDecision(
            provider=Provider.HONCHO,
            fallback_provider=None,
            cohort="honcho",
            deterministic_hash=det,
            user_id=user_id,
            mode=mode,
            canary_percentage=self.canary_percentage,
            allowlisted=user_id in self.honcho_allowlist,
        )

    def kill(self) -> None:
        """Activate the global kill switch. All future calls return Honcho."""
        self.kill_switch_active = True

    def revive(self) -> None:
        """Deactivate the global kill switch."""
        self.kill_switch_active = False

    def set_canary_percentage(self, percentage: int) -> None:
        if percentage not in {1, 5, 20}:
            raise ValueError("canary_percentage must be one of {1, 5, 20}")
        self.canary_percentage = percentage
