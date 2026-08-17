"""Turning many runs into a few numbers, and those numbers into a verdict.

The unit of feedback here is not "it worked" but "here is the distribution,
here is the band it must sit in, here is which side of the band it is on."
That distinction is what lets a threshold serve as a merge gate: a mean on its
own drifts quietly, a mean with a declared band fails loudly the first time it
leaves.

Metric expressions are `<aggregate>:<name>`:

    rate:success          share of runs whose outcome label is "success"
    mean:steps            arithmetic mean of the per-run metric "steps"
    p95:latency_ms        95th percentile, nearest-rank
    max:retries           worst observed run

Percentiles use nearest-rank (`ceil(p/100 * n)`), not interpolation. Two
implementations that interpolate differently produce different p95s from the
same data, and a gate that disagrees with itself across machines is worse than
no gate.
"""

from __future__ import annotations

import math
import statistics
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .contracts import RunOutcome
from .determinism import fingerprint


def percentile(values: Sequence[float], pct: float) -> float:
    """Nearest-rank percentile. `pct` in [0, 100]."""
    if not values:
        raise ValueError("percentile of an empty sample")
    if not 0 <= pct <= 100:
        raise ValueError(f"percentile must be in [0, 100], got {pct}")
    ordered = sorted(values)
    rank = max(1, math.ceil(pct / 100 * len(ordered)))
    return float(ordered[rank - 1])


_AGGREGATES: dict[str, Callable[[Sequence[float]], float]] = {
    "mean": lambda v: float(statistics.fmean(v)),
    "median": lambda v: float(statistics.median(v)),
    "min": lambda v: float(min(v)),
    "max": lambda v: float(max(v)),
    "sum": lambda v: float(sum(v)),
    "stdev": lambda v: float(statistics.pstdev(v)),
    "count": lambda v: float(len(v)),
    "p50": lambda v: percentile(v, 50),
    "p75": lambda v: percentile(v, 75),
    "p90": lambda v: percentile(v, 90),
    "p95": lambda v: percentile(v, 95),
    "p99": lambda v: percentile(v, 99),
}


class MetricError(ValueError):
    """An unparseable or unsatisfiable metric expression."""


@dataclass(frozen=True)
class Threshold:
    """One metric expression and the band it must stay inside.

    At least one bound is required; a threshold with neither would pass
    unconditionally, which is indistinguishable from having forgotten to write
    it and much harder to notice.
    """

    metric: str
    min: float | None = None
    max: float | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if self.min is None and self.max is None:
            raise ValueError(
                f"threshold on {self.metric!r} declares neither min nor max — "
                "a band with no edges never fails"
            )
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(
                f"threshold on {self.metric!r} has min={self.min} > max={self.max}"
            )

    @classmethod
    def from_dicts(cls, specs: Sequence[Mapping[str, Any]]) -> list["Threshold"]:
        out: list[Threshold] = []
        for index, spec in enumerate(specs):
            metric = str(spec.get("metric", "")).strip()
            if not metric:
                raise ValueError(f"threshold #{index + 1}: missing `metric`")
            out.append(
                cls(
                    metric=metric,
                    min=_as_float(spec.get("min")),
                    max=_as_float(spec.get("max")),
                    note=str(spec.get("note", "")),
                )
            )
        return out

    def band(self) -> str:
        if self.min is not None and self.max is not None:
            return f"[{_fmt(self.min)}, {_fmt(self.max)}]"
        if self.min is not None:
            return f">= {_fmt(self.min)}"
        return f"<= {_fmt(self.max)}"


def _as_float(value: Any) -> float | None:
    return None if value is None else float(value)


def _fmt(value: float) -> str:
    return f"{value:g}"


@dataclass(frozen=True)
class ThresholdResult:
    threshold: Threshold
    observed: float
    ok: bool

    def format(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        note = f"  ({self.threshold.note})" if self.threshold.note else ""
        return (
            f"{verdict}  {self.threshold.metric} = {_fmt(self.observed)}  "
            f"expected {self.threshold.band()}{note}"
        )


@dataclass
class Summary:
    """Everything a batch of runs is worth remembering."""

    subject: str = ""
    runs: int = 0
    base_seed: int = 0
    outcomes: Counter = field(default_factory=Counter)
    metrics: dict[str, dict[str, float]] = field(default_factory=dict)
    gate: list[ThresholdResult] = field(default_factory=list)
    fingerprint: str = ""
    notes: list[str] = field(default_factory=list)
    # None when nobody asked; False means the same seed produced two different
    # runs, which invalidates every number above it.
    deterministic: bool | None = None

    @property
    def passed(self) -> bool:
        """A batch with no thresholds passes — it reports, it does not gate.

        A batch that failed its determinism check never passes, whatever the
        thresholds say: numbers derived from an irreproducible run are not
        evidence, and letting them gate a merge is worse than not gating.
        """
        if self.deterministic is False:
            return False
        return all(result.ok for result in self.gate)

    def value(self, expression: str) -> float:
        return _evaluate(expression, self.outcomes, self.metrics, self.runs)

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "runs": self.runs,
            "base_seed": self.base_seed,
            "fingerprint": self.fingerprint,
            "outcomes": dict(self.outcomes),
            "metrics": self.metrics,
            "passed": self.passed,
            "deterministic": self.deterministic,
            "gate": [
                {
                    "metric": r.threshold.metric,
                    "observed": r.observed,
                    "min": r.threshold.min,
                    "max": r.threshold.max,
                    "ok": r.ok,
                }
                for r in self.gate
            ],
            "notes": list(self.notes),
        }


def summarize(
    outcomes: Sequence[RunOutcome],
    thresholds: Sequence[Threshold] = (),
    *,
    subject: str = "",
    base_seed: int = 0,
) -> Summary:
    """Aggregate runs, then judge them against the declared bands."""
    if not outcomes:
        raise ValueError("cannot summarize zero runs")

    outcome_counts: Counter = Counter(run.outcome for run in outcomes)
    samples: dict[str, list[float]] = {}
    for run in outcomes:
        for name, value in run.metrics.items():
            samples.setdefault(name, []).append(float(value))

    aggregated = {
        name: {agg: fn(values) for agg, fn in _AGGREGATES.items()}
        for name, values in samples.items()
    }

    summary = Summary(
        subject=subject,
        runs=len(outcomes),
        base_seed=base_seed,
        outcomes=outcome_counts,
        metrics=aggregated,
        fingerprint=fingerprint(
            [(run.seed, run.outcome, tuple(sorted(run.metrics.items()))) for run in outcomes]
        ),
    )
    summary.gate = [
        ThresholdResult(
            threshold=threshold,
            observed=(observed := summary.value(threshold.metric)),
            ok=(threshold.min is None or observed >= threshold.min)
            and (threshold.max is None or observed <= threshold.max),
        )
        for threshold in thresholds
    ]
    return summary


def _evaluate(
    expression: str,
    outcomes: Counter,
    metrics: Mapping[str, Mapping[str, float]],
    runs: int,
) -> float:
    aggregate, _, name = expression.partition(":")
    aggregate = aggregate.strip()
    name = name.strip()

    if aggregate == "rate":
        if not name:
            raise MetricError("`rate:` needs an outcome label, e.g. rate:success")
        return outcomes.get(name, 0) / runs if runs else 0.0

    if aggregate == "count" and not name:
        return float(runs)

    if aggregate not in _AGGREGATES:
        known = ", ".join(["rate"] + sorted(_AGGREGATES))
        raise MetricError(
            f"unknown aggregate {aggregate!r} in {expression!r}. Known aggregates: {known}"
        )
    if name not in metrics:
        known = ", ".join(sorted(metrics)) or "(none reported)"
        raise MetricError(
            f"no run reported a metric named {name!r} (from {expression!r}). "
            f"Metrics available: {known}"
        )
    return metrics[name][aggregate]


def format_distribution(summary: Summary, top: int = 8) -> list[str]:
    """Outcome shares, most common first — the shape behind the headline rate."""
    lines = []
    for label, count in summary.outcomes.most_common(top):
        share = count / summary.runs if summary.runs else 0.0
        lines.append(f"  {label}: {count} ({share:.1%})")
    return lines
