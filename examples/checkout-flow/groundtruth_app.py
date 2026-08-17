"""The adoption template: everything a project has to write, and nothing more.

Roughly a hundred lines of glue turns the toy engine next door into five MCP
tools and a CI gate. Copy this file into your own repo, replace the four
functions with your project's equivalents, and you are done — the seed
batching, aggregation, threshold gating, output budgeting and error phrasing
all come from the library.

Read it in this order:

  1. `load_flow`    — how a name becomes a config. Everything else needs this.
  2. `check_policy` — a hand-written check, for the constraints a rule file
                      cannot express. The declarative rules live in rules.toml.
  3. `run_flow`     — one deterministic run producing a Trace. This single
                      function powers both the `replay` and `simulate` tools.
"""

from __future__ import annotations

import json
import random
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import engine
from groundtruth_mcp import Context, Issue, Loaded, Toolkit, Trace

FLOW_DIR = Path(__file__).parent / "flows"

kit = Toolkit(
    name="checkout-flow",
    description=(
        "A config-driven checkout state machine. Flows live in flows/*.json and describe "
        "the states a customer moves through, the payment gateway's retry policy, and where "
        "customers are allowed to drop out."
    ),
    # Tool descriptions read "which flow to check" instead of "which target to
    # check". Small thing; it measurably improves which tool an agent reaches for.
    subject_noun="flow",
    default_runs=200,
    max_runs=20_000,
    max_steps=200,
)


# ---------------------------------------------------------------------------
# 1. Loading
# ---------------------------------------------------------------------------


@kit.loader
def load_flow(name: str) -> Loaded:
    """Resolve a flow name to its parsed JSON, plus where it came from.

    Returning `Loaded` rather than a bare dict means every lint report says
    which file it read — the difference between "fix the dangling transition"
    and "fix the dangling transition in flows/standard_checkout.json".
    """
    path = FLOW_DIR / f"{name}.json"
    if not path.is_file():
        return None  # the library turns this into "no flow named X; available: ..."
    return Loaded(
        subject=json.loads(path.read_text(encoding="utf-8")),
        source=str(path.relative_to(Path(__file__).parent)),
    )


@kit.targets
def list_flows() -> dict[str, str]:
    """Names and one-line descriptions, used to make "not found" errors useful.

    Read from disk on every call rather than cached: an agent that just wrote a
    new flow file should see it on the next tool call, not after a restart.
    """
    found: dict[str, str] = {}
    for path in sorted(FLOW_DIR.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found[path.stem] = str(data.get("description", ""))
    return found


# ---------------------------------------------------------------------------
# 2. Checking — the rules.toml file covers structure; this covers semantics
# ---------------------------------------------------------------------------


@kit.validator
def check_policy(flow: Mapping[str, Any], ctx: Context) -> list[Issue]:
    """Constraints that need arithmetic, not a schema.

    A declarative rule can say `gateway_failure_rate` is between 0 and 1. It
    cannot say that this particular failure rate, combined with this retry
    limit, leaves more than one checkout in fifty failing outright — that is a
    product decision expressed as a formula, and formulas belong in code.
    """
    issues: list[Issue] = []
    policy = flow.get("policy", {})
    failure_rate = float(policy.get("gateway_failure_rate", 0.0))
    max_retries = int(policy.get("max_retries", 0))

    residual = failure_rate ** (max_retries + 1)
    if residual > 0.02:
        issues.append(
            Issue(
                code="RETRY_BUDGET_TOO_THIN",
                severity="error",
                message=(
                    f"{failure_rate:.0%} gateway failure with {max_retries} retries leaves "
                    f"{residual:.1%} of checkouts failing on payment alone (budget: 2.0%)"
                ),
                path="policy.max_retries",
                hint="raise max_retries, or lower gateway_failure_rate if the gateway improved",
            )
        )

    terminals = {
        state.get("outcome") for state in flow.get("states", []) if state.get("kind") == "terminal"
    }
    if "success" not in terminals:
        issues.append(
            Issue(
                code="NO_SUCCESS_TERMINAL",
                severity="error",
                message="no terminal state declares outcome = 'success'",
                path="states",
                hint='mark the confirmation state with {"kind": "terminal", "outcome": "success"}',
            )
        )
    return issues


# ---------------------------------------------------------------------------
# 3. Running — one function, two tools
# ---------------------------------------------------------------------------


@kit.runner
def run_flow(flow: Mapping[str, Any], seed: int, ctx: Context) -> Trace:
    """One checkout, start to finish, fully determined by `(flow, seed)`.

    No simulator is registered: the library derives `simulate` from this by
    running it once per seed and keeping the outcome and metrics. Register a
    separate `@kit.simulator` only when building the full trace is too
    expensive to do ten thousand times.
    """
    states = engine.index_states(flow)
    policy = flow.get("policy", {})
    rng = random.Random(seed)

    trace = Trace(subject=str(flow.get("id", "")), seed=seed)
    current = flow.get("start")
    attempts = 0
    total_latency = 0

    for _ in range(ctx.max_steps):
        state = states.get(current)
        if state is None:
            trace.outcome = "broken"
            trace.issues.append(
                Issue(
                    code="MISSING_STATE",
                    severity="error",
                    message=f"transition led to {current!r}, which is not declared",
                    path="states",
                    hint="run lint — a dangling transition is a config error, not a runtime one",
                )
            )
            break

        if state.get("kind") == "terminal":
            trace.outcome = str(state.get("outcome", state.get("id", "ended")))
            trace.step(current, note=f"terminal: {trace.outcome}")
            break

        if state.get("kind") == "gateway":
            attempts += 1

        result = engine.advance(state, policy, rng, attempts)
        total_latency += result.latency_ms
        trace.step(
            current,
            action=result.signal,
            note=result.note,
            state={"attempts": attempts, "latency_ms": total_latency},
        )

        if result.target is None:
            trace.outcome = "stuck"
            trace.issues.append(
                Issue(
                    code="NO_TRANSITION",
                    severity="error",
                    message=f"state {current!r} emitted {result.signal!r} with nowhere to go",
                    path=f"states[{current}].transitions",
                    hint=f"add a transition with when = {result.signal!r}, or an always transition",
                )
            )
            break
        current = result.target
    else:
        trace.outcome = trace.outcome or "step_budget_exhausted"
        trace.truncated = True

    trace.metrics = {
        "steps": float(len(trace.steps)),
        "latency_ms": float(total_latency),
        "payment_attempts": float(attempts),
    }
    return trace
