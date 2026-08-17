# Contributing

```bash
pip install -e ".[dev,mcp]"
pytest -q
python scripts/mcp_smoke.py          # real stdio round trip against the example
```

## What fits here

The library holds what is the same in every project: the seed loop, the
aggregation, the threshold gating, the output budget, the error phrasing, the
MCP surface. It does not hold anything that knows what a state, a stage or a
scenario *means* — that belongs in the adopting project's toolkit module, which
is why the API is four small hooks rather than a base class to inherit from.

Good additions:

- **A new check type** in `checks/rules.py`. One decorated function, plus tests
  for the issue it emits *and* for the rule-file error when it is misconfigured.
  Malformed rules must fail at load time, never at check time.
- **A new metric aggregate** in `stats.py::_AGGREGATES` — immediately usable in
  thresholds.
- **A new data source**: any object with `available()`, `query()` and
  `schema()`. Enforcement must come from the datastore, not from string
  inspection.

Harder sells: predicates in the selector language, a plugin system, an async
API, anything that makes the core depend on a third-party package. The core's
lack of dependencies is a feature — it is what lets a bare CI runner enforce
thresholds without installing an agent stack.

## House style

- Comments explain *why*. The code already says what.
- Error messages state what was wrong **and** what to pass instead. An agent
  cannot act on `KeyError: 'states'`; it can act on "no flow named 'chekout';
  available: standard_checkout, express_checkout".
- Tests cover the boundary, not the mock. `test_data_guard.py` goes around the
  SQL guard entirely to confirm the database refuses the write — that is the
  claim worth testing.
- No new user-visible string without a fix in it, where a fix exists.
