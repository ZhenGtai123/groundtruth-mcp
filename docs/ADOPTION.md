# Wiring this into your own project

Budget an afternoon. Most of it is deciding what is worth exposing; the code is
about a hundred lines.

```bash
pip install "groundtruth-mcp[mcp]"
```

## 1. Decide what the agent is allowed to observe

The tools are cut along the lines of the questions someone editing your project
actually asks, not along your API surface:

| The question | The tool | What it must be |
|---|---|---|
| Is this config self-consistent? | `lint` | Fast (sub-second), no side effects, precise about *where* |
| What happens when I run this one? | `replay` | Deterministic given `(subject, seed)` |
| Is the change better or worse overall? | `simulate` | Cheap enough to run thousands of times |
| What is actually in the data? | `query` | Read-only, with the boundary enforced by the datastore |

If your project has no simulation-shaped question, register a `runner` and skip
thresholds. Three good tools beat five where two are noise.

## 2. Write the toolkit module

Copy `examples/checkout-flow/groundtruth_app.py` next to your config files and
replace the bodies. Only the loader is mandatory.

```python
from groundtruth_mcp import Context, Issue, Loaded, Toolkit, Trace

kit = Toolkit(name="my-project", subject_noun="pipeline")

@kit.loader
def load(name: str):
    path = CONFIG_DIR / f"{name}.yaml"
    if not path.is_file():
        return None                      # becomes "no pipeline named X; available: ..."
    return Loaded(subject=parse(path), source=str(path))

@kit.targets
def targets() -> dict[str, str]:
    return {p.stem: describe(p) for p in CONFIG_DIR.glob("*.yaml")}

@kit.validator
def check(pipeline, ctx: Context) -> list[Issue]:
    return [Issue(code="...", severity="error", message="...", path="...", hint="...")]

@kit.runner
def run_once(pipeline, seed: int, ctx: Context) -> Trace:
    trace = Trace()
    ...
    trace.step("stage-1", action="ok", state={"queued": 3})
    trace.metrics = {"duration_ms": 1200.0}
    trace.outcome = "success"
    return trace
```

Three details that decide how useful this is:

**`Issue.path` must be precise enough to edit from.** `states[3].transitions[1].to`
sends an agent to the line. "A transition is invalid" sends it grepping.

**`Issue.hint` is the difference between a report and a fix.** Say what to
change, in the imperative.

**The runner must be pure.** All randomness from a `random.Random(seed)` (or
your language's equivalent) created inside the function. No clock, no set
iteration, no module-level generator. Verify with
`groundtruth simulate <target> --check-determinism`.

Register a separate `@kit.simulator` returning a `RunOutcome` only when
building a full trace is too expensive to do thousands of times. Otherwise the
runner powers both tools.

## 3. Declare the structural rules

Anything expressible as "this config is a graph with ids, edges and a start"
belongs in `rules.toml`, not in Python. Twelve check types cover it; see the
table in the README.

```toml
[[rule]]
type = "ref_exists"
select = "stages[].depends_on[]"
collection = "stages[]"
key = "id"
code = "UNKNOWN_DEPENDENCY"
hint = "name a stage that exists, or drop the dependency"
```

Keep in Python only the checks that need arithmetic or domain knowledge — "does
this retry budget meet the product's failure target" is a formula, and formulas
belong in code where they can be tested.

## 4. Write `groundtruth.toml`

```toml
[project]
name = "my-project"
toolkit = "groundtruth_app:kit"

[lint]
rules = "rules.toml"

[simulate]
runs = 200
max_runs = 20000

[[thresholds]]
metric = "rate:success"
min = 0.80
note = "why this number, for whoever has to change it"

[data]
driver = "sqlite"
url = "var/events.db"
allow_tables = ["runs", "events"]
deny_columns = ["email", "card_last4"]
```

Then check your work:

```bash
groundtruth doctor
```

It prints which capabilities are wired, which thresholds are declared, whether
the data source is reachable, and whether the MCP SDK is installed.

Pick threshold numbers by running the simulation first and reading the
distribution. A band you derived from an observed distribution is a decision; a
round number you guessed is a coin flip that will either never fire or fire
constantly. `docs/DETERMINISM.md` has the arithmetic for how much headroom to
leave.

## 5. Give the agent the tools

`.mcp.json` at your repo root:

```json
{
  "mcpServers": {
    "my-project": {
      "command": "python",
      "args": ["-m", "groundtruth_mcp.cli", "--config", "groundtruth.toml", "serve"]
    }
  }
}
```

Point `command` at the interpreter that has your project's dependencies — a
virtualenv's `python`, not necessarily the one on `PATH`. Then prove it works
the way a client does, over a real subprocess:

```bash
python scripts/mcp_smoke.py groundtruth.toml
```

## 6. Make the same numbers a merge gate

The point of one config file is that CI and the agent cannot disagree.

```yaml
- run: groundtruth lint my_pipeline
- run: groundtruth simulate my_pipeline --runs 2000 --seed 0 --gate --check-determinism
```

`.github/workflows/ci.yml` in this repo is a working version, including
publishing the batch to the job summary so a reviewer can see the distribution
without re-running anything.

## Common shapes

| If your project is… | `subject_noun` | `lint` checks | one run is | thresholds on |
|---|---|---|---|---|
| A workflow / DAG engine | `pipeline` | dangling deps, cycles, unreachable stages | one execution with seeded task failures | success rate, p95 duration, retry count |
| A rules or pricing engine | `ruleset` | undefined variables, unreachable rules, overlapping conditions | one evaluation over a seeded input | match rate, fallthrough rate |
| A game or simulation | `scenario` | undefined refs, unreachable endings, broken graph | one playthrough under a fixed policy | win rate, run length, action variety |
| An agent / prompt pipeline | `flow` | unknown tools, unreachable branches, missing fallbacks | one seeded trajectory against a stub model | completion rate, step count, tool-error rate |
| An infra config | `environment` | dangling refs, missing required keys, quota arithmetic | one deployment against a fake API | apply success rate, plan size |

## Things worth not doing

**Do not expose a write path.** Everything here is read-only by construction.
An agent that can trigger a deploy from a tool call will eventually trigger a
deploy from a tool call.

**Do not return unbounded output.** The output budget (`[output].max_chars`)
exists because one enthusiastic query can evict the rest of the conversation.

**Do not paper over a missing capability with a tool that explains itself.** A
registered tool that always answers "not configured" costs a call and some
context to teach the agent something the tool list said for free. `build_server`
skips capabilities you did not register, deliberately.
