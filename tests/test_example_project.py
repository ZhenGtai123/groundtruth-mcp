"""End to end over the bundled example: config -> toolkit -> every capability.

If this file passes, `git clone && groundtruth lint standard_checkout` works
for a stranger. That is the only claim a README's quickstart can make honestly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth_mcp import ProjectConfig
from groundtruth_mcp.cli import EXIT_FINDINGS, EXIT_OK, main

CONFIG = Path(__file__).resolve().parent.parent / "examples" / "checkout-flow" / "groundtruth.toml"


@pytest.fixture(scope="module")
def kit():
    return ProjectConfig.load(CONFIG).build()


def test_config_wires_up_every_capability(kit):
    assert (kit.has_lint, kit.has_runner, kit.has_simulator, kit.has_data) == (True,) * 4
    assert len(kit.ruleset) == 12
    assert [t.metric for t in kit.thresholds] == [
        "rate:success",
        "rate:stuck",
        "p95:latency_ms",
        "mean:payment_attempts",
    ]
    assert set(kit.available_targets()) == {
        "standard_checkout",
        "express_checkout",
        "broken_checkout",
    }


def test_the_healthy_flow_lints_clean(kit):
    report = kit.lint("standard_checkout")
    assert report.issues == []
    assert report.source.endswith("standard_checkout.json")


def test_the_broken_flow_reports_each_planted_defect_once(kit):
    report = kit.lint("broken_checkout")
    codes = {issue.code for issue in report.issues}
    assert codes == {
        "DANGLING_TRANSITION",
        "DUPLICATE_STATE",
        "UNKNOWN_STATE_KIND",
        "UNREACHABLE_STATE",
        "DEAD_END",
        "RATE_OUT_OF_RANGE",
        "RETRY_BUDGET_TOO_THIN",
    }
    assert not report.ok
    dangling = next(i for i in report.issues if i.code == "DANGLING_TRANSITION")
    assert dangling.path == "states[1].transitions[0].to"


def test_a_hand_written_validator_catches_what_no_schema_would(kit):
    """express_checkout is structurally perfect and still wrong."""
    report = kit.lint("express_checkout")
    assert [i.code for i in report.errors] == ["RETRY_BUDGET_TOO_THIN"]


def test_the_same_seed_replays_identically(kit):
    first = kit.replay("standard_checkout", seed=7)
    second = kit.replay("standard_checkout", seed=7)
    assert first.fingerprint() == second.fingerprint()
    assert [s.node for s in first.steps] == [s.node for s in second.steps]
    assert first.outcome == "success"


def test_different_seeds_take_different_paths(kit):
    paths = {tuple(s.node for s in kit.replay("standard_checkout", seed=s)) for s in range(30)}
    assert len(paths) > 1


def test_the_simulation_is_reproducible_and_inside_its_band(kit):
    first = kit.simulate("standard_checkout", runs=500, seed=0, check_determinism=True)
    second = kit.simulate("standard_checkout", runs=500, seed=0)
    assert first.fingerprint == second.fingerprint
    assert first.deterministic is True
    assert first.passed is True
    assert first.value("rate:success") > 0.8


def test_a_thinner_retry_budget_shows_up_in_the_numbers(kit):
    """The comparison the agent is actually making when it edits a policy."""
    standard = kit.simulate("standard_checkout", runs=1000, seed=0)
    express = kit.simulate("express_checkout", runs=1000, seed=0)
    assert express.value("rate:payment_failed") > standard.value("rate:payment_failed")


def test_cli_exit_codes_are_what_ci_branches_on(capsys):
    assert main(["--config", str(CONFIG), "lint", "standard_checkout"]) == EXIT_OK
    assert main(["--config", str(CONFIG), "lint", "broken_checkout"]) == EXIT_FINDINGS
    assert (
        main(["--config", str(CONFIG), "simulate", "standard_checkout", "--runs", "100", "--gate"])
        == EXIT_OK
    )
    assert "PASS" in capsys.readouterr().out


def test_cli_json_output_is_machine_readable(capsys):
    import json

    main(["--config", str(CONFIG), "simulate", "standard_checkout", "--runs", "50", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["runs"] == 50
    assert payload["passed"] is True
    assert "fingerprint" in payload
