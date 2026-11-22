# Security and evidence boundaries

Atlas Reweave 0.1.0a1 is an experimental local library for trusted callbacks and
one operator. It has not undergone an independent security assessment.

## Implemented boundaries

Tool calls select a stage from a fixed registry. They cannot supply callable
source, an executable, a module path or a filesystem destination. An operator
may select a Python factory when launching the MCP server; that is trusted code.

The engine copies and validates JSON, separates work identity from judgement
identity, checks parent acceptance before exposing parent outputs, and suppresses
outputs for nonaccepted receipts. Store reads validate structure and hashes.
An exclusive sidecar SQLite transaction prevents another cooperating Engine
from owning the same canonical store path. Shutdown stops admission and drains
admitted operations before releasing ownership.

Producer attempts are reserved durably before invocation. Counts survive failed
callbacks and process interruption. Logical records, encoded bytes, graph depth,
parent count, active workers and pending stream work have explicit limits.

## Outside the boundary

Callbacks execute with the Python process's permissions. They can ignore this
contract, access the network, alter files or hang. Use a separate operating-system
or container boundary when executing untrusted code. MCP tool annotations are
descriptive hints, not permission enforcement.

Budgets are application limits. They do not cap all Python allocations, SQLite
journal overhead, filesystem blocks, CPU time or external child processes. A
timeout in an MCP client does not forcibly stop a Python callback. Close waits
for admitted work; it does not release the lease while that work can still write.

The lease assumes cooperating users and local filesystem locking. It does not
protect against a local writer replacing the database or its sidecar, hard-link
aliases, compromised callbacks or network-filesystem locking failures. Back up
only after all engines are closed, and preserve the database as one coherent
SQLite file rather than copying a live file mid-transaction.

Hashes are integrity checks, not signatures. Locally writable records and a
bundle with recomputed hashes are not independently attested evidence. Offline
bundle verification does not rerun a checker, determine current external policy
or establish the truth/authenticity of a source.

All supplied data is stored locally in plaintext. Never put API keys in an
identity manifest. Source snapshots may contain confidential or copyrighted
material; sharing a bundle is a separate data-disclosure decision. The included
source excerpt fixture uses invented text and proves only literal consistency.

## External assessments and access

A third-party directory entry does not establish maintainer approval or an
independent security audit. Treat a scanner's findings as claims about its
inspected snapshot and check them against the source and reproducible behaviour.
The [MCP annotation evidence](evidence/mcp-tool-annotations.json), for example,
records protocol metadata without claiming that hints enforce callback safety.

An unsolicited listing does not authorise app installation, repository access or
publication of an ownership challenge. Assess an integration's exact permissions,
repository scope, data handling, terms and practical benefit before connecting
it. Public discovery requires no disclosure of private work stores, credentials
or unpublished research.

## Reporting

Use the repository's private vulnerability-reporting facility if it is available
in your GitHub interface. Otherwise open a minimal issue asking for a private
reporting route, without posting exploit details, credentials or affected data.
No monitored security email address or response-time commitment is asserted.
