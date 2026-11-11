# Atlas Reweave contribution instructions

Build a small, testable execution library for trusted agent callbacks. Preserve the
separation between immutable computation and versioned acceptance. A cached child
must never bypass the current acceptance of its parents.

Use the standard library for the core. MCP is an optional adapter. Do not add
network access, model calls, telemetry, subprocess execution from tool inputs or
paid services. Keep examples explicitly deterministic software fixtures.

Changes to stored schemas require an explicit version decision. Do not silently
reset a store or replace immutable evidence. Test actual interruption, concurrency
and invalidation boundaries. Package tests must work outside the source checkout.

Do not claim general novelty, exactly-once external effects, hard operating-system
quotas, malicious-writer tamper resistance or scientific/learning outcomes.
Document measured results and limitations precisely. Use British English.

Agree file ownership while contributors are working concurrently. Changes need
a review of the actual diff and relevant behaviour before merge or publication.
See docs/AGENT_PARTICIPATION.md for the public contribution process.
