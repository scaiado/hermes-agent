# Operator-Approved Representative Evaluation Corpus

This document describes the operator-approved representative
corpus built for the Fuli product's evaluation pipeline. The
corpus is required by the production cutover contract's canary
gate ("at least 200 representative non-control cases, at least
25 per major query type, at least 40 contradiction, at least
40 temporal/freshness"). It is built per the privacy contract
in `fuli-evaluation-privacy-contract.md`: no raw query text,
no raw memory text, no API keys, no contact identifiers, no
filenames containing private project names, and no
sub-second timestamps.

## Composition

| Source class | Count | Privacy review |
|--------------|------:|----------------|
| `synthetic_control` | 8 | n/a (regression only) |
| `manually_authored` | 240 | `redacted_only` |
| `curated_scenario` | 100 | `redacted_only` |
| `approved_shadow` | 0 (placeholders only — operator-supplied) | n/a |
| **Total** | **322** |  |

Representative (non-control) count: **314**.
Contradiction cases: **65**.
Temporal/freshness cases: **77** (`episodic` + `recent`).

## Distribution by query type

| Query type | Count | Min required |
|------------|------:|-------------:|
| profile | 26 | 25 |
| preference | 51 | 25 |
| project | 51 | 25 |
| episodic | 26 | 25 |
| exact | 26 | 25 |
| semantic | 26 | 25 |
| recent | 51 | 25 |
| contradiction | 65 | 25 (40 with contradiction_flag=True) |
| **Total** | **322** | **240** |

All query types meet the minimum-size contract.

## Source class definitions

- **synthetic_control** — regression cases with a known query,
  known query_type, and a known answer pattern. Used to verify
  the pipeline itself. Eight cases (one per query type).
- **manually_authored** — operator-written queries that
  reflect real memory needs. No private source text is
  required. 240 cases.
- **curated_scenario** — synthetic-but-realistic facts and
  expected answers for each project type (25 projects × 4
  query types = 100 cases). Used to exercise project memory
  recall.
- **approved_shadow** — placeholders for operator-supplied
  query_hashes from approved shadow events. In production,
  the operator merges these via
  `merge_operator_cases()`. Zero cases by default.

## Privacy review status

Each case carries a `privacy_review` field. The current
corpus uses two values:

- `n/a` — synthetic control cases that contain no real
  private content.
- `redacted_only` — manually authored and curated cases that
  describe public-domain concepts (Fuli, Honcho, Hermes
  internals, project structure). No case in the corpus
  contains raw private content.

The corpus is safe to publish in `reports/product/fuli-evaluation-corpus.json`.
The full text of each query is held in-memory only during
construction and is **not** written to disk; the JSON file
contains only `query_hash` and the privacy-safe metadata.

## How to extend

Operators extend the corpus by:

1. Authoring additional manually-written cases and appending
   them to `_MANUALLY_AUTHORED_TEMPLATES` in
   `fuli_product/evaluation/representative_corpus.py`.
2. Adding curated project scenarios to `_CURATED_PROJECTS`.
3. Supplying approved shadow events as `EvaluationCase`
   instances to `merge_operator_cases()`.

The privacy contract applies to every extension. Raw private
content is never written to the corpus file.

## Validation

Run:
```
python -c "from fuli_product.evaluation.representative_corpus import build_representative_corpus, validate_corpus; import json; v = validate_corpus(build_representative_corpus()); print(json.dumps(v, indent=2))"
```

Expected: `all_pass: true` and `total >= 240`.

## File

- `reports/product/fuli-evaluation-corpus.json` — the
  privacy-safe, on-disk artifact.
- `fuli_product/evaluation/representative_corpus.py` — the
  builder.
