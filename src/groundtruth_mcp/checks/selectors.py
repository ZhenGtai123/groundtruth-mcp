"""A deliberately small path language for pointing at parts of a config.

Rule files need to say "every `to` field of every transition of every state".
JSONPath can say that and forty other things; forty other things is exactly
the problem, because a rule file is read by whoever inherits your repo at 2am
and every extra operator is another thing they have to look up.

The whole grammar:

    states                       a key
    states[]                     every element of a list
    states[].transitions[].to    keep walking, flattening as you go
    policy.max_retries           nested objects

Every match carries the concrete path it was found at — `states[3].
transitions[1].to` — because that string is the difference between a lint
result an agent can act on and one it has to go hunting for.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

_SEGMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)(\[\])?$")


@dataclass(frozen=True)
class Match:
    """A value found by a selector, and where it lives."""

    path: str
    value: Any
    parent: Any = None
    key: str | int | None = None


class SelectorError(ValueError):
    """Raised at rule-load time for a malformed selector, never at check time."""


def parse(expr: str) -> list[tuple[str, bool]]:
    """`"states[].transitions[].to"` -> segments, each flagged as iterating or not.

    `[("states", True), ("transitions", True), ("to", False)]`
    """
    cleaned = (expr or "").strip()
    if not cleaned:
        raise SelectorError("empty selector")
    segments: list[tuple[str, bool]] = []
    for raw in cleaned.split("."):
        match = _SEGMENT_RE.match(raw.strip())
        if not match:
            raise SelectorError(
                f"bad selector segment {raw!r} in {expr!r} — expected `name` or `name[]` "
                "(identifier characters, optional trailing [] to iterate a list)"
            )
        segments.append((match.group(1), bool(match.group(2))))
    return segments


def resolve(document: Any, expr: str, *, root: str = "") -> list[Match]:
    """Every value `expr` selects in `document`.

    Missing keys yield nothing rather than raising — "no transitions declared"
    is a legitimate shape that a `required_fields` rule should report, not an
    exception that takes down the whole lint run.
    """
    return list(_walk(document, parse(expr), root))


def _walk(node: Any, segments: list[tuple[str, bool]], prefix: str) -> Iterator[Match]:
    if not segments:
        return
    (name, iterate), rest = segments[0], segments[1:]

    if not isinstance(node, dict) or name not in node:
        return
    value = node[name]
    path = f"{prefix}.{name}" if prefix else name

    if iterate:
        if not isinstance(value, list):
            return
        for index, element in enumerate(value):
            element_path = f"{path}[{index}]"
            if rest:
                yield from _walk(element, rest, element_path)
            else:
                yield Match(path=element_path, value=element, parent=value, key=index)
        return

    if rest:
        yield from _walk(value, rest, path)
    else:
        yield Match(path=path, value=value, parent=node, key=name)


def resolve_one(document: Any, expr: str, *, default: Any = None) -> Any:
    """First match's value, or `default`. For scalar config lookups like `start`."""
    matches = resolve(document, expr)
    return matches[0].value if matches else default


def parent_path(path: str) -> str:
    """`"states[3].transitions[1].to"` -> `"states[3].transitions[1]"`."""
    head, sep, _ = path.rpartition(".")
    return head if sep else path
