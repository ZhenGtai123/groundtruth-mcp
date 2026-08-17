"""The query boundary, tested at both layers — including the one that matters.

The regex tests below are about error quality. The two that matter for safety
are `test_read_only_connection_refuses_a_write` and
`test_select_into_is_stopped_by_the_database_not_the_regex`: they go around the
guard entirely and confirm SQLite itself refuses, which is the property the
whole design rests on.
"""

from __future__ import annotations

import sqlite3

import pytest

from groundtruth_mcp import QueryRejected, SqlGuard, SqliteSource


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "demo.db"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE runs (seed INTEGER, outcome TEXT, email TEXT);
        INSERT INTO runs VALUES (1, 'success', 'a@example.invalid'),
                                (2, 'abandoned', 'b@example.invalid');
        CREATE TABLE secrets (token TEXT);
        INSERT INTO secrets VALUES ('nope');
        """
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def source(db):
    return SqliteSource(
        db,
        SqlGuard(
            allow_tables=frozenset({"runs"}),
            deny_columns=frozenset({"email"}),
            max_rows=10,
        ),
    )


def test_reads_rows_and_appends_a_limit(source):
    result = source.query("SELECT seed, outcome FROM runs")
    assert result.row_count == 2
    assert result.statement.endswith("LIMIT 10")


def test_denied_columns_never_reach_the_result(source):
    result = source.query("SELECT * FROM runs")
    assert result.redacted == ["email"]
    assert all("email" not in row for row in result.rows)
    assert "email" not in result.columns


@pytest.mark.parametrize(
    "sql, fragment",
    [
        ("", "empty query"),
        ("DELETE FROM runs", "starts with"),
        ("SELECT 1; DROP TABLE runs", "multiple statements"),
        ("SELECT * INTO copy FROM runs", "INTO"),
        ("UPDATE runs SET outcome = 'x'", "starts with"),
        ("SELECT * FROM secrets", "not exposed"),
    ],
)
def test_rejections_explain_themselves(source, sql, fragment):
    with pytest.raises(QueryRejected, match=fragment):
        source.query(sql)


def test_cte_names_are_not_mistaken_for_unlisted_tables(source):
    result = source.query("WITH recent AS (SELECT seed FROM runs) SELECT * FROM recent")
    assert result.row_count == 2


def test_max_rows_is_clamped_to_the_ceiling():
    guard = SqlGuard(max_rows=100, row_ceiling=500)
    assert guard.clamp_rows(None) == 100  # the project default
    assert guard.clamp_rows(10_000) == 500  # the ceiling, whatever the caller asks for
    assert guard.clamp_rows(0) == 1
    # A ceiling below the default wins: the ceiling is the hard bound.
    assert SqlGuard(max_rows=100, row_ceiling=50).clamp_rows(None) == 50


def test_missing_database_is_a_message_not_a_crash(tmp_path):
    source = SqliteSource(tmp_path / "absent.db")
    ok, reason = source.available()
    assert not ok and "does not" not in reason  # phrased as an instruction, not a complaint
    with pytest.raises(QueryRejected, match="no SQLite database"):
        source.query("SELECT 1")
    assert source.schema() == []


def test_schema_lists_readable_tables_and_names_withheld_columns(source):
    tables = source.schema()
    assert [t.name for t in tables] == ["runs"]  # `secrets` is not exposed at all
    assert tables[0].hidden == ["email"]
    assert "email" not in tables[0].format().split("(")[1].split(")")[0]


# -- the actual boundary ----------------------------------------------------


def test_read_only_connection_refuses_a_write(source):
    """Bypass the guard completely; the connection itself must still refuse."""
    connection = source._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("INSERT INTO runs VALUES (3, 'x', 'y')")
    finally:
        connection.close()


def test_select_into_is_stopped_by_the_database_not_the_regex(source):
    """The canonical blocklist bypass, run directly against the connection.

    SQLite spells it `CREATE TABLE ... AS SELECT`; either way the point holds —
    a statement that reads like a SELECT and writes like a CREATE gets past a
    keyword scan and dies at the database.
    """
    connection = source._connect()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE copy AS SELECT * FROM runs")
    finally:
        connection.close()
