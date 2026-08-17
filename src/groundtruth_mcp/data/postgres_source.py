"""PostgreSQL data source. Same guard, same shape, `READ ONLY` as the boundary.

Optional: `pip install groundtruth-mcp[postgres]`. Import failures surface as
an actionable tool result, not a crash at server start — a project that only
uses the SQLite source should never be blocked by a missing psycopg.
"""

from __future__ import annotations

import re
from typing import Any

from .guard import QueryRejected, QueryResult, SqlGuard, TableInfo

# SQLAlchemy-style URLs (postgresql+psycopg://) are what most projects already
# have in their env; psycopg wants the plain scheme.
_DRIVER_SUFFIX = re.compile(r"^postgresql\+\w+://")


class PostgresSource:
    """A read-only view of one PostgreSQL database."""

    driver = "postgres"

    def __init__(self, url: str, guard: SqlGuard | None = None, schema_name: str = "public") -> None:
        self.url = _DRIVER_SUFFIX.sub("postgresql://", url or "")
        self.guard = guard or SqlGuard()
        self.schema_name = schema_name

    @property
    def location(self) -> str:
        """The URL with credentials stripped. Never log or return the raw DSN."""
        return re.sub(r"://[^@/]*@", "://***@", self.url)

    def available(self) -> tuple[bool, str]:
        if not self.url:
            return False, (
                "no PostgreSQL URL configured. Set `[data].url` in your config, or point it at "
                "an environment variable with `url_env = \"DATABASE_URL\"`."
            )
        try:
            import psycopg  # noqa: F401
        except ImportError:
            return False, (
                "psycopg is not installed. Run `pip install groundtruth-mcp[postgres]` to enable "
                "the PostgreSQL data source; every other tool works without it."
            )
        return True, ""

    def _connect(self):
        import psycopg

        connection = psycopg.connect(self.url, autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute(f"SET LOCAL statement_timeout = {self.guard.statement_timeout_ms}")
            # The actual boundary. Postgres now refuses any data-modifying
            # statement for this transaction, whatever the regex let through.
            cursor.execute("SET TRANSACTION READ ONLY")
        return connection

    def query(self, sql: str, max_rows: int | None = None) -> QueryResult:
        ok, reason = self.available()
        if not ok:
            raise QueryRejected(reason)

        statement = self.guard.validate(sql, max_rows)
        try:
            connection = self._connect()
        except Exception as exc:  # noqa: BLE001 - connection errors are a tool result, not a crash
            raise QueryRejected(
                f"could not connect to {self.location}: {type(exc).__name__}: {exc}"
            ) from exc

        try:
            with connection.cursor() as cursor:
                cursor.execute(statement)
                columns = [description.name for description in (cursor.description or [])]
                raw = [dict(zip(columns, record)) for record in cursor.fetchall()]
        except Exception as exc:  # noqa: BLE001
            raise QueryRejected(f"PostgreSQL rejected the query: {exc}") from exc
        finally:
            connection.rollback()
            connection.close()

        rows, redacted = self.guard.redact(raw)
        visible = [c for c in columns if c.lower() not in self.guard.deny_columns]
        return QueryResult(columns=visible, rows=rows, redacted=redacted, statement=statement)

    def schema(self) -> list[TableInfo]:
        ok, _ = self.available()
        if not ok:
            return []
        try:
            connection = self._connect()
        except Exception:  # noqa: BLE001
            return []
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT table_name, column_name, data_type FROM information_schema.columns "
                    "WHERE table_schema = %s ORDER BY table_name, ordinal_position",
                    (self.schema_name,),
                )
                grouped: dict[str, list[tuple[str, str]]] = {}
                hidden: dict[str, list[str]] = {}
                for table, column, kind in cursor.fetchall():
                    if self.guard.allow_tables and table.lower() not in self.guard.allow_tables:
                        continue
                    if column.lower() in self.guard.deny_columns:
                        hidden.setdefault(table, []).append(column)
                    else:
                        grouped.setdefault(table, []).append((column, kind))
        finally:
            connection.rollback()
            connection.close()

        return [
            TableInfo(name=table, columns=columns, hidden=hidden.get(table, []))
            for table, columns in sorted(grouped.items())
        ]
