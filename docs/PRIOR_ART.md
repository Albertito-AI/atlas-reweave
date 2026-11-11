# Prior art and the scope of this contribution

Atlas Reweave is an original, small implementation combining several established
ideas for a particular agent-tool problem: keeping an expensive computation while
reassessing its acceptability, and carrying that reassessment through dependants.
No exhaustive novelty search or world-first claim is made.

## Relevant existing work

| Primary source | Established idea | Relationship to Reweave |
|---|---|---|
| Jeffrey Dean and Sanjay Ghemawat, [MapReduce: Simplified Data Processing on Large Clusters](https://research.google/pubs/mapreduce-simplified-data-processing-on-large-clusters/), OSDI 2004 | A data-parallel programming model with managed execution and failure handling | Motivation for explicit dataflow and recovery. Reweave is a small local library, not a replacement cluster system. |
| [Ray Data internals](https://docs.ray.io/en/latest/data/internals.html) and its [serialization documentation](https://docs.ray.io/en/latest/ray-core/objects/serialization.html) | Streaming execution, backpressure, object management and specific zero-copy mechanisms | Reweave uses a bounded Python submission window. It does not claim Ray's distributed execution, zero-copy transfers or GPU behaviour. |
| [LangGraph persistence](https://docs.langchain.com/oss/python/langgraph/persistence) | Checkpointed graph state and recovery/resumption | Durable agent execution is prior art. Reweave focuses on immutable completed work and separately versioned acceptance. |
| [Prefect caching documentation](https://github.com/PrefectHQ/prefect/blob/main/docs/v3/concepts/caching.mdx) | Cache policies, explicit cache keys and persisted task results | Input/code-based cache identity is established. Reweave keeps acceptance context out of the work key and puts it into a separate judgement graph. |
| [Dagster asset checks](https://dagster.io/docs/guides/test/asset-checks) | Explicit checks of materialised assets | Checking an existing output is established. Reweave combines that idea with per-work identities, current-parent judgement binding and accepted-only output/export. |
| Zexun Wang, [Proof-Carrying Agent Actions](https://arxiv.org/abs/2606.04104), and [verified-loop](https://github.com/nancyzwang/verified-loop) | Agent actions gated by explicit evidence and verification | Verification-gated agents are prior art. Reweave's local hash-checked receipts are not general proofs or independently attested action authorisations. |
| [MCP Python SDK, exact v2.3.0 source](https://github.com/modelcontextprotocol/python-sdk/tree/2118f14f8a19bc158d8a1cf90af58d85d187f849) | Standard tool discovery and calls over supported transports | The optional adapter uses the official SDK. Reweave does not invent a replacement protocol. |

These links were consulted on 11 November 2026. Moving documentation describes
the named projects; the MCP implementation and transport checks are tied to the
specific SDK release. Reweave's implementation uses standard-library code written
for this repository, rather than copying any of these systems' implementations.

## What this implementation contributes

The concrete deliverable is a small, installable execution layer with:

1. A work key binding the declared input, producer contract and immutable parent
   outputs, and a separate judgement key binding the checked work, output digest,
   current verifier/policy and current parent judgements.
2. A current dependency-closure check before cached output is released.
3. Completed-generation persistence before checking, plus durable attempted-work
   accounting and tested process-death recovery.
4. Bounded iterator admission and duplicate suppression, portable audit bundles,
   a Python API and a four-tool MCP adapter.
5. Reproducible negative tests and measured callback traces that identify where
   reuse occurred and where acceptance prevented it.

This combination is intended to be useful to agent builders who want a local,
inspectable component. It is not evidence that the approach is unprecedented,
faster than production orchestration systems or useful in every workload.

## Use of the supplied design brief

The owner's supplied dataflow brief motivated streaming, feedback, curation and
checkpointing. Reweave translates those themes into bounded local agent work,
explicit acceptance, retained rejected evidence and restartable checks.

The brief is not treated as a validated technical specification. Its biographical
claims and proposed storage, GPU-utilisation and convergence percentages are not
adopted as facts or measured results. No cloud cluster, training system or paid
infrastructure was provisioned. The private supplied PDF is not redistributed.

The measured result is deliberately narrower: actual function calls on the same
small deterministic fixture workload. [Benchmark method and limitations](BENCHMARK.md)
explain exactly what can be inferred from those observations.
