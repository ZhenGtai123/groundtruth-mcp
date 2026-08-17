"""The registry your project fills in, and the loop this package runs around it.

You bring four small functions — how to load a config, how to check it, how to
run it once, what one run is worth numerically. This module supplies
everything around them that is the same in every project: seed batching,
aggregation, threshold gating, determinism checking, output budgeting, error
messages an agent can act on, and the MCP surface itself.

The division is deliberate. Your engine is the part nobody else can write; the
harness around it is the part everybody rewrites badly. In the codebase this
was extracted from, the harness had grown into the tool wrapper and the tool
wrapper had grown into the CI script, so the CI band lived in one file, the
tool's copy of the band lived in another, and they drifted. Here there is one
`Toolkit`, and the MCP server and the CI gate are two thin readers of it.

    from groundtruth_mcp import Toolkit

    kit = Toolkit(name="checkout-flow")

    @kit.loader
    def load(target): ...

    @kit.runner
    def run_once(flow, seed, ctx): ...

Register a `runner` and you get `replay` *and* `simulate` — the simulator
falls back to running the full trace and keeping its outcome and metrics.
Register a `simulator` too when a full trace per run is too expensive to build
ten thousand times.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .budget import DEFAULT_MAX_CHARS
from .checks.rules import RuleSet
from .contracts import (
    CapabilityNotConfigured,
    Issue,
    Report,
    RunOutcome,
    TargetNotFound,
    ToolkitError,
    Trace,
)
from .data.guard import QueryRejected, QueryResult, TableInfo
from .determinism import SeedPolicy, seeds
from .stats import Summary, Threshold, summarize


@dataclass(frozen=True)
class Loaded:
    """A loaded subject plus where it came from, for provenance in output."""

    subject: Any
    source: str = ""


@dataclass
class Context:
    """What your functions get besides the subject itself."""

    target: str
    settings: Mapping[str, Any] = field(default_factory=dict)
    seed: int | None = None
    max_steps: int = 500


@dataclass
class Capability:
    fn: Callable[..., Any]
    description: str = ""

    @property
    def label(self) -> str:
        return getattr(self.fn, "__name__", "<anonymous>")


def _decorator(register: Callable[[Callable[..., Any], str], None]):
    """Support both `@kit.runner` and `@kit.runner(description=...)`."""

    def outer(fn: Callable[..., Any] | None = None, *, description: str = ""):
        if fn is not None:
            register(fn, description)
            return fn

        def inner(real_fn: Callable[..., Any]):
            register(real_fn, description)
            return real_fn

        return inner

    return outer


class Toolkit:
    """One project's capabilities, plus the machinery that runs them."""

    def __init__(
        self,
        name: str,
        *,
        description: str = "",
        version: str = "0.1.0",
        subject_noun: str = "target",
        default_runs: int = 100,
        max_runs: int = 2000,
        max_steps: int = 500,
        seed_policy: SeedPolicy = "offset",
        max_output_chars: int = DEFAULT_MAX_CHARS,
    ) -> None:
        self.name = name
        self.description = description
        self.version = version
        # What your configs are called in the agent's tool descriptions —
        # "flow", "pipeline", "playbook". The tool text reads better in the
        # domain's own nouns, and the agent picks the right tool more often.
        self.subject_noun = subject_noun
        self.default_runs = default_runs
        self.max_runs = max_runs
        self.max_steps = max_steps
        self.seed_policy: SeedPolicy = seed_policy
        self.max_output_chars = max_output_chars

        self.settings: dict[str, Any] = {}
        self.ruleset: RuleSet | None = None
        self.thresholds: list[Threshold] = []
        self.source: Any = None  # a data source, duck-typed (see data/)

        self._loader: Capability | None = None
        self._targets: Capability | None = None
        self._validators: list[Capability] = []
        self._runner: Capability | None = None
        self._simulator: Capability | None = None

    # -- registration -------------------------------------------------------

    @property
    def loader(self):
        """`(target: str) -> subject | Loaded`. Required by every other capability."""
        return _decorator(lambda fn, desc: setattr(self, "_loader", Capability(fn, desc)))

    @property
    def targets(self):
        """`() -> Iterable[str] | Mapping[str, str]`. Only used to make errors useful."""
        return _decorator(lambda fn, desc: setattr(self, "_targets", Capability(fn, desc)))

    @property
    def validator(self):
        """`(subject, ctx) -> Report | Iterable[Issue] | None`. Register as many as you like."""
        return _decorator(lambda fn, desc: self._validators.append(Capability(fn, desc)))

    @property
    def runner(self):
        """`(subject, seed: int, ctx) -> Trace`. Must be a pure function of its arguments."""
        return _decorator(lambda fn, desc: setattr(self, "_runner", Capability(fn, desc)))

    @property
    def simulator(self):
        """`(subject, seed: int, ctx) -> RunOutcome`. Optional; derived from the runner."""
        return _decorator(lambda fn, desc: setattr(self, "_simulator", Capability(fn, desc)))

    def use_rules(self, ruleset: RuleSet) -> None:
        self.ruleset = ruleset

    def use_thresholds(self, thresholds: Sequence[Threshold]) -> None:
        self.thresholds = list(thresholds)

    def use_data_source(self, source: Any) -> None:
        self.source = source

    # -- introspection ------------------------------------------------------

    @property
    def has_lint(self) -> bool:
        return bool(self._validators) or (self.ruleset is not None and len(self.ruleset) > 0)

    @property
    def has_runner(self) -> bool:
        return self._runner is not None

    @property
    def has_simulator(self) -> bool:
        return self._simulator is not None or self._runner is not None

    @property
    def has_data(self) -> bool:
        return self.source is not None

    def available_targets(self) -> dict[str, str]:
        if self._targets is None:
            return {}
        result = self._call(self._targets, "targets")
        if isinstance(result, Mapping):
            return {str(k): str(v) for k, v in result.items()}
        return {str(item): "" for item in (result or [])}

    def target_hint(self) -> str:
        available = self.available_targets()
        if not available:
            return ""
        listing = ", ".join(sorted(available))
        return f" Available {self.subject_noun}s: {listing}."

    # -- capabilities -------------------------------------------------------

    def load(self, target: str) -> Loaded:
        if self._loader is None:
            raise CapabilityNotConfigured(
                f"toolkit {self.name!r} has no loader. Register one with @kit.loader — "
                "every other capability needs a way to turn a name into a config."
            )
        name = (target or "").strip()
        if not name:
            raise TargetNotFound(
                f"no {self.subject_noun} given.{self.target_hint() or ' Pass a name.'}"
            )
        try:
            result = self._loader.fn(name)
        except (TargetNotFound, CapabilityNotConfigured):
            raise
        except FileNotFoundError as exc:
            raise TargetNotFound(
                f"no {self.subject_noun} named {name!r}.{self.target_hint()}"
            ) from exc
        except Exception as exc:
            raise ToolkitError(
                f"loading {self.subject_noun} {name!r} raised {type(exc).__name__}: {exc}"
            ) from exc

        if result is None:
            raise TargetNotFound(f"no {self.subject_noun} named {name!r}.{self.target_hint()}")
        if isinstance(result, Loaded):
            return result
        return Loaded(subject=result, source="")

    def context(self, target: str, *, seed: int | None = None) -> Context:
        return Context(
            target=target,
            settings=dict(self.settings),
            seed=seed,
            max_steps=self.max_steps,
        )

    def lint(self, target: str) -> Report:
        """Rule-file checks first, then every registered validator, merged."""
        if not self.has_lint:
            raise CapabilityNotConfigured(
                f"toolkit {self.name!r} has no checks. Add [[lint.rule]] entries to your config "
                "or register a function with @kit.validator."
            )
        loaded = self.load(target)
        ctx = self.context(target)
        report = Report(subject=target, source=loaded.source)

        if self.ruleset is not None:
            report.extend(self.ruleset.run(loaded.subject))

        for validator in self._validators:
            result = self._call(validator, f"validator {validator.label}", loaded.subject, ctx)
            if result is None:
                continue
            if isinstance(result, Report):
                report.extend(result.issues)
            elif isinstance(result, Issue):
                report.issues.append(result)
            elif isinstance(result, Iterable):
                report.extend(_as_issues(result, validator.label))
            else:
                raise ToolkitError(
                    f"validator {validator.label} returned {type(result).__name__}; expected a "
                    "Report, an iterable of Issue, or None"
                )
        return report

    def replay(self, target: str, seed: int = 0, max_steps: int | None = None) -> Trace:
        """One deterministic run, with the full step-by-step record."""
        if self._runner is None:
            raise CapabilityNotConfigured(
                f"toolkit {self.name!r} has no runner. Register one with @kit.runner to make "
                "individual runs replayable."
            )
        loaded = self.load(target)
        ctx = self.context(target, seed=seed)
        if max_steps is not None:
            ctx.max_steps = max(1, int(max_steps))

        trace = self._call(
            self._runner, f"runner {self._runner.label}", loaded.subject, int(seed), ctx
        )
        if not isinstance(trace, Trace):
            raise ToolkitError(
                f"runner {self._runner.label} returned {type(trace).__name__}; expected a Trace"
            )
        trace.subject = trace.subject or target
        trace.seed = int(seed)
        return trace

    def simulate(
        self,
        target: str,
        runs: int | None = None,
        seed: int = 0,
        *,
        check_determinism: bool = False,
        determinism_sample: int = 20,
    ) -> Summary:
        """`runs` seeded runs, aggregated and judged against the thresholds."""
        if not self.has_simulator:
            raise CapabilityNotConfigured(
                f"toolkit {self.name!r} has neither a simulator nor a runner. Register one with "
                "@kit.simulator (cheap, numbers only) or @kit.runner (full trace)."
            )
        loaded = self.load(target)
        # 0 means "use the project default", not "run zero times": MCP tool
        # parameters cannot be null, so 0 is how a caller says nothing.
        count = self.default_runs if not runs else int(runs)
        count = max(1, min(count, self.max_runs))
        base = int(seed)

        batch = seeds(base, count, self.seed_policy)
        outcomes = [self._one_run(loaded.subject, target, run_seed) for run_seed in batch]
        summary = summarize(outcomes, self.thresholds, subject=target, base_seed=base)
        summary.notes.append(f"source: {loaded.source}" if loaded.source else "")
        summary.notes = [note for note in summary.notes if note]

        if check_determinism:
            sample = batch[: max(1, min(determinism_sample, count))]
            repeat = [self._one_run(loaded.subject, target, run_seed) for run_seed in sample]
            diverged = [
                index
                for index, (first, second) in enumerate(zip(outcomes, repeat, strict=False))
                if (first.outcome, dict(first.metrics)) != (second.outcome, dict(second.metrics))
            ]
            summary.deterministic = not diverged
            if diverged:
                offenders = ", ".join(str(sample[i]) for i in diverged[:5])
                summary.notes.append(
                    f"NOT DETERMINISTIC: {len(diverged)} of {len(sample)} re-runs differed "
                    f"(seeds {offenders}). Every number in this report is unreliable until that "
                    "is fixed — the usual causes are reading a clock, iterating a set, or a "
                    "module-level RNG shared between runs."
                )
            else:
                summary.notes.append(f"determinism: {len(sample)} seeds re-ran identically")

        return summary

    def _one_run(self, subject: Any, target: str, run_seed: int) -> RunOutcome:
        ctx = self.context(target, seed=run_seed)
        if self._simulator is not None:
            result = self._call(
                self._simulator, f"simulator {self._simulator.label}", subject, run_seed, ctx
            )
            if isinstance(result, RunOutcome):
                return result
            if isinstance(result, Trace):
                return _outcome_of(result, run_seed)
            raise ToolkitError(
                f"simulator {self._simulator.label} returned {type(result).__name__}; "
                "expected a RunOutcome"
            )

        assert self._runner is not None  # guarded by has_simulator
        trace = self._call(self._runner, f"runner {self._runner.label}", subject, run_seed, ctx)
        if not isinstance(trace, Trace):
            raise ToolkitError(
                f"runner {self._runner.label} returned {type(trace).__name__}; expected a Trace"
            )
        return _outcome_of(trace, run_seed)

    def query(self, sql: str, max_rows: int | None = None) -> QueryResult:
        if self.source is None:
            raise CapabilityNotConfigured(
                f"toolkit {self.name!r} has no data source. Add a [data] section to your config "
                "to enable read-only queries; every other tool works without one."
            )
        return self.source.query(sql, max_rows)

    def schema(self) -> list[TableInfo]:
        if self.source is None:
            raise CapabilityNotConfigured(f"toolkit {self.name!r} has no data source configured.")
        return self.source.schema()

    # -- internals ----------------------------------------------------------

    def _call(self, capability: Capability, label: str, *args: Any) -> Any:
        """Call user code, converting anything it raises into an actionable error.

        A traceback is the right thing for a developer at a REPL and the wrong
        thing for a model deciding what to do next. The exception type and
        message survive; the stack does not.
        """
        try:
            return capability.fn(*args)
        except ToolkitError:
            raise
        except QueryRejected:
            raise
        except Exception as exc:
            raise ToolkitError(f"{label} raised {type(exc).__name__}: {exc}") from exc


def _outcome_of(trace: Trace, run_seed: int) -> RunOutcome:
    return RunOutcome(
        seed=run_seed,
        outcome=trace.outcome or "unlabelled",
        metrics=dict(trace.metrics) or {"steps": float(len(trace.steps))},
    )


def _as_issues(items: Iterable[Any], label: str) -> list[Issue]:
    issues: list[Issue] = []
    for item in items:
        if not isinstance(item, Issue):
            raise ToolkitError(
                f"validator {label} yielded {type(item).__name__}; expected Issue objects"
            )
        issues.append(item)
    return issues
