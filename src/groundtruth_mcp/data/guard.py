"""Read-only enforcement for the query tool, in two layers that are not equals.

Layer one is a regex: single statement, must start with SELECT or WITH, no
write verbs, tables must be on the allowlist. It exists to fail fast with a
message the caller can act on — "your query starts with UPDATE" beats a
database error the model has to decode. It is **not** the security boundary.
A keyword blocklist over text is always one case away from wrong; the classic
demonstration is

    SELECT * INTO audit_copy FROM users

which starts with SELECT, contains no denied verb, and creates a table.

Layer two is the boundary: the connection itself is opened read-only and the
statement runs inside a read-only transaction, so the *database* refuses to
write regardless of what got past the regex. SQLite gets `mode=ro` plus
`PRAGMA query_only`; PostgreSQL gets a `READ ONLY` transaction. Both also get
a statement timeout, because a runaway cross join should end in an error
rather than a hung tool call.

The rule this encodes: when the datastore can enforce a constraint
authoritatively, never let application-level text parsing be the only thing
enforcing it. The parser is user experience; the transaction mode is the
guarantee.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

_WRITE_KEYWORDS = re.compile(
    r"\b(insert|update|delete|drop|alter|truncate|grant|revoke|create|replace|execute|"
    r"call|merge|copy|vacuum|reindex|lock|refresh|comment|attach|detach|pragma|do|into|"
    r"listen|notify|prepare|deallocate|discard|cluster|checkpoint|unlisten|set|reset)\b",
    re.IGNORECASE,
)

# Table references: what follows FROM or JOIN, minus subqueries (an opening
# paren) and CTE names (resolved separately below).
_TABLE_RE = re.compile(r"\b(?:from|join)\s+([A-Za-z_][A-Za-z0-9_.\"]*)", re.IGNORECASE)
_CTE_RE = re.compile(r"\b(?:with|,)\s+([A-Za-z_][A-Za-z0-9_]*)\s+as\s*\(", re.IGNORECASE)
_LIMIT_RE = re.compile(r"\blimit\b", re.IGNORECASE)


class QueryRejected(ValueError):
    """The query never reached the database. The message says why, and what to send instead."""


@dataclass(frozen=True)
class SqlGuard:
    """Policy for one data source."""

    allow_tables: frozenset[str] = frozenset()
    deny_columns: frozenset[str] = frozenset()
    max_rows: int = 100
    row_ceiling: int = 1000
    statement_timeout_ms: int = 5000

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> SqlGuard:
        return cls(
            allow_tables=frozenset(str(t).lower() for t in config.get("allow_tables", [])),
            deny_columns=frozenset(str(c).lower() for c in config.get("deny_columns", [])),
            max_rows=int(config.get("max_rows", 100)),
            row_ceiling=int(config.get("row_ceiling", 1000)),
            statement_timeout_ms=int(config.get("statement_timeout_ms", 5000)),
        )

    def clamp_rows(self, requested: int | None) -> int:
        value = self.max_rows if requested is None else int(requested)
        return max(1, min(value, self.row_ceiling))

    def validate(self, sql: str, max_rows: int | None = None) -> str:
        """Return the statement to execute, or raise `QueryRejected`."""
        cleaned = (sql or "").strip()
        if not cleaned:
            raise QueryRejected("empty query — send one SELECT or WITH statement")

        body = cleaned[:-1].rstrip() if cleaned.endswith(";") else cleaned
        if ";" in body:
            raise QueryRejected(
                "multiple statements are not allowed (found ';' before the end). "
                "Send exactly one SELECT or WITH statement."
            )

        lowered = body.lstrip().lower()
        if not (lowered.startswith("select") or lowered.startswith("with")):
            raise QueryRejected(
                f"only read queries are allowed; this one starts with {body.split()[0]!r}. "
                "The statement must begin with SELECT or WITH."
            )

        denied = _WRITE_KEYWORDS.search(body)
        if denied:
            raise QueryRejected(
                f"query contains the disallowed keyword {denied.group(0)!r}. This is a blunt "
                "whole-word scan that also matches inside string literals — if it caught a "
                "legitimate identifier, rename the alias or fetch the column without filtering "
                "on that literal."
            )

        self._check_tables(body)

        limit = self.clamp_rows(max_rows)
        if not _LIMIT_RE.search(body):
            body = f"{body}\nLIMIT {limit}"
        return body

    def _check_tables(self, body: str) -> None:
        if not self.allow_tables:
            return
        cte_names = {name.lower() for name in _CTE_RE.findall(body)}
        for raw in _TABLE_RE.findall(body):
            name = raw.strip('"').lower()
            bare = name.rsplit(".", 1)[-1]
            if bare in cte_names or bare in self.allow_tables:
                continue
            allowed = ", ".join(sorted(self.allow_tables))
            raise QueryRejected(
                f"table {raw!r} is not exposed by this data source. Readable tables: {allowed}."
            )

    def redact(self, rows: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
        """Drop denied columns from a result set.

        Unlike the keyword scan, this one *is* enforcement: the values never
        enter the string this tool returns, so they never enter the model's
        context. It is how `email` or `card_last4` stay out of a transcript
        even when someone writes `SELECT *`.
        """
        if not self.deny_columns:
            return [dict(row) for row in rows], []
        removed: set[str] = set()
        cleaned: list[dict[str, Any]] = []
        for row in rows:
            kept = {}
            for key, value in row.items():
                if key.lower() in self.deny_columns:
                    removed.add(key)
                    continue
                kept[key] = value
            cleaned.append(kept)
        return cleaned, sorted(removed)


@dataclass
class QueryResult:
    columns: list[str] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    redacted: list[str] = field(default_factory=list)
    statement: str = ""

    @property
    def row_count(self) -> int:
        return len(self.rows)


@dataclass(frozen=True)
class TableInfo:
    name: str
    columns: list[tuple[str, str]]  # (column, declared type)
    hidden: list[str] = field(default_factory=list)

    def format(self) -> str:
        body = ", ".join(f"{name} {kind}".strip() for name, kind in self.columns)
        hidden = f"  [redacted: {', '.join(self.hidden)}]" if self.hidden else ""
        return f"{self.name}({body}){hidden}"


def format_schema(tables: Iterable[TableInfo]) -> str:
    listing = list(tables)
    if not listing:
        return "(no readable tables)"
    return "\n".join(table.format() for table in listing)
