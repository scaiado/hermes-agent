# Mnemosyne feature-gap analysis

This is a research-only document. It does NOT introduce Mnemosyne
as a dependency of the Fuli product. It evaluates Mnemosyne's
public feature set against the Fuli product roadmap and
classifies each feature into one of:

- **adopt now**
- **prototype**
- **benchmark only**
- **reject**
- **defer**

The user's mission explicitly states that Mnemosyne's
orchestration, collaborative editor, and networking layers are
out of scope for the Fuli product. Those are classified **reject**
without further analysis.

## Mnemosyne feature inventory

Mnemosyne is a local-first memory system that emphasizes typed
memories, hybrid search, consolidation, and decay.

### Storage
- LibSQL local-first storage.
- FTS5 + vector hybrid search inside the same engine.

### Memory model
- Typed memories (typed schema; per-type fields).
- Project-aware namespaces.
- Graph linking between memories.

### Lifecycle
- Consolidation (merge similar memories).
- Importance recalibration over time.
- Link decay (weaken stale links).
- Archival (move cold memories to slower storage).
- Supersede relationships (newer memory replaces older).

### Privacy
- Privacy-preserving feedback (signal without leaking payload).

### Adaptation
- Online ranking adaptation.
- Audit/event persistence (every state change is logged).

### Operational
- Local dashboard (browser-based).

## Per-feature classification

| Feature | Classification | Rationale |
|---------|---------------|-----------|
| **LibSQL local-first storage** | **benchmark only** | Fuli is already local-first with its own SQLite/embeddings stack. Adopting LibSQL would require a storage-layer rewrite. The benchmark suite can run Mnemosyne in LibSQL mode for comparison. |
| **FTS5 + vector hybrid search** | **prototype** | Fuli's current retrieval is single-method (the secondary_call wrapper). Adding FTS5 alongside the vector index and running RRF over both is a focused spike. The qualified pilot already exposes the seam. |
| **Typed memory schema** | **adopt now** | Fuli's `query_type` taxonomy is a thin typed schema. Extending it with type-specific fields (e.g. `episodic` with `event_time`, `contradiction` with `supersedes`) is a small additive change. |
| **Project-aware namespaces** | **adopt now** | Fuli already has `namespace` in the config. The product's lifecycle can support per-project namespaces with a one-line change. |
| **Graph linking between memories** | **prototype** | Fuli's current model is list-of-fingerprints. Adding a graph edge table and a small graph query path is a focused spike. The privacy contract (no raw text) is preserved by using fingerprint edges. |
| **Consolidation** | **prototype** | A background process that merges similar memories. The product's lifecycle already has a hook for background jobs; consolidation can use the captured rows from the shadow pipeline. |
| **Importance recalibration** | **defer** | Useful but not on the Phase 6/7 critical path. |
| **Link decay** | **defer** | Same. |
| **Archival** | **defer** | The product is local-first; archival to slower storage is a deployment question, not a feature gap. |
| **Supersede relationships** | **prototype** | The Fuli product's `query_type=contradiction` taxonomy already implies supersede semantics. A prototype that detects and persists supersede edges during shadow capture is small. |
| **Privacy-preserving feedback** | **adopt now** | The product's `fuli_product.evaluation.adjudication` already records blind outcomes without raw payload. The same pattern (record only fingerprints and the outcome) is the privacy-preserving feedback the mission calls for. |
| **Online ranking adaptation** | **defer** | Not on the critical path. The canary stage does not require online learning. |
| **Audit/event persistence** | **adopt now** | The product's comparison store already persists every comparison event. The audit log is in scope. |
| **Local dashboard** | **defer** | Not on the critical path. The `hermes memory` CLI is the operator surface. |
| **Orchestration layer** | **reject** | Mission directive. |
| **Collaborative editor** | **reject** | Mission directive. |
| **Networking layer** | **reject** | Mission directive. |

## Near-term prototypes prioritized

1. **Typed memory schema** (small, additive, in `fuli_product/`).
2. **Supersede/contradiction lifecycle** (uses existing
   `query_type=contradiction` taxonomy).
3. **Consolidation** as a background job on captured rows.
4. **Decay and archival** as optional config-gated processes.
5. **Privacy-safe online feedback** already in place via
   `fuli_product.evaluation.adjudication`.
6. **Project-aware namespaces** as a one-line config extension.

## Hardline: do not adopt

- Mnemosyne orchestration, collaborative editor, and networking
  layers. Mission directive.
- Mnemosyne's local dashboard in the Hermes core. The dashboard
  surface is owned upstream.

## Source provenance

This analysis is based on the public Mnemosyne feature inventory
the user mentioned in the mission and on the Fuli product's
existing scope. It is not derived from a fresh code review of
Mnemosyne's repository in this mission.
