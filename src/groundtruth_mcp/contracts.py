"""The shared vocabulary every capability speaks.

Four dataclasses carry everything that moves between your project, this
package, and the agent: an `Issue` (something is wrong, here, do this), a
`Report` (a bag of issues about one subject), a `Trace` (what actually
happened during one deterministic run), and a `RunOutcome` (the numeric
residue of one run, cheap enough to keep ten thousand of).

They are deliberately dumb — no behaviour beyond formatting and a couple of
derived views. The whole point of this package is that your project keeps its
own logic; these types only give it a shape the agent-facing layer can render
consistently, so a lint issue from a YAML rule and a lint issue from your
hand-written checker print identically and sort together.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator, Literal, Mapping

Severity = Literal["error", "warning", "info"]

# Errors first: an agent reading a truncated report should hit the blocking
# problems before it runs out of budget on advisories.
SEVERITY_ORDER: dict[str, int] = {"error": 0, "warning": 1, "info": 2}


def normalize_severity(value: str) -> Severity:
    """Coerce user-supplied severity strings, loudly rejecting typos.

    Rule files are hand-edited; `severity = "warn"` should fail at load time
    with a usable message, not silently become an error-level issue.
    """
    lowered = (value or "").strip().lower()
    if lowered not in SEVERITY_ORDER:
        raise ValueError(
            f"unknown severity {value!r} — use one of: error, warning, info"
        )
    return lowered  # type: ignore[return-value]


@dataclass(frozen=True)
class Issue:
    """One actionable finding, anchored to a location in the subject.

    `path` is what makes a lint result usable by an agent rather than merely
    true: "something references a state that doesn't exist" sends it grepping,
    `states[3].transitions[1].to` sends it straight to the line to edit.
    """

    code: str
    severity: Severity
    message: str
    path: str = ""
    hint: str = ""

    def format(self) -> str:
        location = f" {self.path}" if self.path else ""
        hint = f"\n    fix: {self.hint}" if self.hint else ""
        return f"[{self.code}]{location}  {self.message}{hint}"


@dataclass
class Report:
    """Issues about one subject, plus where that subject came from."""

    subject: str = ""
    source: str = ""
    issues: list[Issue] = field(default_factory=list)

    def add(
        self,
        code: str,
        severity: str,
        message: str,
        path: str = "",
        hint: str = "",
    ) -> None:
        self.issues.append(
            Issue(
                code=code,
                severity=normalize_severity(severity),
                message=message,
                path=path,
                hint=hint,
            )
        )

    def extend(self, issues: Iterable[Issue]) -> None:
        self.issues.extend(issues)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def infos(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "info"]

    @property
    def ok(self) -> bool:
        """True when nothing blocks. Warnings and infos never block."""
        return not self.errors

    def sorted_issues(self) -> list[Issue]:
        return sorted(
            self.issues,
            key=lambda i: (SEVERITY_ORDER[i.severity], i.code, i.path),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "source": self.source,
            "ok": self.ok,
            "counts": {
                "error": len(self.errors),
                "warning": len(self.warnings),
                "info": len(self.infos),
            },
            "issues": [
                {
                    "code": i.code,
                    "severity": i.severity,
                    "message": i.message,
                    "path": i.path,
                    "hint": i.hint,
                }
                for i in self.sorted_issues()
            ],
        }


@dataclass(frozen=True)
class TraceStep:
    """One observable step of a run.

    `state` is a snapshot, not a reference — a runner that mutates a dict in
    place and appends the same object every step produces a trace where every
    step shows the final state, which reads as a bug in your engine when it is
    only a bug in your instrumentation. `Trace.step()` copies for you.
    """

    index: int
    node: str
    action: str = ""
    note: str = ""
    state: Mapping[str, Any] = field(default_factory=dict)

    def format(self) -> str:
        action = f" --{self.action}-->" if self.action else ""
        note = f"  # {self.note}" if self.note else ""
        return f"{self.index:>3}. {self.node}{action}{note}"


@dataclass
class Trace:
    """The full record of one seeded run: what happened, in order, and why.

    This is the artifact that replaces guessing. An agent that changed a retry
    policy does not have to reason about whether the change was correct — it
    replays seed 7 and reads the twelve steps that actually executed.
    """

    subject: str = ""
    seed: int = 0
    steps: list[TraceStep] = field(default_factory=list)
    outcome: str = ""
    metrics: dict[str, float] = field(default_factory=dict)
    issues: list[Issue] = field(default_factory=list)
    truncated: bool = False

    def step(
        self,
        node: str,
        action: str = "",
        note: str = "",
        state: Mapping[str, Any] | None = None,
    ) -> None:
        self.steps.append(
            TraceStep(
                index=len(self.steps),
                node=node,
                action=action,
                note=note,
                state=dict(state) if state else {},
            )
        )

    def __iter__(self) -> Iterator[TraceStep]:
        return iter(self.steps)

    def __len__(self) -> int:
        return len(self.steps)

    def fingerprint(self) -> str:
        """Stable hash of the visited path and outcome.

        Two runs of the same seed must produce the same fingerprint. Metrics
        are excluded on purpose: a float that differs in the 15th decimal
        across platforms would make an otherwise-identical path look changed.
        """
        payload = json.dumps(
            {
                "outcome": self.outcome,
                "path": [(s.node, s.action) for s in self.steps],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "seed": self.seed,
            "outcome": self.outcome,
            "steps": [
                {
                    "index": s.index,
                    "node": s.node,
                    "action": s.action,
                    "note": s.note,
                    "state": dict(s.state),
                }
                for s in self.steps
            ],
            "metrics": dict(self.metrics),
            "truncated": self.truncated,
            "fingerprint": self.fingerprint(),
            "issues": [
                {"code": i.code, "severity": i.severity, "message": i.message}
                for i in self.issues
            ],
        }


@dataclass(frozen=True)
class RunOutcome:
    """One run boiled down to a label and some numbers.

    A simulator returns ten thousand of these, so it holds no trace, no
    objects, no references into your engine's state — just what the statistics
    layer needs to aggregate.
    """

    seed: int
    outcome: str
    metrics: Mapping[str, float] = field(default_factory=dict)


class ToolkitError(Exception):
    """Base class for every failure this package reports back to a caller.

    Every message must say what was wrong *and* what to pass instead. An agent
    cannot act on `KeyError: 'states'`; it can act on "no target named
    'chekout'; available targets: standard_checkout, broken_checkout".
    """


class TargetNotFound(ToolkitError):
    pass


class CapabilityNotConfigured(ToolkitError):
    pass
