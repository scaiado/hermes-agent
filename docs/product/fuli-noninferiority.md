# Fuli Non-Inferiority Report

## Verdict: **NOT EVALUABLE**

The non-inferiority gate requires blind adjudications drawn
from a real-world Fuli deployment. The seven-day
qualification (RUN_ID `p1-controlled-soak-20260716T123125Z`)
auto-paused with a Fuli secondary success rate of 79.96%
(below the 80% hard gate) and therefore did not produce
adjudication-eligible evidence.

The non-inferiority calculator (`fuli_product/evaluation/noninferiority.py`)
is implemented and tested. The canary quality gate logic
is also implemented and tested. Both pieces of
infrastructure are ready to evaluate a future adjudicated
corpus; today, the adjudicated corpus does not exist.

## Method (defined before evaluation, per the mission directive)

The non-inferiority calculator uses the following definitions
and margins. These are fixed and would be applied unchanged
to a future adjudicated corpus:

### Definitions

- **usable adjudications**: cases where the blind workflow
  recorded `candidate_a_better` or `candidate_b_better`
  (excluding `tie`, `both_bad`, `insufficient_evidence`).
- **Fuli win share** (excluding ties): `fuli_wins /
  usable_adjudications`.
- **Net preference**: `(fuli_wins - honcho_wins) /
  usable_adjudications`.
- **Bootstrap 95% CI**: 2,000 bootstrap resamples with a
  fixed seed; the 2.5% and 97.5% percentiles of the
  resampled net preference.

### Margins

- **Overall non-inferiority**: Fuli may be no more than 10
  percentage points worse than Honcho. The lower bound of
  the bootstrap 95% CI must be greater than -10pp AND the
  point estimate must be greater than -10pp.
- **Per-query-type floor**: no major query type point
  estimate worse than -20pp.
- **Contradiction floor**: contradiction cases not worse
  than -10pp.
- **Fuli success floor**: Fuli retrieval success >= 99%.
- **Fuli latency floor**: p95 latency <= 500 ms.
- **Hard operational gates**: no privacy, namespace,
  persistence, or queue-drop failures.

### Canary quality gate (overall_pass)

The canary gate passes when **all** of the following hold:

1. usable_at_least_100 (usable adjudications >= 100)
2. ci_low_above_minus_10pp (bootstrap CI lower > -10pp)
3. net_above_minus_10pp (point estimate > -10pp)
4. per_type_above_minus_20pp (every major query type > -20pp)
5. contradiction_above_minus_10pp (contradiction cases > -10pp)
6. fuli_success_at_least_99pct (>= 99%)
7. p95_latency_under_500ms (<= 500 ms)
8. No hard operational gate failure.

These margins are fixed. They are not changed after
seeing the results.

## Why this is NOT EVALUABLE today

The mission's Stage 6 requires a non-inferiority evaluation
based on real adjudications from a real Fuli deployment.
The seven-day soak auto-paused. There is no real
adjudicated corpus to evaluate. The non-inferiority
calculator and gate are implemented and tested, but they
have not been run on real adjudications.

## What is in place

- `fuli_product/evaluation/noninferiority.py` —
  calculator + gate.
- `tests/fuli_product/test_evidence.py` — 22 tests
  covering the calculator and gate logic, including:
  - balanced adjudication summary
  - Fuli-losing adjudication summary
  - gate PASS on balanced 1000-case scenario
  - gate FAIL on Fuli-losing-by-20pp scenario
  - gate FAIL when n < 100
  - bootstrap CI is deterministic and reproducible

## Bootstrap CI sanity check (synthetic, not real)

To prove the bootstrap CI math, the test suite includes a
synthetic 1000-case 50/50 scenario. The bootstrap CI
narrows to roughly +/- 6pp on N=1000 (down from +/- 18pp
on N=100). This is the expected behavior and confirms the
bootstrap is well-calibrated.

## What is required for a real evaluation

A subsequent, separately authorized mission must:

1. Re-run the seven-day qualification to completion (no
   early auto-pause). Investigate and remediate the
   20% Fuli secondary failure rate first.
2. Build a real adjudicated corpus of >= 100 blind
   adjudications, drawn from the qualification's redacted
   disagreements export.
3. Run `fuli_product.evaluation.noninferiority.summary()`
   on the adjudicated corpus.
4. Run `fuli_product.evaluation.noninferiority.canary_gate()`
   on the resulting report.
5. Report the verdict to the operator.

This mission did not execute any of those steps because
the prerequisite (qualification PASS) was not met.
