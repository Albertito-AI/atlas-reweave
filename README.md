# Atlas Reweave

**Reuse computation. Recheck acceptance.**

Atlas Reweave is a small, local execution library for AI-agent builders. It saves
the output of a trusted computation separately from the decision that accepts
it. A changed checker or policy can assess the saved output again without
repeating generation. A downstream stage cannot reuse its result while a
supporting parent is rejected, stale, missing or corrupt.

Version **0.1.0a1** is an experimental source distribution. The core uses only the
Python standard library. An optional adapter exposes four tools through the
official MCP Python SDK. No model account, API key, GPU or paid service is needed
to run the examples or tests.

## What it does

- Separates immutable **work identity** from versioned **judgement identity**.
- Revalidates the dependency closure before releasing any accepted output.
- Commits completed generation before checking, so a checker failure or process
  exit can be followed by a new check without regenerating that stored result.
- Streams an iterator through a bounded pending window and suppresses concurrent
  duplicate generation within one engine.
- Reserves producer attempts durably before calling a callback. Failed and
  interrupted attempts remain counted after restart.
- Exports the current accepted dependency closure as a portable, hash-checked
  audit bundle. Offline verification checks consistency, not semantic truth or
  authorship.

This is an original implementation and a focused combination of established
ideas. It does not claim to invent caching, dataflow, durable execution, asset
checks or verification. See the [prior-art comparison](docs/PRIOR_ART.md).

## Try it

Python **3.11 or later** is required. Install from this repository; this snapshot
does not assert a PyPI publication or a GitHub tagged release.

```bash
git clone https://github.com/Albertito-AI/atlas-reweave.git
cd atlas-reweave
python -m pip install .
python -m reweave demo --store ./demo.sqlite3
python -m reweave benchmark --output ./benchmark.json
```

The demonstration uses exact rational arithmetic and invented source text. It is
a deterministic software fixture, with no hidden model call. A second solver
implementation, a separate substitution checker and a literal-source checker
provide concrete acceptance mechanisms without pretending that a fixture is a
live AI agent.

### Change acceptance without repeating generation

```python
from reweave import Engine
from reweave.examples import make_stages, RATIONAL_EXAMPLE

with Engine("work.sqlite3", make_stages(), scope="tutorial") as engine:
    original = engine.run("linear.solve", RATIONAL_EXAMPLE)
    assert original.status == "accepted"

with Engine("work.sqlite3", make_stages(integer_only=True), scope="tutorial") as engine:
    checked = engine.revalidate(original.work_key)
    assert checked.status == "rejected"
    assert engine.stats()["producer_calls"] == 0
```

The rational candidate still satisfies its equations. The new policy requires
integer solutions, so the acceptance decision changes. This demonstrates a
policy change, not a mathematical discovery or an improvement in a model.

## Register your own agent work

```python
from reweave import Engine, Stage, Verdict

def produce(value, parents):
    return {"count": len(value["items"])}

def verify(value, output, parents):
    expected = len(value["items"])
    valid = type(output.get("count")) is int and output["count"] == expected
    return Verdict(valid, "Checked the item count against the supplied input")

stage = Stage(
    name="items.count",
    producer_revision="count-v1",
    producer=produce,
    verifier_revision="count-check-v1",
    verifier=verify,
    description="Count the items in an explicitly supplied JSON list.",
    input_example={"items": ["a", "b"]},
)

with Engine("counts.sqlite3", [stage], scope="my-project") as engine:
    for receipt in engine.stream("items.count", [{"items": ["a", "b"]}] * 3):
        print(receipt.to_dict())
```

Callbacks receive JSON copies of the input and ordered parent **outputs**. They
must validate their input and be pure with respect to the identity you declare.
Include source snapshots and explicit code, prompt, model and dependency
versions. `fingerprint_files()` can hash a named file manifest; it cannot discover
hidden dependencies or recognise that an external website changed.

Producers never receive parent judgement identifiers, because a policy-only
change must not alter an otherwise reusable producer input. Only accepted
receipts include an `output` field. Inspection returns historical metadata and
does not imply current acceptance.

## Agent interface

```bash
python -m pip install '.[mcp]'
python -m reweave serve --store ./agent-work.sqlite3 --scope my-agent
```

The optional stdio server exposes `list_stages`, `run_registered_stage`,
`recheck_work` and `inspect_receipt`. It accepts registered operations and JSON
data. Operator-selected factories are configured at server startup; tool calls
cannot supply a module, shell command, file path or callback to execute.

See [MCP setup and actual interoperability checks](docs/MCP.md).

## Integration example: local evidence replay

[Evidence Replay in Colibri Atlas](https://github.com/Albertito-AI/colibri-atlas/tree/main/examples/atlas_evidence), public prototype **0.1.0a1**,
checks candidate quotations against supplied source snapshots. Its optional
Reweave adapter rechecks a saved capture under changed sources or quotation rules
and blocks dependent report export when the evidence is rejected. It also exports
a RegNav Source Watch manifest with automated relations labelled `context`.

Archived related software release: [Evidence Replay 0.1.0a1 (public prototype)](https://doi.org/10.5281/zenodo.24206408).

The [validation record](https://github.com/Albertito-AI/colibri-atlas/blob/main/examples/atlas_evidence/VALIDATION.md) reports **48 tests passed, zero skips**,
including Colibri's actual HTTP gateway with a scripted engine double.
Real-model quality remains untested. Matching a quotation establishes literal
agreement with supplied text, not semantic support for the candidate claim.

## Evidence and boundaries

The [benchmark](docs/BENCHMARK.md) compares actual producer and verifier calls on
the same small deterministic workload. The [verification receipt](docs/VERIFICATION.md)
records the tested source, installation checks and observed results. Counts are
not estimates of tokens, money, GPU throughput, model learning or user adoption.

Reweave is designed for one trusted operator and cooperating local processes on
a local filesystem. It is not a sandbox for hostile callbacks or an exactly-once
external-action service. A crash before generation is durably committed can
require another attempt. A hung callback can hold shutdown open. Logical record
and concurrency limits are not hard CPU, RAM or filesystem quotas.

Records and bundles can contain the supplied data in plaintext. Keep secrets out
of inputs/configuration and review an export before sharing it. Hashes detect
inconsistent bytes; a writer able to replace both records and hashes can create
different consistent evidence. See [architecture](docs/ARCHITECTURE.md) and
[security boundaries](SECURITY.md).

## Development

```bash
python -m pip install .
python -m unittest discover -s tests -v
```

Core tests use the standard library. MCP tests skip when the optional SDK is
absent and run separately in the optional-adapter CI job. CI builds an sdist and
wheel, then installs into a fresh environment and runs tests outside the source
checkout. It has read-only repository permissions and no schedule.

The source is [MIT-licensed](LICENSE). [Contribution guidance](CONTRIBUTING.md)
and [citation metadata](CITATION.cff) are included. Agents are welcome:
[start with the public participation guide](docs/AGENT_PARTICIPATION.md).
