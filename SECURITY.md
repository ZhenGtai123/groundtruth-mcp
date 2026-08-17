# Security policy

## Reporting

Report a vulnerability privately through GitHub's
[security advisory form](https://github.com/ZhenGtai123/groundtruth-mcp/security/advisories/new)
rather than a public issue. Expect an acknowledgement within a week.

## The threat model this package actually has

It hands a language model a channel into your project. Three properties are
load-bearing, and a bug in any of them is a security bug, not a papercut.

**1. `query` cannot write.** The keyword scan in `data/guard.py` is user
experience — it rejects `DELETE FROM …` with a message the caller can act on
instead of a database error. It is explicitly *not* the boundary, and its gaps
are documented rather than hidden (`SELECT * INTO copy FROM users` starts with
`SELECT` and creates a table). Enforcement is the datastore: `mode=ro` plus
`PRAGMA query_only` on SQLite, a `READ ONLY` transaction on PostgreSQL, and a
statement timeout on both. `tests/test_data_guard.py` bypasses the guard and
asserts the connection still refuses.

If you find input that writes through a source in this package, that is a
vulnerability. If you find input that gets past the *regex* but is still
refused by the database, that is the design working — an issue is welcome, an
advisory is not needed.

**2. Denied columns never reach the model.** `deny_columns` is enforced after
the fetch and before the result string is built, so `SELECT *` cannot leak
them. A path that renders a denied value is a vulnerability.

**3. Relayed content is labelled.** Anything read from a data source is wrapped
in `<untrusted>` tags before it enters a tool result, because a row can contain
text shaped like an instruction. This is a mitigation, not a guarantee — no
tagging scheme survives a model that decides to follow the content anyway.
Treat it as defence in depth and do not connect this to a database whose rows
you would not want summarised aloud.

## What is out of scope

- **Your own toolkit module.** `@kit.loader`, `@kit.runner` and friends execute
  code you wrote, with your project's privileges. This package does not sandbox
  them. Do not register a capability that writes, deploys, or spends money.
- **`[project].toolkit` importing arbitrary code.** Loading a `groundtruth.toml`
  from an untrusted repository executes that repository's Python, exactly like
  running its test suite would. Treat a config file as code.
- **Denial of service through expensive simulations.** `max_runs`,
  `max_steps`, `max_rows` and the statement timeout bound one call; they are
  budget controls, not a defence against an operator who sets them to a
  million.

## Supported versions

Pre-1.0: fixes land on `main` and in the next release. There are no backports.
