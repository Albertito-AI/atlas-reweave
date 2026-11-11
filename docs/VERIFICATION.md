# Verification receipt

Date: 11 November 2026. Source distribution: **0.1.0a1**.

## Local installed-package evidence

The revised-core local gates ran on Python **3.12.14**, Linux, after the independent
review fixes described below. They executed the actual Python orchestration from
[verify.yml](../.github/workflows/verify.yml), rather than a separate abbreviated
installation procedure.

| Gate | Observed result |
|---|---|
| Build | Source distribution built, then wheel built from that source distribution |
| Core installation | Fresh virtual environment; wheel installed with `--no-index --no-deps` |
| Import boundary | Working directory outside checkout; `PYTHONPATH` and `PYTHONHOME` removed; module resolved from installed `site-packages` |
| Core suite | **79 tests discovered: 77 passed and two optional MCP integration tests skipped because the SDK was absent** |
| Optional adapter | Fresh environment installed the actual wheel with its MCP extra; **all three adapter tests passed** with SDK **2.3.0** |
| Dependency consistency | `pip check` passed in both environments |
| Installed entry point | `reweave --help` passed |
| Actual installed demonstration | Four fixture receipts accepted |
| Actual installed benchmark | All three callback-count pairs, output/status agreement, workload digest and input digest matched committed evidence |
| Packaged implementation | All 11 Python modules in the wheel matched the source bytes |

Those revised-core gates completed at **07:18:41 UTC**. The optional adapter
tests exercise real child processes through stdio, including automatic and legacy
protocol negotiation, all four tools, duplicate reuse, restart with changed
policy, blocked cached descendants and invalid tool arguments. Missing-SDK
startup is also checked for exit code 2, installation guidance on stderr and no
protocol output on stdout.

The installed package test collection and the adapter job overlap. Their counts
are not added together as if they represented independent tests or contributors.

## Exact implemented-source guard

At the revised-core installed check, these three material modules had the
following SHA-256 values:

| Module | SHA-256 |
|---|---|
| `engine.py` | `5cf45e42f6d0df6f5859fc213c7a9d332e023c81bdab0c1111fa1b4df13b8a94` |
| `bundle.py` | `6c3b9e852c59f5809ecefb803cf6c2765dbb173891376dc1af57957d293383c3` |
| `store.py` | `f3a7719dec23204e586e16c6a8b515235631a0aa5b637c1b2b7a11695c712f52` |

The [computational-source manifest](../evidence/source-manifest.json) records the
implementation, tests, package metadata and CI file hashes. The benchmark also
records its own example/benchmark module hashes. Documentation is maintained
separately; an archive hash can change when these documentation receipts are added.

## Concrete defects found before publication

Independent review reproduced three boundaries and the implementation was
corrected before publication:

1. **Shared dependency depth.** A subtree first visited on a short path could
   reuse a memoised acceptance on a longer path without accounting for its height.
   Memoised accepted results now retain subtree height, and every reuse checks
   the complete path against the current depth limit.
2. **Interrupted shutdown.** Interrupting a closer while it waited for an active
   callback could leave later close callers waiting for an owner that no longer
   existed. Close now separates stopped admission from active closer ownership.
   An interruption retains the Store lease and allows a subsequent close to finish.
3. **Current export bounds.** Offline verification and export now both apply the
   complete closure's current work and judgement limits, including the exact
   boundary case.

Four focused review regressions cover these cases. The shutdown regression uses
an actual timed signal in a bounded subprocess and checks lease retention,
refusal of new work, safe retry and durable completed work. That signal-specific
case is skipped where the required signal API is unavailable.

A separate Store review corrected the classification of an apparently empty
foreign SQLite database: a view-only database or one with a nonzero foreign
application ID is refused without replacing its schema or identity.

Direct pre-fix reproductions established the two engine failures. The patch
landed before the first execution of the new regression file, so no artificial
pre-fix test-suite result is claimed.

## Benchmark

For the same **16 ordered requests** over five real subprocess phases:

| Strategy | Producer calls | Verifier calls |
|---|---:|---:|
| No cache | 16 | 16 |
| Persistent cache including current policy in its key | 7 | 7 |
| Reweave | 5 | 7 |

All strategies returned identical statuses and accepted outputs. Policy change
alone caused Reweave **zero producer calls and two verifier calls**. The separate
rejected-parent case blocked a cached child and refused export; the corrupted
output case returned no output and invoked neither callback.

These are actual small-fixture callback counts, not measurements of latency,
tokens, money, GPU performance, scientific discovery, learning or adoption.
See [BENCHMARK.md](BENCHMARK.md) and the [raw receipt](../evidence/benchmark.json).

## Hosted validation

The configured public workflow builds and checks the installed package on Ubuntu
with Python 3.11/3.12 and Windows with Python 3.12, plus the optional MCP job. Its
permissions are read-only and it has no schedule. This local receipt does not
claim an unobserved hosted result. The repository's
[Actions history](https://github.com/Albertito-AI/atlas-reweave/actions) and the
source-revision-specific PR/merge receipt establish hosted outcomes when those
runs complete. Artifacts retain distribution candidates and actual CLI reports.

The first hosted run at `493bb53bdbe066db85b3b539f75ccdc66b065617`
passed both Linux jobs and MCP, but exposed Windows portability defects.
[Failed run 42978990265](https://github.com/Albertito-AI/atlas-reweave/actions/runs/42978990265)
is retained. A SQLite connection's transaction context had been mistaken for
connection closure in disposable fixtures, and the crash-test readiness gate
used a selector on a subprocess pipe. Windows rejected deletion of the still-open
files and did not support that selector operation.

The benchmark and tests now explicitly close those connections after committing
their transactions, and readiness uses a bounded reader thread with deterministic
process/pipe cleanup. The original assertions and mandatory Windows job remain.
The benchmark receipt was regenerated from an actual run with its new source
hash; all measured counts and acceptance results were unchanged. The subsequent
exact-source workflow establishes whether the Windows repair passes.

No outside contributor, independent user, live-model result or production
security review is asserted by these local and hosted software checks.
