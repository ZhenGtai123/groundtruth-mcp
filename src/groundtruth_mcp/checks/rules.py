"""Declarative semantic checks over any structured config.

A JSON Schema tells you a field is a string. It cannot tell you that the
string names a state that no longer exists, that three states form a ring with
no exit, or that the node you set as `start` was renamed last week. Those are
the failures that actually reach production, and they are the ones an agent
editing your config is most likely to introduce, because it edits one file at
a time and the constraint lives across files.

So: twelve check types, each covering one way structured configs go wrong,
declared in a rule file instead of hand-written per project. Anything they
cannot express, you write as a normal Python validator and register it
alongside — see `Toolkit.validator`. This is a floor, not a ceiling.

    [[lint.rule]]
    type = "ref_exists"
    select = "states[].transitions[].to"
    collection = "states[]"
    key = "id"
    code = "DANGLING_TRANSITION"
    hint = "point it at an existing state id, or delete the transition"
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..contracts import Issue, normalize_severity
from . import graph as graphlib
from . import selectors

CheckFn = Callable[[Any, "Rule"], list[Issue]]

_REGISTRY: dict[str, tuple[CheckFn, tuple[str, ...], str, str]] = {}


def _check(
    name: str, *, requires: tuple[str, ...] = (), code: str = "", severity: str = "error"
):
    """Register a check type with its required fields and defaults."""

    def decorate(fn: CheckFn) -> CheckFn:
        _REGISTRY[name] = (fn, requires, code or name.upper(), severity)
        return fn

    return decorate


class RuleError(ValueError):
    """A malformed rule. Raised while loading, never while checking."""


@dataclass(frozen=True)
class Rule:
    """One configured check. `options` holds the type-specific fields."""

    type: str
    code: str
    severity: str
    hint: str = ""
    options: Mapping[str, Any] = field(default_factory=dict)

    def get(self, name: str, default: Any = None) -> Any:
        return self.options.get(name, default)

    def issue(self, message: str, path: str = "") -> Issue:
        return Issue(
            code=self.code,
            severity=self.severity,  # type: ignore[arg-type]
            message=message,
            path=path,
            hint=self.hint,
        )


@dataclass
class RuleSet:
    """An ordered list of rules, applied to a document in declaration order."""

    rules: list[Rule] = field(default_factory=list)

    @classmethod
    def from_dicts(cls, specs: Sequence[Mapping[str, Any]]) -> "RuleSet":
        return cls([_compile(index, spec) for index, spec in enumerate(specs)])

    def run(self, document: Any) -> list[Issue]:
        issues: list[Issue] = []
        for rule in self.rules:
            check_fn = _REGISTRY[rule.type][0]
            issues.extend(check_fn(document, rule))
        return issues

    def __len__(self) -> int:
        return len(self.rules)


def _compile(index: int, spec: Mapping[str, Any]) -> Rule:
    where = f"lint rule #{index + 1}"
    if not isinstance(spec, Mapping):
        raise RuleError(f"{where}: expected a table, got {type(spec).__name__}")

    rule_type = str(spec.get("type", "")).strip()
    if rule_type not in _REGISTRY:
        known = ", ".join(sorted(_REGISTRY))
        raise RuleError(f"{where}: unknown check type {rule_type!r}. Known types: {known}")

    _, required, default_code, default_severity = _REGISTRY[rule_type]
    options = {k: v for k, v in spec.items() if k not in ("type", "code", "severity", "hint")}

    missing = [name for name in required if name not in options]
    if missing:
        raise RuleError(
            f"{where} (type={rule_type}): missing required field(s) {', '.join(missing)}"
        )

    # Fail here, at load, rather than producing zero matches at check time and
    # letting a typo'd selector read as "no problems found".
    for name in ("select", "collection", "start"):
        if isinstance(options.get(name), str):
            selectors.parse(options[name])
    if isinstance(options.get("edges"), str):
        selectors.parse(options["edges"])

    try:
        severity = normalize_severity(str(spec.get("severity", default_severity)))
    except ValueError as exc:
        raise RuleError(f"{where}: {exc}") from exc

    return Rule(
        type=rule_type,
        code=str(spec.get("code") or default_code),
        severity=severity,
        hint=str(spec.get("hint", "")),
        options=options,
    )


# ---------------------------------------------------------------------------
# Field-level checks
# ---------------------------------------------------------------------------


@_check("required_fields", requires=("select", "fields"), code="MISSING_FIELD")
def _required_fields(document: Any, rule: Rule) -> list[Issue]:
    fields = [str(f) for f in rule.get("fields", [])]
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        if not isinstance(match.value, Mapping):
            issues.append(rule.issue("expected an object here", match.path))
            continue
        for name in fields:
            value = match.value.get(name)
            if value is None or value == "":
                issues.append(rule.issue(f"missing required field {name!r}", match.path))
    return issues


@_check("unique_key", requires=("select", "key"), code="DUPLICATE_KEY")
def _unique_key(document: Any, rule: Rule) -> list[Issue]:
    key = str(rule.get("key"))
    seen: dict[Any, str] = {}
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        if not isinstance(match.value, Mapping):
            continue
        value = match.value.get(key)
        if value is None:
            continue  # a required_fields rule owns that complaint
        if value in seen:
            issues.append(
                rule.issue(
                    f"duplicate {key}={value!r} (first declared at {seen[value]})",
                    match.path,
                )
            )
        else:
            seen[value] = match.path
    return issues


@_check("enum", requires=("select", "values"), code="INVALID_VALUE")
def _enum(document: Any, rule: Rule) -> list[Issue]:
    allowed = list(rule.get("values", []))
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        if match.value not in allowed:
            issues.append(
                rule.issue(
                    f"{match.value!r} is not one of {allowed}",
                    match.path,
                )
            )
    return issues


_TYPE_PREDICATES: dict[str, Callable[[Any], bool]] = {
    "string": lambda v: isinstance(v, str),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, Mapping),
}


@_check("type", requires=("select", "expect"), code="WRONG_TYPE")
def _type(document: Any, rule: Rule) -> list[Issue]:
    expect = str(rule.get("expect"))
    predicate = _TYPE_PREDICATES.get(expect)
    if predicate is None:
        known = ", ".join(sorted(_TYPE_PREDICATES))
        raise RuleError(f"type check: unknown expect={expect!r}. Known: {known}")
    return [
        rule.issue(f"expected {expect}, got {type(m.value).__name__}", m.path)
        for m in selectors.resolve(document, rule.get("select"))
        if not predicate(m.value)
    ]


@_check("range", requires=("select",), code="OUT_OF_RANGE")
def _range(document: Any, rule: Rule) -> list[Issue]:
    low = rule.get("min")
    high = rule.get("max")
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        value = match.value
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            issues.append(rule.issue(f"expected a number, got {type(value).__name__}", match.path))
            continue
        if low is not None and value < low:
            issues.append(rule.issue(f"{value} is below the minimum {low}", match.path))
        if high is not None and value > high:
            issues.append(rule.issue(f"{value} is above the maximum {high}", match.path))
    return issues


@_check("pattern", requires=("select", "regex"), code="PATTERN_MISMATCH", severity="warning")
def _pattern(document: Any, rule: Rule) -> list[Issue]:
    try:
        compiled = re.compile(str(rule.get("regex")))
    except re.error as exc:
        raise RuleError(f"pattern check: invalid regex {rule.get('regex')!r}: {exc}") from exc
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        if not isinstance(match.value, str) or not compiled.search(match.value):
            issues.append(
                rule.issue(f"{match.value!r} does not match /{compiled.pattern}/", match.path)
            )
    return issues


@_check("not_empty", requires=("select",), code="EMPTY_VALUE")
def _not_empty(document: Any, rule: Rule) -> list[Issue]:
    issues: list[Issue] = []
    for match in selectors.resolve(document, rule.get("select")):
        value = match.value
        empty = value is None or (hasattr(value, "__len__") and len(value) == 0)
        if empty:
            issues.append(rule.issue("value is empty", match.path))
    return issues


@_check("ref_exists", requires=("select", "collection", "key"), code="DANGLING_REF")
def _ref_exists(document: Any, rule: Rule) -> list[Issue]:
    key = str(rule.get("key"))
    known = {
        m.value.get(key)
        for m in selectors.resolve(document, rule.get("collection"))
        if isinstance(m.value, Mapping)
    }
    known.update(rule.get("allow", []))
    label = str(rule.get("collection")).rstrip("[]")
    return [
        rule.issue(f"{m.value!r} does not name any {label}.{key}", m.path)
        for m in selectors.resolve(document, rule.get("select"))
        if m.value is not None and m.value not in known
    ]


# ---------------------------------------------------------------------------
# Graph checks — all four share one graph builder
# ---------------------------------------------------------------------------

_GRAPH_FIELDS = ("collection", "key", "edges")


def _graph_of(document: Any, rule: Rule) -> tuple[graphlib.Graph, dict[str, str]]:
    """Build the graph a rule describes, plus node_key -> config path."""
    key_field = str(rule.get("key"))
    edge_expr = str(rule.get("edges"))
    nodes: list[str] = []
    paths: dict[str, str] = {}
    pairs: list[tuple[str, str]] = []

    for match in selectors.resolve(document, rule.get("collection")):
        if not isinstance(match.value, Mapping):
            continue
        source = match.value.get(key_field)
        if not isinstance(source, str):
            continue
        nodes.append(source)
        paths.setdefault(source, match.path)
        for edge in selectors.resolve(match.value, edge_expr):
            if isinstance(edge.value, str):
                pairs.append((source, edge.value))

    return graphlib.build(pairs, nodes), paths


@_check("reachable", requires=_GRAPH_FIELDS + ("start",), code="UNREACHABLE_NODE")
def _reachable(document: Any, rule: Rule) -> list[Issue]:
    graph, paths = _graph_of(document, rule)
    start = selectors.resolve_one(document, str(rule.get("start")))
    if not isinstance(start, str) or start not in graph:
        return [
            rule.issue(
                f"start node {start!r} is not declared in {rule.get('collection')} — "
                "every other node is unreachable by definition",
                str(rule.get("start")),
            )
        ]
    return [
        rule.issue(f"{node!r} cannot be reached from {start!r}", paths.get(node, ""))
        for node in graphlib.unreachable(graph, start)
    ]


@_check("no_dead_end", requires=_GRAPH_FIELDS, code="DEAD_END")
def _no_dead_end(document: Any, rule: Rule) -> list[Issue]:
    graph, paths = _graph_of(document, rule)
    terminal_field = rule.get("terminal_field")
    terminal_values = set(rule.get("terminal_values", []))
    terminals: list[str] = []
    if terminal_field:
        key_field = str(rule.get("key"))
        for match in selectors.resolve(document, rule.get("collection")):
            if not isinstance(match.value, Mapping):
                continue
            if match.value.get(terminal_field) in terminal_values:
                node_key = match.value.get(key_field)
                if isinstance(node_key, str):
                    terminals.append(node_key)
    return [
        rule.issue(
            f"{node!r} has no outgoing edge and is not marked terminal — a run that "
            "arrives here stops with no result",
            paths.get(node, ""),
        )
        for node in graphlib.dead_ends(graph, terminals)
    ]


@_check("no_self_loop", requires=_GRAPH_FIELDS, code="SELF_LOOP", severity="warning")
def _no_self_loop(document: Any, rule: Rule) -> list[Issue]:
    graph, paths = _graph_of(document, rule)
    return [
        rule.issue(f"{node!r} transitions to itself", paths.get(node, ""))
        for node in graphlib.self_loops(graph)
    ]


@_check("no_cycle", requires=_GRAPH_FIELDS, code="CYCLE", severity="warning")
def _no_cycle(document: Any, rule: Rule) -> list[Issue]:
    graph, paths = _graph_of(document, rule)
    allowed = {frozenset(group) for group in rule.get("allow", [])}
    issues: list[Issue] = []
    for component in graphlib.cycles(graph):
        if frozenset(component) in allowed:
            continue  # an intentional retry loop, declared as such in the rule
        issues.append(
            rule.issue(
                "cycle: " + " -> ".join(component) + f" -> {component[0]}",
                paths.get(component[0], ""),
            )
        )
    return issues


def known_check_types() -> list[str]:
    return sorted(_REGISTRY)
