# Atlas Reweave MCP bundle

This is the local **0.1.0-alpha.1** bundle of Atlas Reweave source **0.1.0a1**,
by Albert Tan Sin Hon and contributors. The original code remains MIT licensed.

The four tools list stages, run a registered stage, recheck work and inspect
historical receipts. Running or rechecking work can write to the local SQLite
store. The built-in stages are deterministic software fixtures. This is a public
evaluation alpha, with no hosted service, model account or API key requirement.

## Install a published bundle

Use the release asset and SHA-256 checksum from the
[versioned GitHub release](https://github.com/Albertito-AI/atlas-reweave/releases/tag/mcp-v0.1.0-alpha.1).
Confirm the official Registry record for
`io.github.Albertito-AI/atlas-reweave` before relying on directory metadata.
A committed release request alone does not establish successful publication.

An MCPB-aware client must support **manifest 0.4 and the UV runtime**. Select a
trusted existing directory for the SQLite store when the client asks for
`store_directory`. The bundle uses Python 3.12 and frozen Python dependencies.
UV downloads those dependencies on first use. The supplied fixture tools make
no model or network calls. The host's model and its own data handling are separate.

For a client that accepts a stdio command, extract the checksum-verified bundle
into a new directory and use absolute paths:

```json
{
  "command": "uv",
  "args": [
    "run", "--frozen", "--directory", "/ABSOLUTE/PATH/reweave-bundle",
    "server.py", "--store", "/ABSOLUTE/PATH/private-work/reweave.sqlite3",
    "--scope", "mcp-local"
  ]
}
```

This is a Python/UV bundle, not an npm package. Do not run a `.mcpb` URL through
`npx`. Keep the bundle and store in separate directories so an application update
does not replace the stored work. The server acquires exclusive ownership while
it is running; do not point simultaneous server processes at the same store.

See [the adapter contract](https://github.com/Albertito-AI/atlas-reweave/blob/main/docs/MCP.md) for tool effects, protocol negotiation,
trusted operator factories and the limits of local resource controls. Tool hints
describe effects; they do not enforce a security boundary. No desktop interface
or independent end-user trial is claimed by the release tests.

## Reproduce and publish

`scripts/package_mcpb.py` builds a deterministic ZIP from an explicit file list,
with fixed timestamps, modes and ordering. It rejects a release request whose
source digest does not match the reviewed inputs. `SOURCE-MANIFEST.json` identifies
every bundled source file. No store, credentials, private records or Git history
are included.

```sh
uv sync --frozen --project mcp
python scripts/package_mcpb.py --output /tmp/reweave-dist
uv run --frozen --project mcp python scripts/check_mcpb.py --dist /tmp/reweave-dist
```

The publication workflow validates and tests pull requests without publication
credentials. Publishing is restricted to protected `main` after a change to
`mcp/release-request.json`, or an explicit workflow dispatch on that branch.
It uses GitHub OIDC for the official Registry and a job-scoped GitHub token for
release assets. Existing tags or assets with different contents cause failure;
the workflow does not overwrite them. Registry readback must match the submitted
metadata and package digest.

Registry publication makes a package discoverable. It does not establish that a
particular directory lists it, that a client installs it successfully, or that
anyone has adopted it. Preserve release and downstream observations separately.

## Author and related work

[Albert Tan Sin Hon](https://alberttansinhon.com/) ·
[Research Lab](https://alberttansinhon.com/#research-lab) ·
[info@alberttansinhon.com](mailto:info@alberttansinhon.com)

The release helpers follow the independently versioned IBOL publication pattern
in [Albert's public hub](https://github.com/Albertito-AI/Albertito-AI/tree/main/scripts/ibol).
