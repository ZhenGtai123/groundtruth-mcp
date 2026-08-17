"""A small config-driven flow engine — the stand-in for *your* project's engine.

This is not part of the library. It exists so the example has something real to
lint, replay and simulate: a checkout that walks a state machine, occasionally
loses the customer, and retries a flaky payment gateway.

The one property that matters, and the one your engine must also have: a run is
a pure function of `(flow, seed)`. All randomness comes from the `random.Random`
instance seeded per run — never the module-level `random`, never `time`, never
iteration order over a set. Break that and every number the simulator produces
becomes a rumour.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from typing import Any

# Signals a state can emit; each transition declares the one it answers to.
ALWAYS = "always"
ABANDON = "abandon"
SUCCESS = "success"
FAILURE = "failure"
RETRIES_LEFT = "retries_left"
RETRIES_EXHAUSTED = "retries_exhausted"


class FlowError(RuntimeError):
    """The flow is malformed in a way the run cannot continue past."""


def index_states(flow: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """State id -> state. First declaration wins, matching the lint's duplicate report."""
    index: dict[str, Mapping[str, Any]] = {}
    for state in flow.get("states", []):
        key = state.get("id")
        if isinstance(key, str):
            index.setdefault(key, state)
    return index


def _pick(state: Mapping[str, Any], signal: str) -> str | None:
    """The target for `signal`, falling back to an `always` transition."""
    fallback: str | None = None
    for transition in state.get("transitions", []):
        when = transition.get("when", ALWAYS)
        if when == signal:
            return transition.get("to")
        if when == ALWAYS:
            fallback = transition.get("to")
    return fallback


def _latency(state: Mapping[str, Any], policy: Mapping[str, Any], rng: random.Random) -> int:
    base = int(state.get("latency_ms", 0))
    jitter = int(policy.get("latency_jitter_ms", 0))
    return base + (rng.randint(0, jitter) if jitter > 0 else 0)


class StepResult:
    """What one state did: where it went, what it emitted, what it cost."""

    __slots__ = ("latency_ms", "note", "signal", "target")

    def __init__(self, signal: str, target: str | None, latency_ms: int, note: str = "") -> None:
        self.signal = signal
        self.target = target
        self.latency_ms = latency_ms
        self.note = note


def advance(
    state: Mapping[str, Any],
    policy: Mapping[str, Any],
    rng: random.Random,
    attempts: int,
) -> StepResult:
    """Execute one state and decide where control goes next.

    Every branch draws from `rng` in a fixed order. Reordering these draws
    changes every historical seed's trace, which is why the seeded fingerprint
    in the simulation summary is worth watching: it catches exactly that.
    """
    kind = state.get("kind", "step")
    latency = _latency(state, policy, rng)

    if kind == "gateway":
        failure_rate = float(policy.get("gateway_failure_rate", 0.0))
        failed = rng.random() < failure_rate
        signal = FAILURE if failed else SUCCESS
        note = f"attempt {attempts} {'declined' if failed else 'authorized'}"
        return StepResult(signal, _pick(state, signal), latency, note)

    if kind == "retry":
        max_retries = int(policy.get("max_retries", 0))
        signal = RETRIES_LEFT if attempts <= max_retries else RETRIES_EXHAUSTED
        note = f"{attempts - 1} retry(s) used of {max_retries}"
        return StepResult(signal, _pick(state, signal), latency, note)

    # A plain step: the customer either continues or gives up here.
    abandon_chance = float(state.get("abandon_chance", policy.get("abandon_chance", 0.0)))
    abandoned = rng.random() < abandon_chance
    signal = ABANDON if abandoned else ALWAYS
    note = "customer left" if abandoned else ""
    return StepResult(signal, _pick(state, signal), latency, note)
