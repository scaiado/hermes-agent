"""Blind adjudication outcomes and rationale codes.

Adjudication outcomes (per the production cutover contract):
- ``fuli_better``: Fuli result is preferred.
- ``honcho_better``: Honcho result is preferred.
- ``tie``: Both are equivalent.
- ``both_bad``: Neither result is acceptable.
- ``insufficient_evidence``: Cannot determine a winner.

Rationale codes capture the dimension on which the comparison
turns: relevance, completeness, recency, accuracy, contradiction
handling. These are the reason codes a human adjudicator can
select after the blind comparison is presented.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional


class AdjudicationOutcome(str, Enum):
    fuli_better = "fuli_better"
    honcho_better = "honcho_better"
    tie = "tie"
    both_bad = "both_bad"
    insufficient_evidence = "insufficient_evidence"


class ReasonCode(str, Enum):
    FULI_MORE_RELEVANT = "fuli_more_relevant"
    FULI_MORE_COMPLETE = "fuli_more_complete"
    FULI_MORE_RECENT = "fuli_more_recent"
    FULI_MORE_ACCURATE = "fuli_more_accurate"
    FULI_HANDLED_CONTRADICTION = "fuli_handled_contradiction"
    HONCHO_MORE_RELEVANT = "honcho_more_relevant"
    HONCHO_MORE_COMPLETE = "honcho_more_complete"
    HONCHO_MORE_RECENT = "honcho_more_recent"
    HONCHO_MORE_ACCURATE = "honcho_more_accurate"
    HONCHO_HANDLED_CONTRADICTION = "honcho_handled_contradiction"
    TIE = "tie"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


REASON_CODES: Dict[str, str] = {
    "fuli_more_relevant": "Fuli result is more relevant to the query",
    "fuli_more_complete": "Fuli result is more complete",
    "fuli_more_recent": "Fuli result is more recent",
    "fuli_more_accurate": "Fuli result is more accurate",
    "fuli_handled_contradiction": "Fuli handled a contradiction better",
    "honcho_more_relevant": "Honcho result is more relevant to the query",
    "honcho_more_complete": "Honcho result is more complete",
    "honcho_more_recent": "Honcho result is more recent",
    "honcho_more_accurate": "Honcho result is more accurate",
    "honcho_handled_contradiction": "Honcho handled a contradiction better",
    "tie": "Results were equivalent",
    "insufficient_evidence": "Could not determine a winner",
}


@dataclass
class Adjudication:
    """A single blind adjudication result."""

    comparison_id: str
    outcome: AdjudicationOutcome
    reason_code: ReasonCode
    query_type: str
    adjudicator: str
    timestamp: str
    notes: Optional[str] = None

    def to_dict(self) -> Dict[str, str]:
        return {
            "comparison_id": self.comparison_id,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value,
            "query_type": self.query_type,
            "adjudicator": self.adjudicator,
            "timestamp": self.timestamp,
            "notes": self.notes or "",
        }


def summarize(judgments: List[Adjudication]) -> Dict[str, int]:
    """Tally a list of adjudications by outcome."""
    out: Dict[str, int] = {o.value: 0 for o in AdjudicationOutcome}
    for j in judgments:
        out[j.outcome.value] += 1
    return out
