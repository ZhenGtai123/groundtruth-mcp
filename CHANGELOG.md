# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — 2026-08-17

First public release.

### Added

- **`Toolkit`** — four registration hooks (`loader`, `validator`, `runner`,
  `simulator`) that a project fills in, plus the seed loop, error phrasing and
  output budgeting around them. Registering only a `runner` yields both
  `replay` and `simulate`.
- **Declarative rule engine** — twelve check types (`required_fields`,
  `unique_key`, `enum`, `type`, `range`, `pattern`, `not_empty`, `ref_exists`,
  `reachable`, `no_dead_end`, `no_self_loop`, `no_cycle`) over a small path
  selector, with per-rule `code`, `severity` and `hint`. Malformed rules fail
  at load time.
- **Graph checks** — reachability, dead ends, self-loops and cycles via an
  iterative Tarjan SCC that handles graphs deeper than the recursion limit.
- **Seeded simulation** — deterministic batches, nearest-rank percentiles,
  threshold bands as `<aggregate>:<name>` expressions, a batch fingerprint, and
  an opt-in determinism check that fails the gate when a seed does not
  reproduce.
- **Read-only data access** — SQLite and PostgreSQL sources with the boundary
  enforced by the datastore, a table allowlist, post-fetch column redaction,
  statement timeouts, and `<untrusted>` fencing on everything returned.
- **MCP server** — `lint`, `replay`, `simulate`, `query` and `describe_data`,
  registered only when the toolkit has the capability, with descriptions
  composed at runtime from the project's vocabulary, real target list and live
  thresholds.
- **CLI** — the same capabilities plus `doctor`, `targets` and `schema`, with
  exit codes CI can branch on (`0` clean, `1` findings, `2` could not run).
- **`groundtruth.toml`** — one config read by both the MCP server and the CI
  gate, so the thresholds an agent optimises against cannot drift from the ones
  CI enforces.
- **Runnable example** — a config-driven checkout with a healthy flow, a
  structurally-perfect-but-wrong flow, and one carrying seven planted defects.
- **CI workflow** — lint, types, a test matrix across 3.11–3.13, a real stdio
  round trip against the MCP SDK, and the simulation gate itself.

### Notes

- The core has no third-party dependencies; the MCP SDK and psycopg are
  optional extras. A CI gate does not need the agent stack installed.
- Both `mcp.server.fastmcp.FastMCP` and `mcp.server.MCPServer` (SDK 2.x) are
  supported; the import falls back across generations.

[Unreleased]: https://github.com/ZhenGtai123/groundtruth-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ZhenGtai123/groundtruth-mcp/releases/tag/v0.1.0
