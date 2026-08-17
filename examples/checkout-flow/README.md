# Example: a config-driven checkout

A four-page checkout with a flaky payment gateway, a retry policy, and
customers who leave. Small enough to read in one sitting, complete enough that
every capability has something real to do.

```bash
pip install -e "../..[mcp]"                       # from this directory
export CONFIG=examples/checkout-flow/groundtruth.toml   # run the rest from the repo root

groundtruth --config $CONFIG doctor
groundtruth --config $CONFIG lint standard_checkout          # clean
groundtruth --config $CONFIG lint broken_checkout            # 6 errors, 1 warning
groundtruth --config $CONFIG replay standard_checkout --seed 3
groundtruth --config $CONFIG simulate standard_checkout --runs 2000 --seed 0 --gate

python examples/checkout-flow/seed_data.py                   # builds var/checkout.db
groundtruth --config $CONFIG schema
groundtruth --config $CONFIG query "SELECT outcome, count(*) AS n FROM runs GROUP BY outcome"
```

## What each file is for

| File | Role |
|---|---|
| `groundtruth_app.py` | **The template.** Loader, validator, runner — copy this and replace the bodies |
| `groundtruth.toml` | Thresholds, simulation defaults, data source. Read by both the MCP server and CI |
| `rules.toml` | Twelve declarative structural checks |
| `engine.py` | Stands in for *your* engine. Not part of the library |
| `flows/*.json` | The configs under test |
| `seed_data.py` | Generates `var/checkout.db` from a deterministic batch |
| `.mcp.json` | Wiring for Claude Code or any MCP client |

## The three flows, and why each exists

**`standard_checkout`** — healthy. Lints clean, simulates at 88.3% success,
passes every threshold. The baseline.

**`express_checkout`** — the interesting one. Structurally perfect, and it
posts a *higher* success rate than the standard flow (91.0%). It is still the
worse config: one retry against an 18% failure rate leaves 3.9% of checkouts
failing on payment, against 0.3% for the standard flow, and the headline number
hides it completely. `check_policy` in `groundtruth_app.py` fails it on the
arithmetic. This is why a hand-written validator is worth the eight lines: no
schema and no aggregate metric catches this.

**`broken_checkout`** — seven planted defects, one per failure mode an agent
introduces when editing config it cannot run: a typo'd transition target, a
duplicated state id, an unknown `kind`, an unreachable state, a non-terminal
dead end, a probability written as a percentage, and a retry budget that cannot
meet the failure target.

## Things to try

Break something and watch which layer catches it:

```bash
# rename a state without updating what points at it  -> DANGLING_TRANSITION
# set gateway_failure_rate to 18 instead of 0.18     -> RATE_OUT_OF_RANGE
# raise shipping.abandon_chance to 0.28              -> lint passes, the gate fails
# make run_flow read time.time()                     -> --check-determinism fails
```

The third is the one worth doing by hand. Nothing structural is wrong, every
check passes, and the simulation reports success falling from 88.3% to 66.8%
against a floor of 80% — exit code 1, on the pull request.
