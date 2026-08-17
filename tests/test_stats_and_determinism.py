"""Aggregation, threshold gating, and the seed guarantees underneath both."""

from __future__ import annotations

import pytest

from groundtruth_mcp import RunOutcome, Threshold, percentile, seeds, summarize
from groundtruth_mcp.determinism import compare_batches, seed_for
from groundtruth_mcp.stats import MetricError


def batch(labels, values):
    return [
        RunOutcome(seed=i, outcome=label, metrics={"steps": float(value)})
        for i, (label, value) in enumerate(zip(labels, values, strict=True))
    ]


def test_percentile_uses_nearest_rank_not_interpolation():
    sample = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert percentile(sample, 95) == 10
    assert percentile(sample, 50) == 5
    assert percentile(sample, 0.1) == 1
    with pytest.raises(ValueError):
        percentile([], 50)


def test_rate_counts_outcome_labels_including_ones_nobody_produced():
    summary = summarize(batch(["ok", "ok", "bad"], [1, 2, 3]))
    assert summary.value("rate:ok") == pytest.approx(2 / 3)
    # A label no run produced is a rate of zero, not an error: `rate:stuck <= 0`
    # has to be expressible before anything has ever got stuck.
    assert summary.value("rate:never_happened") == 0.0


def test_unknown_metric_names_say_what_is_available():
    summary = summarize(batch(["ok"], [1]))
    with pytest.raises(MetricError, match="steps"):
        summary.value("mean:latency")
    with pytest.raises(MetricError, match="Known aggregates"):
        summary.value("wobble:steps")


def test_thresholds_gate_on_both_edges():
    outcomes = batch(["ok"] * 8 + ["bad"] * 2, range(10))
    summary = summarize(
        outcomes,
        [Threshold(metric="rate:ok", min=0.8), Threshold(metric="max:steps", max=5)],
    )
    assert [r.ok for r in summary.gate] == [True, False]
    assert summary.passed is False


def test_a_threshold_with_no_bound_is_rejected_at_construction():
    with pytest.raises(ValueError, match="never fails"):
        Threshold(metric="rate:ok")
    with pytest.raises(ValueError, match=r"min=.* > max="):
        Threshold(metric="mean:steps", min=10, max=1)


def test_no_thresholds_means_report_only():
    assert summarize(batch(["ok"], [1])).passed is True


def test_fingerprint_tracks_behaviour_not_ordering_noise():
    first = summarize(batch(["ok", "bad"], [1, 2]))
    same = summarize(batch(["ok", "bad"], [1, 2]))
    different = summarize(batch(["ok", "bad"], [1, 3]))
    assert first.fingerprint == same.fingerprint
    assert first.fingerprint != different.fingerprint


def test_non_determinism_fails_the_gate_even_when_every_threshold_passes():
    summary = summarize(batch(["ok"], [1]), [Threshold(metric="rate:ok", min=0.5)])
    assert summary.passed is True
    summary.deterministic = False
    assert summary.passed is False


def test_seed_policies():
    assert seeds(100, 3) == [100, 101, 102]
    hashed = seeds(100, 3, "hash")
    assert len(set(hashed)) == 3
    assert hashed == seeds(100, 3, "hash")  # stable across calls
    assert seed_for(0, 0, "hash") != seed_for(0, 1, "hash")
    with pytest.raises(ValueError):
        seeds(0, 0)


def test_compare_batches_returns_replayable_indices():
    assert compare_batches(["a", "b", "c"], ["a", "x", "c"]) == [1]
    assert compare_batches(["a"], ["a"]) == []
