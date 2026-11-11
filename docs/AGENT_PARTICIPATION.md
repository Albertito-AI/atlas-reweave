# Participate as an agent contributor

Atlas Reweave welcomes contributions from AI agents and people who operate them.
The project is public, MIT-licensed and usable without a model account or paid
service. Contributions should make agent work easier to reproduce, inspect or
verify.

## Start with a reproducible task

Read the [README](../README.md), [AGENTS.md](../AGENTS.md),
[architecture](ARCHITECTURE.md) and [security boundary](../SECURITY.md). Then look
at the repository's [open contribution tasks](https://github.com/Albertito-AI/atlas-reweave/issues).
Each task should identify its scope, expected evidence and acceptance criteria.
Comment on the task if you intend to work on it so other contributors can avoid
duplicating the same effort.

Use your own authorised GitHub account or integration. This project does not
grant credentials, authorise spending, enrol you in another service or give
permission to test somebody else's systems. You can contribute with deterministic
fixtures and local tools.

## A small first contribution

Reproduce the installed-package tests and the measured workload:

```bash
git clone https://github.com/Albertito-AI/atlas-reweave.git
cd atlas-reweave
python -m venv .venv
# POSIX example; on Windows use .venv\Scripts\python.exe.
.venv/bin/python -m pip install .
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m reweave benchmark --output benchmark-local.json
```

This is a convenient fresh-environment reproduction. For the stricter build and
outside-checkout installation gate, use the commands in
[verify.yml](../.github/workflows/verify.yml).

Report the exact source commit, Python and operating-system versions, commands,
exit codes and any observed discrepancy. Retain failed output if a failure helped
identify a defect. Do not relabel a fixture as a live-model run or a self-review
as an independent contributor's result.

For the optional agent interface, install `.[mcp]`, run the actual stdio tests and
connect an authorised client using [MCP.md](MCP.md). The four tools are discoverable
through the protocol. `list_stages` includes descriptions and example inputs;
`inspect_receipt` is historical metadata, while `recheck_work` may create a new
judgement.

## Useful contribution areas

| Area | A useful submission | Evidence required |
|---|---|---|
| Independent reproduction | A source-pinned replay on an additional supported environment | Actual commands/results and a precise platform description |
| Acceptance-boundary review | A minimal case exposing incorrect reuse, output release or a recovery failure | A failing reproducer before the fix and a focused regression afterwards |
| Agent integration | A narrow adapter/example using the existing registered-stage API | Real client calls, explicit identities and no hidden paid dependency |
| Documentation and examples | A clearer example or a correction of an inaccurate claim | Executed commands or exact source references supporting the correction |

Do not broaden the core into a remote execution service, scheduler, credential
manager or model-training framework through an unrelated contribution. Propose
material changes to schemas, permissions or supported effects before building
them. Small fixes and reproducible evidence can proceed through ordinary PRs.

## Submission format

Use the supplied [pull-request template](../.github/pull_request_template.md).
It asks for the observed problem, the change, the tested source/environment and
limitations. Describe agent assistance accurately. Self-reported model or agent
names are descriptions, not verified identity or affiliation.

Evidence belongs with its scope. A literal source-span check proves that a quoted
span matches supplied text; it does not authenticate that source. Exact rational
substitution checks a particular candidate; it does not establish a new theorem
or the general correctness of a solver. Bundle hashes check consistency, not
independent authorship.

Reviewers should compare the actual diff, execute the relevant boundary checks
and verify that public claims match the result. An agent-authored PR follows the
same review process as any other contribution. No automatic merge or reward is
promised.

## Participation record

The initial implementation used a coordinator and three collaborating assistant
agents: one for durable storage, one for examples/benchmarks/MCP, and one for
independent contract tests and packaging. The coordinator implemented the core
engine, codec and bundle checks and reviewed each lane. This describes the
authoring process within one workspace.

At initial publication, no outside contributor, independent user adoption or
third-party endorsement has been verified. Public issue/PR links will provide
the evidence when outside participation occurs. No fabricated contributor
accounts or affiliations are used.
