# Deterministic callback-count demonstration

The [recorded report](../evidence/benchmark.json) measures actual producer and
verifier invocations on **16 deliberately selected requests per strategy**. It
demonstrates a specific difference between invalidating generation when policy
changes and retaining generated work for a new judgement. It is not a general
speed, cost or model-efficiency benchmark.

## Observed results

| Strategy | Producer calls | Verifier calls |
| --- | ---: | ---: |
| No cache | 16 | 16 |
| Persistent cache whose identity includes current policy | 7 | 7 |
| Reweave's separate work and judgement identities | 5 | 7 |

All three strategies returned the same statuses and accepted outputs for the
same ordered requests. Counts come from wrappers around the callbacks actually
invoked, and the report retains each callback event. They are not estimates
computed from expected cache-hit ratios.

The exact report records the Python version and SHA-256 hashes of the example
and benchmark modules. The values above were observed using Python 3.12.14 on
Linux. Reproduction validates the counts and outcomes, not an assumption that
arbitrary future source versions will have identical work keys.

## Workload and process boundaries

Every phase starts a **new Python subprocess**. Each strategy retains its own
local state directory between those processes. The Reweave strategy reopens its
SQLite store; the policy-invalidated baseline reloads a JSON cache. The no-cache
baseline performs both callbacks for every request.

The initial system is

```text
2x + y = 5
 x - y = 1
```

with exact solution `(2, 1)`. A second right-hand side `(1, 0)` has exact solution
`(1/3, 1/3)`. The stricter policy accepts only solutions whose denominators are
one. The rational answer still satisfies its equations; the stricter policy
rejects it as unsuitable for that chosen acceptance rule.

| Phase | Requests | No-cache producer/verifier | Policy-invalidated producer/verifier | Reweave producer/verifier |
| --- | ---: | ---: | ---: | ---: |
| Initial requests with duplicates | 5 | 5 / 5 | 2 / 2 | 2 / 2 |
| Reopen state in a new process | 2 | 2 / 2 | 0 / 0 | 0 / 0 |
| Change to integer-only acceptance | 4 | 4 / 4 | 2 / 2 | 0 / 2 |
| Change producer implementation and revision | 2 | 2 / 2 | 2 / 2 | 2 / 2 |
| Change the equation input, with a duplicate | 3 | 3 / 3 | 1 / 1 | 1 / 1 |

The producer-change phase selects an actual second solver implementation:
Gaussian elimination replaces Cramer's rule, with a different explicit producer
revision. The independent substitution checker stays the same. The final phase
separately changes the right-hand side to `(7, 2)`, whose solution is `(3, 1)`.
This distinguishes input invalidation from producer invalidation.

The comparison cache stores complete decided results, including rejections, and
persists across processes. Its cache identity includes work input, producer
identity, verifier identity and policy. A policy change therefore requires both
callbacks again. Reweave instead reuses the durable candidate and invokes only
the current verifier in that phase. This baseline is fully specified in
[benchmark.py](../src/reweave/benchmark.py); it is not a claim about the behaviour
of every other cache or workflow system.

## Separate boundary demonstrations

The report keeps these checks separate from the 16-request comparison:

- **Rejected parent and cached child:** an aggregate first accepts using the
  integer and rational solver outputs. After the integer-only policy is applied,
  the stored child returns `blocked`, contains no output and cannot be exported.
  The measured replay made zero producer calls and two parent-verifier calls.
- **Corrupt output:** after closing a disposable fixture store, the benchmark
  alters one stored output without updating its output or record digest.
  Revalidation returns `corrupt`, contains no output and makes zero producer or
  verifier calls. It does not silently replace the original evidence.

These establish the recorded behaviours for these cases. They do not prove
resistance to an adversary who can rewrite all database contents and hashes.
Actual interruption, concurrent calls, resource limits and bundle validation
have their own core tests.

## Reproduce

After installing the package:

```bash
python -m reweave.benchmark --output evidence/benchmark.json
```

The public function `reweave.benchmark.run_benchmark()` returns the same report
as a JSON-compatible dictionary. It creates disposable stores, runs fixed Python
worker commands, compares actual outcomes and raises if the strategies disagree
or a boundary check fails. No request can supply executable code to those workers.

## Other deterministic examples

`make_stages(integer_only=False)` also registers `text.excerpt`. It produces a
literal substring, source identifier, UTF-8 source digest and Unicode code-point
offsets. Its checker validates those against the supplied source. A changed
source digest or invented quotation is rejected. The demonstration text is
explicitly invented. This can illustrate provenance handling, but a source
identifier and matching digest do not authenticate a publication or establish
the truth, legal authority or relevance of its contents.

No model calls, token counts, expenditure, latency, weight updates, user outcomes
or scientific discoveries were measured. The small callback-count differences
must not be extrapolated to those quantities.
