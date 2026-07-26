# Hindsight feature-gap analysis

This is a research-only document. It does NOT introduce Hindsight
as a dependency of the Fuli product. It evaluates Hindsight's
public feature set against the Fuli product roadmap and
classifies each feature into one of:

- **adopt now**: ready to bring into Fuli as a small, well-
  understood unit.
- **prototype**: worth a focused spike before adoption.
- **benchmark only**: run against Fuli on the benchmark harness,
  do not adopt the implementation.
- **reject**: explicitly out of scope for the Fuli product.
- **defer**: useful but not on the Phase 6/7 critical path.

The classifications are evidence-based recommendations, not
commitments. Final adoption decisions follow the production
cutover contract and require explicit operator approval.

## Hindsight feature inventory

Hindsight is a memory framework that separates memory into
distinct banks and offers explicit retain/recall/reflect APIs.

### Banks
- **World facts**: stable facts about the world.
- **Experiences**: episodic, time-anchored events.
- **Mental models**: derived, higher-order conclusions about the
  user or the world.

The bank split is a clean taxonomic separation that the Fuli
product currently lacks. Fuli's `query_type` taxonomy (profile /
preference / project / episodic / exact / semantic / recent /
contradiction) mixes facts, experiences, and mental models.

### Retain / recall / reflect API separation
- **Retain** = explicit write.
- **Recall** = query-time retrieval.
- **Reflect** = offline synthesis of mental models from past
  experiences.

The Fuli product today mixes write paths (mirror mode) with
read paths (compare/recall) and has no equivalent of `reflect`.

### Hybrid retrieval
- Semantic + keyword (BM25) + graph + temporal parallel.
- Reciprocal-rank fusion (RRF) across retrieval methods.
- Optional cross-encoder reranking.

The Fuli product's qualified pilot currently supports one
retrieval mode per call (typically "hybrid" inside the
secondary_call wrapper, but not multiple parallel retrievers
fused via RRF).

### Reflection
- Background generation of mental models from accumulated
  experiences.
- The reflection loop is decoupled from the live read path.

The Fuli product has no background reflection process.

### Deployment
- Embedded Python (in-process).
- Docker/server.
- Operational UI (browser-based memory browser).

Fuli-Memory-Core is currently embedded-only; no Docker/server
mode, no operational UI.

### Benchmark methodology
- Hindsight ships a benchmark suite (LOCOMO-style long-context
  memory evaluation, multi-hop QA, temporal reasoning).

Fuli's benchmark surface is the one designed in Phase 6
(`fuli_product/evaluation/`, `reports/product/memory-provider-benchmark`).

## Per-feature classification

| Feature | Classification | Rationale |
|---------|---------------|-----------|
| **Banks (world / experiences / mental models)** | **prototype** | The bank split is a clean separation but adopting it would require a Fuli schema migration. Worth a spike that produces a prototype bank-aware schema with backward compatibility. |
| **Retain/Recall/Reflect API separation** | **adopt now** | The Fuli product's interface can adopt the explicit tri-API split without touching the storage layer. The qualified `pilot.shadow_pilot` and the product's lifecycle already separate retain (write) from recall (compare). Adding `reflect` is a small additive layer. |
| **Hybrid retrieval fusion (RRF)** | **prototype** | Fuli's secondary_call wrapper is currently a single function. RRF would require running multiple retrieval methods in parallel and merging. The retrieval adapter (Phase 6 Stage 7) can host RRF as a prototype. |
| **Temporal retrieval** | **prototype** | The Fuli product's `query_type` taxonomy includes `recent` and `episodic`; an explicit temporal retrieval signal (date-bounded recall) is a useful prototype. |
| **Reflection as offline background process** | **prototype** | A scheduled reflection job that ingests captured rows and produces mental-model entries. The existing `pilot.sampling` provides a determinism substrate. The reflection loop must be explicitly opt-in. |
| **Embedded Python mode (reference behavior)** | **benchmark only** | Fuli is already embedded; the comparison we need is whether Fuli's behavior is competitive with Hindsight's embedded mode on the same queries. This is the benchmark suite's job. |
| **Docker/server mode** | **reject** | The user's mission statement explicitly forbids Docker as a mandatory Fuli dependency. Hindsight's server mode is a deployment topology, not a feature Fuli needs. |
| **Operational UI** | **defer** | Not on the Phase 6/7 critical path. The `hermes memory` CLI is the operator surface for now. |
| **Benchmark methodology (LOCOMO-style)** | **adopt now** | The Phase 6 benchmark harness adopts a similar methodology. The Hindsight benchmark corpus can be used as one of the benchmark inputs. |
| **Mental-model generation prompts** | **defer** | Useful but only after the reflect API is wired and the operator is comfortable with offline synthesis. |

## Near-term prototypes prioritized

1. **Explicit retain/recall/reflect interfaces** in
   `fuli_product/` (no Fuli-Memory-Core change required).
2. **Hybrid retrieval fusion (RRF)** in the Phase 6 evaluation
   adapter.
3. **Temporal retrieval** as a query type with a date-bounded
   recall wrapper.
4. **Reflection as an offline background process** gated by a
   config flag; runs on captured rows from the shadow pipeline.
5. **Benchmark harness** at `reports/product/memory-provider-benchmark`.

## Hardline: do not adopt

- Hindsight Docker/server mode. The product is local-first.
- Hindsight operational UI. Hermes CLI is the operator surface.
- Hindsight's full reflection prompt templates. They are a
  starting point but are not a drop-in for Fuli's privacy
  contract.

## Source provenance

This analysis is based on the public Hindsight feature inventory
the user mentioned in the mission and on the Fuli product's
existing scope. It is not derived from a fresh code review of
Hindsight's repository in this mission.

## Post-evidence experiment plan

After the Fuli 1% canary is approved and the qualification
evidence is complete, the following isolated experiment
exercises Hindsight's embedded mode against the same
representative corpus. **It is a research comparison, not a
production candidate.**

- **Mode**: Hindsight embedded Python only (no Docker, no
  server). The Hindsight package is installed in a separate
  virtual environment; the existing Fuli runtime is
  untouched.
- **Corpus**: the same `reports/product/fuli-evaluation-corpus.json`
  used for the Fuli evaluation. No new corpora.
- **Namespace**: a fresh, isolated Hindsight namespace; no
  cross-contamination with the Fuli evaluation namespace.
- **Benchmark dimensions**:
  - retain/recall/reflect API surface — exercised through
    Hindsight's Python bindings.
  - Hybrid retrieval fusion (semantic + keyword + graph +
    temporal) — Hindsight's built-in parallel retrievers +
    RRF; measured against Fuli's single-method retrieval.
  - Temporal retrieval — Hindsight's date-bounded recall
    versus Fuli's `recent` / `episodic` query types.
  - Reflection — Hindsight's background mental-model
    generation; Fuli has no equivalent, so the comparison
    is qualitative (does Fuli need a reflect API?).
  - Resource footprint — RSS delta, peak memory, time to
    retain/recall 1000 cases.
  - Quality — same blind adjudication workflow, same
    reviewer, same outcome codes. The 100-adjudication
    minimum applies; the Hindsight arm produces 100
    blind-adjudicated cases; the non-inferiority calculator
    is reused unchanged.
- **Reporting**: results land in
  `reports/product/hindsight-experiment.json` and
  `docs/research/hindsight-experiment-results.md`.
- **Constraint**: the Hindsight package is installed in a
  separate venv; the Fuli runtime venv and the live
  profiles are untouched. The Fuli product is not modified
  to consume Hindsight.
