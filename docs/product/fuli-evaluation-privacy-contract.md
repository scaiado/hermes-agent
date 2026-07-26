# Fuli Evaluation Privacy Contract

This contract governs what may be stored in the Fuli evaluation
corpus, the blind adjudication workflow, the canary telemetry,
and the export artifacts produced by every evaluation surface in
the Fuli product. It is a hard rule, not a recommendation.

No automatic capture of raw private content is permitted. Any
exception requires explicit operator approval per case.

## Allowed by default

The following are stored in evaluation artifacts as part of the
standard pipeline. Each is hashed, redacted, or aggregate
where possible.

- **query_hash** — SHA-256[:16] of the lower-cased, trimmed
  query text.
- **query_type** — one of the eight REQUIRED_QUERY_TYPES:
  `profile`, `preference`, `project`, `episodic`, `exact`,
  `semantic`, `recent`, `contradiction`.
- **timestamp_bucket** — hour- or day-bucketed timestamp, never
  full ISO-8601 with sub-second precision.
- **provider result fingerprints** — 16-char hex SHA-256
  fingerprints of each result item, never the result text.
- **rank positions** — integer ranks of the result items.
- **result count** — integer count of returned items.
- **latency** — milliseconds, integer.
- **freshness metadata** — day-granular (no time of day).
- **contradiction metadata** — supersede flag, supersession
  target fingerprint, reason code.
- **blind candidate labels** — opaque, deterministic per
  session; e.g. `candidate_A`, `candidate_B`.
- **adjudication outcome** — one of the five canonical
  outcomes (`candidate_a_better`, `candidate_b_better`,
  `tie`, `both_bad`, `insufficient_evidence`).
- **rationale code** — one of the 12 reason codes
  (`exact_match`, `semantic_relevance`, `freshness`,
  `contradiction_resolution`, `profile_accuracy`,
  `preference_accuracy`, `project_context`,
  `hallucination`, `stale_memory`, `missing_memory`,
  `privacy_concern`, `malformed_result`).

## Forbidden by default

The following MUST NOT appear in evaluation artifacts without
explicit operator approval per case.

- **raw user query** — never persisted. The query is reduced
  to a hash; the full text is held in-memory only during the
  immediate comparison and is never written to disk.
- **raw memory text** — never persisted. Provider responses
  are reduced to fingerprints.
- **API keys** — never persisted anywhere in evaluation
  artifacts.
- **email bodies** — never persisted; email is a private
  channel and is out of scope for the evaluation pipeline.
- **filenames containing private project names** — never
  persisted. If a filename is required for a test scenario,
  it must be redacted to a placeholder like `project-XYZ`.
- **contact identifiers** — phone numbers, addresses, full
  names of private individuals, account numbers: never
  persisted.
- **full timestamps that unnecessarily identify user
  activity** — the timestamp is bucketed to the hour or
  day, never the second.
- **provider payloads** — the raw response body of any
  provider call is never persisted.

## Operator approval mechanism for raw content review

Some cases may require the operator to inspect raw content to
debug a missed adjudication or to confirm a contradiction.
The approval flow is:

1. Operator identifies a case_id from the evaluation report.
2. Operator runs:
   `hermes memory fuli adjudicate raw-review --case-id <id>
    --reason "<justification>" --expires-at "<iso-timestamp>"`
3. The system logs the approval with the operator's
   pseudonym, the case_id, the justification, and the
   expiry. The approval is recorded in the audit log.
4. The raw content is then revealed only to the operator,
   in-memory, for the duration of the review session.
5. The raw content is never written to disk, never logged,
   and never transmitted to a third party.
6. After the session ends (or the expiry is reached), the
   in-memory copy is discarded.

The approval is per-case, per-operator, per-expiry. A bulk
"reveal everything" approval is not permitted.

## What "raw content" means here

"Raw content" is the original provider response body, the
original query text, the original memory text, the operator's
own private notes, and any other payload that the privacy
contract would otherwise forbid.

## Default behavior

- No automatic capture of raw content.
- All evaluation artifacts are redacted at the boundary
  (the row serializer, the export builder, the report
  builder).
- The blind adjudication workflow encrypts the
  provider-to-candidate mapping at rest; only the post-batch
  reconciliation step can reveal the mapping, and only to
  the operator running the reconciliation.
- The canary telemetry never includes raw content. The
  router's `RouteDecision` carries only metadata
  (provider, cohort, hash, user_id, mode, allowlisted).

## Audit trail

Every evaluation surface writes an immutable audit record
for:

- corpus construction events (additions, source classes,
  privacy-review status);
- adjudication events (open, prepare, record, status,
  export);
- canary telemetry (routing decisions, fallback events,
  kill-switch toggles);
- export events (who, when, which artifacts, what
  redactions were applied).

The audit record itself contains no raw content; it is a
metadata-only log of who did what when.

## How this contract is enforced

- The product's `Reporter` never emits a key that maps to raw
  content. The schema is closed and reviewed.
- The `CapturedRow` and `replay_row` functions in
  `fuli_product/evaluation/capture.py` and
  `fuli_product/evaluation/replay.py` reduce everything to
  fingerprints before returning.
- The `build_report` function in
  `fuli_product/evaluation/report.py` scans the output for
  forbidden patterns (`secret`, `password`, `api_key`,
  `raw_query`, `payload`) and fails the report if any are
  found.
- The `tests/fuli_product/test_evaluation.py` suite includes
  tests that confirm `CapturedRow.to_dict()` does not
  contain raw query text and that `build_report` produces
  redacted output.

## Source provenance

This contract is defined in response to the production
cutover contract's "no privacy violation" canary gate and
the user's mission statement: "Do not collect raw private
memory content without explicit operator approval."
