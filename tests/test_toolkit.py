"""Toolkit wiring: what each capability requires, and how failures are phrased."""

from __future__ import annotations

import random

import pytest

from groundtruth_mcp import (
    CapabilityNotConfigured,
    Issue,
    Loaded,
    RunOutcome,
    TargetNotFound,
    Threshold,
    Toolkit,
    ToolkitError,
    Trace,
)
from groundtruth_mcp.checks import RuleSet

SUBJECTS = {"good": {"limit": 3}, "bad": {"limit": 0}}


def make_kit(**kwargs) -> Toolkit:
    kit = Toolkit(name="test", subject_noun="widget", **kwargs)

    @kit.loader
    def load(name):
        return Loaded(subject=SUBJECTS[name], source=f"memory:{name}") if name in SUBJECTS else None

    @kit.targets
    def targets():
        return {"good": "works", "bad": "does not"}

    return kit


def test_missing_target_lists_what_is_available():
    kit = make_kit()

    @kit.runner
    def run(subject, seed, ctx):
        return Trace(outcome="ok")

    with pytest.raises(TargetNotFound, match=r"good, bad|bad, good"):
        kit.replay("typo")
    with pytest.raises(TargetNotFound, match="widget"):
        kit.replay("")


def test_capabilities_that_were_never_registered_say_how_to_register_them():
    kit = make_kit()
    with pytest.raises(CapabilityNotConfigured, match=r"@kit\.runner"):
        kit.replay("good")
    with pytest.raises(CapabilityNotConfigured, match=r"@kit\.validator"):
        kit.lint("good")
    with pytest.raises(CapabilityNotConfigured, match=r"@kit\.simulator"):
        kit.simulate("good")
    with pytest.raises(CapabilityNotConfigured, match=r"\[data\]"):
        kit.query("SELECT 1")


def test_user_exceptions_become_actionable_messages_without_a_traceback():
    kit = make_kit()

    @kit.runner
    def run(subject, seed, ctx):
        raise KeyError("states")

    with pytest.raises(ToolkitError, match=r"runner run raised KeyError"):
        kit.replay("good")


def test_a_runner_alone_powers_simulate():
    kit = make_kit()

    @kit.runner
    def run(subject, seed, ctx):
        rng = random.Random(seed)
        trace = Trace(outcome="hit" if rng.random() < 0.5 else "miss")
        trace.metrics = {"draws": 1.0}
        return trace

    assert kit.has_simulator
    summary = kit.simulate("good", runs=50)
    assert summary.runs == 50
    assert sum(summary.outcomes.values()) == 50
    assert summary.metrics["draws"]["mean"] == 1.0


def test_an_explicit_simulator_wins_over_the_runner():
    kit = make_kit()

    @kit.runner
    def run(subject, seed, ctx):
        raise AssertionError("the runner should not be called when a simulator exists")

    @kit.simulator
    def one(subject, seed, ctx):
        return RunOutcome(seed=seed, outcome="cheap", metrics={"cost": 1.0})

    assert kit.simulate("good", runs=5).outcomes["cheap"] == 5


def test_determinism_check_catches_an_engine_that_reads_hidden_state():
    kit = make_kit()
    calls = {"n": 0}

    @kit.simulator
    def drifting(subject, seed, ctx):
        calls["n"] += 1
        return RunOutcome(seed=seed, outcome="ok", metrics={"n": float(calls["n"])})

    summary = kit.simulate("good", runs=5, check_determinism=True)
    assert summary.deterministic is False
    assert summary.passed is False
    assert any("NOT DETERMINISTIC" in note for note in summary.notes)


def test_determinism_check_passes_for_a_pure_engine():
    kit = make_kit()

    @kit.simulator
    def pure(subject, seed, ctx):
        return RunOutcome(seed=seed, outcome="ok", metrics={"seed": float(seed)})

    summary = kit.simulate("good", runs=5, check_determinism=True)
    assert summary.deterministic is True


def test_run_count_is_clamped_to_the_configured_ceiling():
    kit = make_kit(default_runs=7, max_runs=10)

    @kit.simulator
    def one(subject, seed, ctx):
        return RunOutcome(seed=seed, outcome="ok")

    assert kit.simulate("good").runs == 7
    assert kit.simulate("good", runs=10_000).runs == 10
    assert kit.simulate("good", runs=0).runs == 7  # 0 means "use the default"


def test_rule_issues_and_validator_issues_land_in_one_report():
    kit = make_kit()
    kit.use_rules(RuleSet.from_dicts([{"type": "range", "select": "limit", "min": 1}]))

    @kit.validator
    def extra(subject, ctx):
        return [Issue(code="CUSTOM", severity="warning", message="from python", path="limit")]

    report = kit.lint("bad")
    assert {i.code for i in report.issues} == {"OUT_OF_RANGE", "CUSTOM"}
    assert report.ok is False  # one error present
    assert kit.lint("good").errors == []
    assert kit.lint("good").warnings[0].code == "CUSTOM"


def test_a_validator_returning_the_wrong_shape_is_a_clear_error():
    kit = make_kit()

    @kit.validator
    def wrong(subject, ctx):
        return "looks fine to me"

    with pytest.raises(ToolkitError, match="expected"):
        kit.lint("good")


def test_thresholds_read_by_simulate_come_from_the_toolkit():
    kit = make_kit()
    kit.use_thresholds([Threshold(metric="rate:ok", min=1.0)])

    @kit.simulator
    def one(subject, seed, ctx):
        return RunOutcome(seed=seed, outcome="ok" if seed % 2 else "no")

    assert kit.simulate("good", runs=10).passed is False
