"""Turning results into the text an agent actually reads.

This is not cosmetics. A tool result is the entire sensory input the model has
about what your project just did, and it is competing for room with the rest
of the conversation. Three rules hold throughout:

  * The verdict comes first. `ok=False errors=3` in line one means a model
    that reads nothing else still knows whether to keep going.
  * Every line carries a location or a number. Prose that restates the
    verdict costs context and adds nothing.
  * Nothing is silently dropped. When output hits the cap it says so, and
    says how to ask for less.
"""

from __future__ import annotations

import json
from typing import Iterable, Sequence

from .budget import fence_untrusted, truncate
from .contracts import Report, Trace
from .data.guard import QueryResult, TableInfo, format_schema
from .stats import Summary, format_distribution


def render_report(report: Report, *, limit: int) -> str:
    head = [
        f"{report.subject}: {'OK' if report.ok else 'BLOCKED'}  "
        f"errors={len(report.errors)} warnings={len(report.warnings)} infos={len(report.infos)}"
    ]
    if report.source:
        head.append(f"source: {report.source}")
    head.append("")

    if not report.issues:
        head.append("No issues found.")
        return truncate("\n".join(head), limit)

    body: list[str] = []
    for title, bucket in (
        ("ERRORS — these block", report.errors),
        ("WARNINGS", report.warnings),
        ("INFO", report.infos),
    ):
        if not bucket:
            continue
        body.append(f"-- {title} ({len(bucket)}) --")
        body.extend(issue.format() for issue in sorted(bucket, key=lambda i: (i.code, i.path)))
        body.append("")

    return truncate(
        "\n".join(head + body),
        limit,
        advice="fix the errors listed above and re-run; the remaining issues are the same kinds",
    )


def render_trace(trace: Trace, *, limit: int) -> str:
    lines = [
        f"{trace.subject}  seed={trace.seed}  outcome={trace.outcome or '(none)'}  "
        f"steps={len(trace.steps)}  fingerprint={trace.fingerprint()}",
    ]
    if trace.metrics:
        lines.append(
            "metrics: " + "  ".join(f"{k}={_number(v)}" for k, v in sorted(trace.metrics.items()))
        )
    if trace.truncated:
        lines.append("note: the run hit its step budget and was cut short")
    lines.append("")
    lines.append("-- TRACE --")
    lines.extend(step.format() for step in trace.steps)

    if trace.issues:
        lines.append("")
        lines.append("-- ISSUES RAISED DURING THE RUN --")
        lines.extend(issue.format() for issue in trace.issues)

    return truncate(
        "\n".join(lines),
        limit,
        advice="replay with a smaller max_steps, or read the metrics line instead of the path",
    )


def render_summary(summary: Summary, *, limit: int) -> str:
    verdict = "PASS" if summary.passed else "FAIL"
    lines = [
        f"{summary.subject}: {verdict}  runs={summary.runs}  base_seed={summary.base_seed}  "
        f"fingerprint={summary.fingerprint}",
        "",
        "-- OUTCOMES --",
        *format_distribution(summary),
    ]

    if summary.metrics:
        lines.append("")
        lines.append("-- METRICS (mean / p50 / p95 / max) --")
        for name in sorted(summary.metrics):
            stats = summary.metrics[name]
            lines.append(
                f"  {name}: {_number(stats['mean'])} / {_number(stats['p50'])} / "
                f"{_number(stats['p95'])} / {_number(stats['max'])}"
            )

    lines.append("")
    if summary.gate:
        lines.append("-- THRESHOLDS --")
        lines.extend(f"  {result.format()}" for result in summary.gate)
    else:
        lines.append("-- THRESHOLDS --")
        lines.append("  (none declared — this run reports, it does not gate)")

    if summary.notes:
        lines.append("")
        lines.extend(f"note: {note}" for note in summary.notes)

    lines.append("")
    lines.append(
        "Same base_seed and run count always produce the same fingerprint. A changed "
        "fingerprint means behaviour changed, even when every threshold still passes."
    )
    return truncate("\n".join(lines), limit)


def render_query(result: QueryResult, *, limit: int) -> str:
    payload = json.dumps(result.rows, ensure_ascii=False, indent=2, default=str)
    head = f"{result.row_count} row(s)."
    if result.redacted:
        head += f" Withheld column(s): {', '.join(result.redacted)}."
    return truncate(
        f"{head}\n{fence_untrusted(payload)}",
        limit,
        advice="select fewer columns, add a WHERE clause, or lower max_rows",
    )


def render_schema(tables: Sequence[TableInfo], *, limit: int) -> str:
    return truncate(
        "Readable tables (everything else is not exposed):\n" + format_schema(tables), limit
    )


def render_targets(targets: dict[str, str], noun: str, *, limit: int) -> str:
    if not targets:
        return f"No {noun}s are registered."
    lines = [f"{len(targets)} {noun}(s):"]
    for name in sorted(targets):
        description = targets[name]
        lines.append(f"  {name}" + (f" — {description}" if description else ""))
    return truncate("\n".join(lines), limit)


def _number(value: float) -> str:
    """Two decimals for fractions, no decimals for whole counts."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}"


def join_nonempty(parts: Iterable[str]) -> str:
    return "\n".join(part for part in parts if part)
