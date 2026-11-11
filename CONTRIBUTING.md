# Contributing

Open a focused issue or pull request that identifies the observed behaviour,
expected behaviour and a small reproducer. Do not include private data, credentials
or a production store. Synthetic JSON fixtures are usually enough.

The core has no third-party runtime dependencies. Changes should preserve that
boundary unless a concrete need and its maintenance cost are reviewed. Optional
adapters belong behind explicit installation extras.

Read [AGENTS.md](AGENTS.md), the [architecture](docs/ARCHITECTURE.md) and the
current tests before changing identities, schemas, budgets or shutdown. Stored
formats are public contracts. A schema change must be explicit; silently
resetting a database or replacing evidence is not a migration.

Test behaviour, especially negative and interruption boundaries. The important
properties include current-parent acceptance, immutable output binding, exact
JSON types, persistent attempt reservations and ownership held until accepted
work finishes. A passing deterministic fixture does not establish live-model
quality, independent user adoption or a production security review.

Run `python -m unittest discover -s tests -v`. For the optional adapter, install
`.[mcp]` and run `python -m unittest discover -s tests -p test_mcp.py -v`.
CI also checks an installed wheel outside the checkout on Linux and Windows.

Contributions are licensed under the repository's MIT licence. Preserve notices
and identify any material adapted from another project.
