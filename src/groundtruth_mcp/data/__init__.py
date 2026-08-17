"""Read-only data access with a boundary the database enforces, not the parser."""

from .guard import QueryRejected, QueryResult, SqlGuard, TableInfo, format_schema
from .postgres_source import PostgresSource
from .sqlite_source import SqliteSource

__all__ = [
    "PostgresSource",
    "QueryRejected",
    "QueryResult",
    "SqlGuard",
    "SqliteSource",
    "TableInfo",
    "format_schema",
]
