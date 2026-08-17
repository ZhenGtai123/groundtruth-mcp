"""Build the demo's SQLite database from a deterministic simulation batch.

    python examples/checkout-flow/seed_data.py

Everything in `var/checkout.db` is generated: no real customer, no real card,
no real order. The `customers` table exists to demonstrate `deny_columns` —
its `email` and `card_last4` values are synthetic (`@example.invalid` is a
reserved TLD, so those addresses cannot route anywhere) and the query tool
withholds them regardless.

The rows are the recorded output of seeds 0..N, so a query result and a
`replay` of the same seed describe the same run. That is a nice property to
hand an agent: it can cross-check the aggregate against the individual.
"""

from __future__ import annotations

import random
import sqlite3
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent.parent / "src"))

from groundtruth_mcp import Context  # noqa: E402
from groundtruth_app import kit, load_flow, run_flow  # noqa: E402

RUNS = 500
FLOWS = ("standard_checkout", "express_checkout")
DB_PATH = HERE / "var" / "checkout.db"

SCHEMA = """
CREATE TABLE runs (
    seed             INTEGER NOT NULL,
    flow             TEXT    NOT NULL,
    outcome          TEXT    NOT NULL,
    steps            INTEGER NOT NULL,
    latency_ms       INTEGER NOT NULL,
    payment_attempts INTEGER NOT NULL,
    PRIMARY KEY (flow, seed)
);
CREATE TABLE events (
    id         INTEGER PRIMARY KEY,
    flow       TEXT    NOT NULL,
    seed       INTEGER NOT NULL,
    step_index INTEGER NOT NULL,
    state      TEXT    NOT NULL,
    signal     TEXT    NOT NULL,
    note       TEXT    NOT NULL DEFAULT ''
);
CREATE TABLE customers (
    seed       INTEGER PRIMARY KEY,
    label      TEXT NOT NULL,
    email      TEXT NOT NULL,
    card_last4 TEXT NOT NULL
);
CREATE INDEX events_by_run ON events (flow, seed);
"""


def main() -> int:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    connection = sqlite3.connect(DB_PATH)
    try:
        connection.executescript(SCHEMA)
        fake = random.Random(20260817)  # fixed, so the fixture is reproducible too

        for flow_name in FLOWS:
            loaded = load_flow(flow_name)
            if loaded is None:
                print(f"missing flow: {flow_name}", file=sys.stderr)
                return 1
            for seed in range(RUNS):
                ctx = Context(target=flow_name, settings=kit.settings, seed=seed, max_steps=200)
                trace = run_flow(loaded.subject, seed, ctx)
                connection.execute(
                    "INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        seed,
                        flow_name,
                        trace.outcome,
                        int(trace.metrics["steps"]),
                        int(trace.metrics["latency_ms"]),
                        int(trace.metrics["payment_attempts"]),
                    ),
                )
                connection.executemany(
                    "INSERT INTO events (flow, seed, step_index, state, signal, note) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (flow_name, seed, step.index, step.node, step.action, step.note)
                        for step in trace.steps
                    ],
                )

        connection.executemany(
            "INSERT INTO customers VALUES (?, ?, ?, ?)",
            [
                (
                    seed,
                    f"shopper-{seed:04d}",
                    f"shopper-{seed:04d}@example.invalid",
                    f"{fake.randint(0, 9999):04d}",
                )
                for seed in range(RUNS)
            ],
        )
        connection.commit()
    finally:
        connection.close()

    print(f"wrote {DB_PATH} — {RUNS} runs x {len(FLOWS)} flows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
