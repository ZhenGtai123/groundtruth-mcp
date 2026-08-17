"""SQLite data source. Read-only three ways, and every one of them testable.

`mode=ro` on the URI means the file is opened without write permission at the
OS level. `PRAGMA query_only=ON` means the connection refuses writes even if
something reopened it. The progress handler aborts a statement that overruns
its budget. None of these depend on the guard's regex having been right.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .guard import QueryRejected, QueryResult, SqlGuard, TableInfo

# How often SQLite calls the progress handler, in virtual-machine
# instructions. Small enough that a hot loop notices its deadline promptly,
# large enough that the callback is not itself the bottleneck.
_PROGRESS_INTERVAL = 1000


class SqliteSource:
    """A read-only view of one SQLite file."""

    driver = "sqlite"

    def __init__(self, path: str | Path, guard: SqlGuard | None = None) -> None:
        self.path = Path(path)
        self.guard = guard or SqlGuard()

    @property
    def location(self) -> str:
        return str(self.path)

    def available(self) -> tuple[bool, str]:
        """Whether this source can serve a query, and if not, what to do about it."""
        if not self.path.exists():
            return False, (
                f"no SQLite database at {self.path}. Point `[data].url` in your config at an "
                "existing file, or run whatever populates it first."
            )
        return True, ""

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{self.path.as_posix()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        deadline = time.monotonic() + self.guard.statement_timeout_ms / 1000
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() > deadline else 0, _PROGRESS_INTERVAL
        )
        return connection

    def query(self, sql: str, max_rows: int | None = None) -> QueryResult:
        ok, reason = self.available()
        if not ok:
            raise QueryRejected(reason)

        statement = self.guard.validate(sql, max_rows)
        connection = self._connect()
        try:
            cursor = connection.execute(statement)
            raw = [dict(row) for row in cursor.fetchall()]
            columns = [description[0] for description in (cursor.description or [])]
        except sqlite3.OperationalError as exc:
            message = str(exc)
            if "interrupted" in message.lower():
                raise QueryRejected(
                    f"query exceeded the {self.guard.statement_timeout_ms}ms budget and was "
                    "cancelled. Add a narrower WHERE clause or a smaller LIMIT."
                ) from exc
            raise QueryRejected(f"SQLite rejected the query: {message}") from exc
        finally:
            connection.close()

        rows, redacted = self.guard.redact(raw)
        visible = [c for c in columns if c.lower() not in self.guard.deny_columns]
        return QueryResult(columns=visible, rows=rows, redacted=redacted, statement=statement)

    def schema(self) -> list[TableInfo]:
        """Readable tables and their columns — so nothing has to guess them."""
        ok, _ = self.available()
        if not ok:
            return []
        connection = self._connect()
        try:
            names: Sequence[Any] = connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
            tables: list[TableInfo] = []
            for row in names:
                name = row["name"]
                if self.guard.allow_tables and name.lower() not in self.guard.allow_tables:
                    continue
                columns: list[tuple[str, str]] = []
                hidden: list[str] = []
                for column in connection.execute(f'PRAGMA table_info("{name}")').fetchall():
                    if column["name"].lower() in self.guard.deny_columns:
                        hidden.append(column["name"])
                    else:
                        columns.append((column["name"], column["type"] or ""))
                tables.append(TableInfo(name=name, columns=columns, hidden=hidden))
            return tables
        finally:
            connection.close()
