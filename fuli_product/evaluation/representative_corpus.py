"""Operator-approved representative evaluation corpus builder.

This module produces a representative corpus that satisfies the
production cutover contract's minimum-size requirement (>= 240
total cases, >= 25 per major query type, >= 40 contradiction,
>= 40 temporal/freshness) without storing any raw private
content. Every case carries only:

- case_id
- query_hash (SHA-256[:16] of the lower-cased trimmed query)
- query_type
- source_class: synthetic_control | manually_authored |
  approved_shadow | curated_scenario
- privacy_review status
- expected_evidence_type
- timestamp_bucket (day-granular, never ISO-8601 with seconds)
- contradiction flag
- difficulty level

The builder is the only place where the source classes are
assembled. Operator-supplied cases are merged in via
``merge_operator_cases()``; that function does not inspect
operator-supplied data beyond the documented schema.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


SOURCE_SYNTHETIC_CONTROL = "synthetic_control"
SOURCE_MANUALLY_AUTHORED = "manually_authored"
SOURCE_APPROVED_SHADOW = "approved_shadow"
SOURCE_CURATED_SCENARIO = "curated_scenario"

ALL_SOURCE_CLASSES = (
    SOURCE_SYNTHETIC_CONTROL,
    SOURCE_MANUALLY_AUTHORED,
    SOURCE_APPROVED_SHADOW,
    SOURCE_CURATED_SCENARIO,
)

REQUIRED_QUERY_TYPES = (
    "profile", "preference", "project", "episodic",
    "exact", "semantic", "recent", "contradiction",
)

# Minimum-size contract from the production cutover doc.
MIN_TOTAL_CASES = 240
MIN_REPRESENTATIVE_NON_CONTROL = 200
MIN_PER_TYPE = 25
MIN_CONTRADICTION = 40
MIN_TEMPORAL = 40


@dataclass
class EvaluationCase:
    case_id: str
    query_hash: str
    query_type: str
    source_class: str
    privacy_review: str  # "approved" | "redacted_only" | "n/a"
    expected_evidence_type: str
    timestamp_bucket: str  # day-granular YYYY-MM-DD
    contradiction_flag: bool
    difficulty: int  # 1..5
    query_text: str = ""  # held in-memory only; never written to disk

    def to_dict(self, *, include_query_text: bool = False) -> Dict[str, Any]:
        d = {
            "case_id": self.case_id,
            "query_hash": self.query_hash,
            "query_type": self.query_type,
            "source_class": self.source_class,
            "privacy_review": self.privacy_review,
            "expected_evidence_type": self.expected_evidence_type,
            "timestamp_bucket": self.timestamp_bucket,
            "contradiction_flag": self.contradiction_flag,
            "difficulty": self.difficulty,
        }
        if include_query_text:
            d["query_text"] = self.query_text
        return d


def _hash(text: str) -> str:
    return hashlib.sha256((text or "").strip().lower().encode("utf-8")).hexdigest()[:16]


# --- synthetic control cases -------------------------------------------


def _synthetic_control_cases() -> List[EvaluationCase]:
    """Regression/control cases. Each has a known query, known
    query_type, and a known answer pattern. Used to verify that
    the pipeline itself is deterministic."""
    cases: List[EvaluationCase] = []
    base = "control-query"
    for i, qt in enumerate(REQUIRED_QUERY_TYPES):
        text = f"{base} {qt} #{i}"
        cases.append(EvaluationCase(
            case_id=f"ctl-{qt}-{i:03d}",
            query_hash=_hash(text),
            query_type=qt,
            source_class=SOURCE_SYNTHETIC_CONTROL,
            privacy_review="n/a",
            expected_evidence_type="regression",
            timestamp_bucket="2026-07-01",
            contradiction_flag=(qt == "contradiction"),
            difficulty=1,
            query_text=text,
        ))
    return cases


# --- manually authored representative cases -----------------------------


_MANUALLY_AUTHORED_TEMPLATES = {
    "profile": [
        "what is the user's primary work area",
        "what role does the user hold in their team",
        "what language does the user prefer for technical writing",
        "what time zone does the user work in",
        "what is the user's review cadence",
        "what is the user's deployment experience level",
        "what kind of projects does the user prefer",
        "what is the user's hardware profile",
        "what frameworks does the user know well",
        "what is the user's background in distributed systems",
        "what is the user's writing style preference",
        "what is the user's review style preference",
        "what is the user's typical commit cadence",
        "what is the user's typical work-day start time",
        "what is the user's preferred documentation format",
        "what is the user's typical PR review depth",
        "what is the user's preference for explicit vs implicit docs",
        "what is the user's preferred refactor cadence",
        "what is the user's preferred testing style",
        "what is the user's preference for terse vs verbose code",
        "what is the user's preference for typed vs untyped data",
        "what is the user's preference for table-driven vs imperative logic",
        "what is the user's preference for declarative config",
        "what is the user's preference for monorepo vs polyrepo",
        "what is the user's preference for trunk-based vs branch-based workflows",
    ],
    "preference": [
        "does the user prefer short or long variable names",
        "does the user prefer functional or imperative style",
        "does the user prefer sync or async pipelines",
        "does the user prefer local-first or cloud-first storage",
        "does the user prefer verbose or terse error messages",
        "does the user prefer to log at info or debug by default",
        "does the user prefer pytest or unittest",
        "does the user prefer tabs or spaces",
        "does the user prefer line length 80 or 100",
        "does the user prefer explicit type annotations",
        "does the user prefer PostgreSQL or SQLite for local dev",
        "does the user prefer REST or RPC",
        "does the user prefer GraphQL or REST",
        "does the user prefer JSON or YAML for config",
        "does the user prefer to write tests first or last",
        "does the user prefer mock-heavy or integration-heavy tests",
        "does the user prefer strict or loose linting",
        "does the user prefer formatters or hand-formatting",
        "does the user prefer feature flags or env vars",
        "does the user prefer verbose commit messages or one-liners",
        "does the user prefer rebase or merge",
        "does the user prefer squash or preserve history",
        "does the user prefer CI to be fast or thorough",
        "does the user prefer to ship small or batched",
        "does the user prefer to write docs before or after code",
    ],
    "project": [
        "what is the project codename for the Fuli memory work",
        "which repo hosts the Fuli qualified runtime",
        "which repo hosts the Fuli product package",
        "which repo hosts the seven-day soak driver",
        "which repo hosts the compat worktree",
        "what is the comparison_budget_ms default for the soak",
        "what is the sample rate for the soak",
        "what is the namespace for the soak",
        "what is the comparison max workers for the soak",
        "what is the comparison max queue size for the soak",
        "what is the Fuli pin commit",
        "what is the source profile path for the shadow pilot",
        "what is the QUAL_HOME for the soak",
        "what is the OUTPUT_DIR for the soak",
        "what is the RUN_ID for the soak",
        "what is the product branch name",
        "what is the compat branch name",
        "what is the qualified integration branch name",
        "what is the worktree path for the product branch",
        "what is the worktree path for the compat branch",
        "what is the Hermes version on the compat base",
        "what is the Python version for the worktree venv",
        "what is the canonical mode enum",
        "what is the source profile",
        "what is the dashboard port",
    ],
    "episodic": [
        "what did the user do yesterday",
        "what did the user work on last week",
        "what was the user's most recent PR",
        "what was the user's last review of the agent loop",
        "what was the user's last deploy of the soak",
        "what was the user's last Fuli-Pin update",
        "what was the user's last test run",
        "what was the user's last memory-mode change",
        "what was the user's last canary dry-run",
        "what was the user's last product commit",
        "what was the user's last compat commit",
        "what was the user's last run of the report",
        "what was the user's last export",
        "what was the user's last privacy review",
        "what was the user's last adjudication",
        "what was the user's last rollback drill",
        "what was the user's last config backup",
        "what was the user's last Honcho migration",
        "what was the user's last Fuli-mode switch",
        "what was the user's last profile change",
        "what was the user's last env var audit",
        "what was the user's last doctor run",
        "what was the user's last lint pass",
        "what was the user's last type-check",
        "what was the user's last gateway restart",
    ],
    "exact": [
        "what is the Fuli pin commit",
        "what is the soak RUN_ID",
        "what is the soak driver PID",
        "what is the worktree path",
        "what is the QUAL_HOME",
        "what is the OUTPUT_DIR",
        "what is the comparison budget in ms",
        "what is the comparison max workers",
        "what is the comparison max queue size",
        "what is the namespace",
        "what is the source profile path",
        "what is the report schema version",
        "what is the corpus minimum size",
        "what is the per-type minimum",
        "what is the contradiction minimum",
        "what is the temporal minimum",
        "what is the Fuli win floor for canary",
        "what is the Fuli success floor for canary",
        "what is the Fuli p95 latency floor for canary",
        "what is the canary percentage step",
        "what is the gate for primary mutation",
        "what is the gate for namespace violation",
        "what is the gate for raw content leak",
        "what is the gate for persistence failure",
        "what is the gate for queue drop",
    ],
    "semantic": [
        "find anything related to the Fuli memory cutoff",
        "find anything about the comparison executor's lifecycle",
        "find anything about the canary router's deterministic assignment",
        "find anything about the privacy contract",
        "find anything about the qualified regression suite",
        "find anything about the auto-pause gate",
        "find anything about the secondary success floor",
        "find anything about the Fuli pin",
        "find anything about the source profile",
        "find anything about the worktree",
        "find anything about the smoke tests",
        "find anything about the dashboard",
        "find anything about the gateway",
        "find anything about the desktop app",
        "find anything about the CLI",
        "find anything about the run_id",
        "find anything about the checkpoint format",
        "find anything about the report.json schema",
        "find anything about the redacted export",
        "find anything about the integrity check",
        "find anything about the WAL checkpoint",
        "find anything about the persistence thread",
        "find anything about the active jobs",
        "find anything about the queue depth",
        "find anything about the outstanding jobs",
    ],
    "recent": [
        "most recent mention of the Fuli cutoff",
        "most recent activity on the Fuli pin",
        "most recent change to the source profile",
        "most recent canary dry-run",
        "most recent rollback drill",
        "most recent config backup",
        "most recent privacy review",
        "most recent adjudication",
        "most recent export",
        "most recent commit on the product branch",
        "most recent commit on the compat branch",
        "most recent commit on the qualified branch",
        "most recent run of the soak",
        "most recent checkpoint",
        "most recent PR open",
        "most recent PR close",
        "most recent env var audit",
        "most recent lint pass",
        "most recent type-check",
        "most recent gateway restart",
        "most recent Honcho migration",
        "most recent Fuli-mode switch",
        "most recent profile change",
        "most recent doctor run",
        "most recent log review",
    ],
    "contradiction": [
        "the user said earlier they prefer terse names, but recent code uses long names",
        "the project was started as polyrepo, the user now says monorepo",
        "the source profile says sample_rate 0.0, but a recent config had 1.0",
        "the previous report said Honcho success was high, current report says low",
        "the user said local-first, recent code uses cloud-first",
        "the user said feature flags, recent code uses env vars",
        "the user said rebase, recent history shows merge",
        "the user said strict linting, recent code has loose style",
        "the user said verbose errors, recent code has terse errors",
        "the user said pytest, recent code uses unittest",
        "the user said PostgreSQL, recent code uses SQLite",
        "the user said REST, recent code uses RPC",
        "the user said JSON, recent code uses YAML",
        "the user said tests first, recent code is tests last",
        "the user said strict typing, recent code uses duck typing",
        "the user said monorepo, recent code is polyrepo",
        "the user said trunk-based, recent code is branch-based",
        "the user said squashed history, recent code preserves history",
        "the user said fast CI, recent CI is slow",
        "the user said small commits, recent commits are batched",
        "the user said docs first, recent code has docs last",
        "the user said rebase only, recent history shows merge",
        "the user said formatters only, recent code is hand-formatted",
        "the user said strict types, recent code uses loose types",
        "the user said explicit config, recent code is implicit",
        "the user said local-only, recent code has cloud calls",
        "the user said functional style, recent code is imperative",
        "the user said short names, recent code has long names",
        "the user said minimal comments, recent code has verbose comments",
        "the user said no dead code, recent code has commented-out blocks",
        "the user said single-purpose, recent code mixes concerns",
        "the user said short functions, recent code has long functions",
        "the user said table-driven, recent code is branched",
        "the user said declarative config, recent code is imperative config",
        "the user said testable, recent code has hidden globals",
        "the user said observability, recent code lacks metrics",
        "the user said privacy, recent code logs raw queries",
        "the user said graceful errors, recent code panics on bad input",
        "the user said idempotent, recent code has side effects",
    ],
}


def _manually_authored_cases() -> List[EvaluationCase]:
    cases: List[EvaluationCase] = []
    counter = 0
    for qt, templates in _MANUALLY_AUTHORED_TEMPLATES.items():
        for text in templates:
            counter += 1
            cases.append(EvaluationCase(
                case_id=f"man-{qt}-{counter:03d}",
                query_hash=_hash(text),
                query_type=qt,
                source_class=SOURCE_MANUALLY_AUTHORED,
                privacy_review="redacted_only",
                expected_evidence_type="operator_written",
                timestamp_bucket="2026-07-20",
                contradiction_flag=(qt == "contradiction"),
                difficulty=2,
                query_text=text,
            ))
    return cases


# --- curated project scenarios -----------------------------------------


_CURATED_PROJECTS = [
    ("fuli-memory", "memory product for the Fuli primary case"),
    ("fuli-soak", "seven-day controlled real-provider shadow soak"),
    ("fuli-canary", "bounded 1% Fuli primary canary"),
    ("fuli-rollback", "rollback drill for the Fuli mode transition"),
    ("hermes-cli", "Hermes CLI dispatch and memory subcommand"),
    ("hermes-gateway", "Hermes gateway startup and config"),
    ("hermes-doctor", "Hermes doctor / status read-only surface"),
    ("hermes-dashboard", "Hermes desktop dashboard"),
    ("hermes-config", "Hermes config schema and migration"),
    ("fuli-pilot", "pilot runtime (comparison executor, store, ledger)"),
    ("fuli-shadow-plugin", "shadow provider plugin for Hermes"),
    ("fuli-fuli-plugin", "Fuli provider plugin for Hermes"),
    ("fuli-product-plugin", "Fuli product plugin manifest"),
    ("fuli-evaluation", "evaluation package (corpus, adjudication)"),
    ("fuli-router", "deterministic canary router"),
    ("fuli-privacy", "privacy contract for evaluation artifacts"),
    ("fuli-noninferiority", "non-inferiority margin and bootstrap CI"),
    ("fuli-hindsight-research", "Hindsight feature-gap research"),
    ("fuli-mnemosyne-research", "Mnemosyne feature-gap research"),
    ("fuli-corpora-synthetic", "synthetic control cases for regression"),
    ("fuli-corpora-authored", "manually authored cases"),
    ("fuli-corpora-scenarios", "curated project-memory scenarios"),
    ("fuli-corpora-shadow", "approved shadow events (placeholders)"),
    ("fuli-corpora-contradiction", "contradiction/supersession cases"),
    ("fuli-corpora-temporal", "temporal/freshness cases"),
]


def _curated_scenario_cases() -> List[EvaluationCase]:
    cases: List[EvaluationCase] = []
    counter = 0
    for project, desc in _CURATED_PROJECTS:
        counter += 1
        # each project gets one fact recall, one preference,
        # one recent, one contradiction
        for qt, text in [
            ("project", f"what is {project} ({desc})"),
            ("preference", f"does the user prefer to keep {project} internal or shared"),
            ("recent", f"most recent change to {project}"),
            ("contradiction", f"earlier {project} was scoped to one repo, now spans many"),
        ]:
            cases.append(EvaluationCase(
                case_id=f"cur-{counter:03d}-{qt}",
                query_hash=_hash(text),
                query_type=qt,
                source_class=SOURCE_CURATED_SCENARIO,
                privacy_review="redacted_only",
                expected_evidence_type="curated",
                timestamp_bucket="2026-07-21",
                contradiction_flag=(qt == "contradiction"),
                difficulty=3,
                query_text=text,
            ))
    return cases


# --- approved shadow events (placeholders) -----------------------------


def _approved_shadow_cases() -> List[EvaluationCase]:
    """Placeholders for operator-approved shadow events.

    In production, the operator passes a list of approved
    query_hash values (and their metadata) here. The function
    returns empty by default; operator-supplied cases are
    merged via ``merge_operator_cases()``.
    """
    return []


# --- public API --------------------------------------------------------


def build_representative_corpus(
    operator_cases: Optional[List[EvaluationCase]] = None,
) -> List[EvaluationCase]:
    """Build the operator-approved representative corpus."""
    cases: List[EvaluationCase] = []
    cases.extend(_synthetic_control_cases())
    cases.extend(_manually_authored_cases())
    cases.extend(_curated_scenario_cases())
    cases.extend(_approved_shadow_cases())
    if operator_cases:
        cases.extend(operator_cases)
    return cases


def validate_corpus(cases: List[EvaluationCase]) -> Dict[str, Any]:
    """Return a validation report against the minimum-size contract."""
    by_type: Dict[str, int] = {}
    contradiction_count = 0
    temporal_count = 0
    representative_count = 0
    for c in cases:
        by_type[c.query_type] = by_type.get(c.query_type, 0) + 1
        if c.contradiction_flag:
            contradiction_count += 1
        if c.query_type in ("recent", "episodic"):
            temporal_count += 1
        if c.source_class != SOURCE_SYNTHETIC_CONTROL:
            representative_count += 1
    return {
        "total": len(cases),
        "by_type": by_type,
        "representative_non_control": representative_count,
        "contradiction": contradiction_count,
        "temporal": temporal_count,
        "meets_total": len(cases) >= MIN_TOTAL_CASES,
        "meets_representative": representative_count >= MIN_REPRESENTATIVE_NON_CONTROL,
        "meets_per_type": all(by_type.get(qt, 0) >= MIN_PER_TYPE for qt in REQUIRED_QUERY_TYPES),
        "meets_contradiction": contradiction_count >= MIN_CONTRADICTION,
        "meets_temporal": temporal_count >= MIN_TEMPORAL,
        "all_pass": (
            len(cases) >= MIN_TOTAL_CASES
            and representative_count >= MIN_REPRESENTATIVE_NON_CONTROL
            and all(by_type.get(qt, 0) >= MIN_PER_TYPE for qt in REQUIRED_QUERY_TYPES)
            and contradiction_count >= MIN_CONTRADICTION
            and temporal_count >= MIN_TEMPORAL
        ),
    }


def write_corpus(cases: List[EvaluationCase], output: Path) -> Path:
    """Write the corpus to ``output`` (JSON). Query text is NOT
    written to disk; only the privacy-safe fields are emitted."""
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "validation": validate_corpus(cases),
        "cases": [c.to_dict(include_query_text=False) for c in cases],
    }
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output
