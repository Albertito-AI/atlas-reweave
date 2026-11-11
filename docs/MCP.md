# Optional MCP adapter

The adapter exposes four narrow operations over stdio. It uses one long-lived
Engine, a fixed stage registry and an operator-selected local SQLite store.
It does not call a model or expose shell execution, arbitrary imports, a database
path selector or a way to change acceptance policy through a tool call.

## Install and start

Use Python 3.11 or later and install the optional dependency:

```bash
python -m pip install '.[mcp]'
python -m reweave.mcp_server --store /private/work/reweave.sqlite --scope my-local-work
```

For a built wheel, install `atlas_reweave-0.1.0a1-py3-none-any.whl[mcp]` using its
actual local path. The extra pins the official Python SDK to `mcp==2.3.0`; it also
installs the SDK's declared dependencies. The core package has no third-party
runtime dependency. No SDK CLI extra is needed for this module entrypoint.

The default registry contains `linear.solve`, `linear.aggregate` and
`text.excerpt`. They are deterministic software fixtures. The solver accepts a
nonsingular 2-by-2 rational system and checks the candidate by exact substitution.
The excerpt binds a literal span to supplied text and its digest; it does not
authenticate the source or establish the truth of its contents.

`--integer-only` starts the fixture registry with a stricter solution-acceptance
policy. Restarting with that flag can recheck existing generated work. A cached
aggregate is blocked if one of its parents is now rejected.

## Four tools

| Tool | Arguments | Result and effects |
| --- | --- | --- |
| `list_stages` | `{}` | `{"stages": [...]}` with names, descriptions, input examples and producer/verifier revisions; read-only |
| `run_registered_stage` | `{"request": {"stage_name": "...", "input": ..., "parents": []}}` | A current receipt; may generate work or write a judgement |
| `recheck_work` | `{"request": {"work_key": "<64 lowercase hex characters>"}}` | A current receipt for stored work and its dependency closure; may write judgements |
| `inspect_receipt` | `{"request": {"work_key": "<64 lowercase hex characters>"}}` | Historical metadata only, with `current_acceptance: "not_evaluated"`; no output, revalidation or write |

The substantive request models forbid extra fields. Stage names are resolved
against the fixed registry. Parents are ordered, unique work keys, subject to
the Engine's parent-count limit. The registry's producers still validate their
own input meaning; an object being valid JSON is not enough to make it a valid
linear system or source span.

Read `result.is_error` before reading a successful result. A malformed tool
envelope or unknown stage is a tool error. A known work item that is rejected,
blocked, stale, corrupt, over budget or fails execution returns a receipt with
that status. Such receipts contain no `output` field. A rejected result is not
converted into successful output merely because the MCP exchange succeeded.

## A real client

Use the same installed environment for this client and its subprocess:

```python
import anyio
import sys
from mcp import Client, StdioServerParameters

async def main():
    process = StdioServerParameters(
        command=sys.executable,
        args=["-m", "reweave.mcp_server", "--store", "/private/work/reweave.sqlite"],
    )
    with anyio.fail_after(20):
        async with Client(process, read_timeout_seconds=5, cache=None) as client:
            tools = await client.list_tools()
            print([tool.name for tool in tools.tools])
            result = await client.call_tool("run_registered_stage", {
                "request": {
                    "stage_name": "linear.solve",
                    "input": {"matrix": [[2, 1], [1, -1]], "rhs": [5, 1]},
                    "parents": [],
                }
            })
            if result.is_error:
                raise RuntimeError(result.content)
            print(result.structured_content)

anyio.run(main)
```

SDK v2's Python fields use `structured_content`, `is_error`, `input_schema` and
`output_schema`. MCP wire messages use their protocol aliases. The high-level
client handles process lifetime and protocol negotiation. `mode="legacy"`
explicitly exercises the older handshake; the adapter tests cover both automatic
and legacy negotiation over actual subprocess pipes.

## Operator-controlled registry factory

An operator can launch a different trusted registry:

```bash
python -m reweave.mcp_server --store /private/work/reweave.sqlite \
  --scope my-local-work --factory my_project.registry:make_stages
```

The function receives no arguments and returns Stage objects. This is a trusted
Python import at process startup, so normal Python import side effects apply.
The operator owns its code, explicit revision identities and policies. It cannot
be combined with `--integer-only`, which belongs to the built-in fixtures. No MCP
argument can select a factory, import path, executable, store path or policy.
Send startup diagnostics to stderr; stdout belongs to the stdio protocol.

## Concurrency and limits

The SDK executes ordinary `def` tools in worker threads. They call the same
thread-safe Engine; its Store serialises transactions. The adapter does not
create a fresh Engine per call or reset its budgets. Producer attempts and stored
record caps are durable. The Engine's session counters describe this Engine
lifetime. The standard Limits apply unless an embedding operator explicitly
constructs an Engine with different limits before calling `create_server(engine)`.

A client timeout does not forcibly stop a trusted synchronous Python callback.
Likewise, field-size validation happens after the SDK parses a request. Neither
the adapter nor its timeouts establish hard operating-system memory, CPU or disk
limits. Tool annotations describe effects; they are not an authorisation system.
The two execution tools conservatively advertise `openWorldHint: true` because
an operator can register callbacks whose external dependencies the adapter cannot
inspect. The built-in fixtures perform no network or model calls. This hint does
not grant network access, make an external effect pure or enforce the callback
contract. A future model-backed callback would need an explicit model/configuration
identity and a clear statement that the first recorded candidate is retained;
it must not be described as deterministic solely because its result is cached.
Keep the stdio process within the operator's intended local trust boundary.

## Verification and SDK sources

`tests/test_mcp.py` launches the installed module from a temporary directory,
discovers the four tools, checks duplicate reuse and errors, then restarts the
process against the same store to exercise policy rejection of a cached child.
It also exercises the legacy protocol. The test skips only if the optional SDK is
absent; an installed unsupported SDK version fails explicitly.

The API was checked against official SDK tag `v2.3.0`, commit
`2118f14f8a19bc158d8a1cf90af58d85d187f849`:

- [Server and tool registration](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/server/mcpserver/server.py)
- [High-level Client](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/client/client.py)
- [Subprocess transport](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/client/stdio.py)
- [Worker-thread dispatch and argument validation](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/src/mcp/server/mcpserver/utilities/func_metadata.py)
- [Cancellation semantics](https://github.com/modelcontextprotocol/python-sdk/blob/2118f14f8a19bc158d8a1cf90af58d85d187f849/docs/handlers/cancellation.md)

Passing these tests demonstrates the tested SDK exchanges, not universal
compatibility with every host application or correctness of arbitrary registered
callbacks.
