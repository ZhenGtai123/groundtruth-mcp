"""The same capabilities, on a command line, for CI and for humans.

Every agent-facing tool has a CLI twin that reads the identical config and
calls the identical toolkit. That is the whole trick behind making a
simulation into a merge gate: CI runs `groundtruth simulate --gate`, the agent
runs the `simulate` tool, and neither can drift from the other because there
is nothing to drift — one threshold list, one code path, two front doors.

Exit codes, since CI reads those and not prose:

    0  clean — no errors, thresholds satisfied
    1  findings — lint errors, a threshold outside its band, non-determinism
    2  the tool could not run — bad config, missing capability, rejected query
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from .config import ConfigError, ProjectConfig
from .contracts import ToolkitError
from .data.guard import QueryRejected
from .render import (
    render_query,
    render_report,
    render_schema,
    render_summary,
    render_targets,
    render_trace,
)
from .toolkit import Toolkit

EXIT_OK = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="groundtruth",
        description="Run your project's own checks, replays, simulations and queries — "
        "the same ones your coding agent calls over MCP.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="path to groundtruth.toml (default: search upward from the working directory)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("targets", help="list the configs this project exposes")
    sub.add_parser("schema", help="show the tables the query tool can read")
    sub.add_parser("serve", help="run the MCP server over stdio")
    sub.add_parser("doctor", help="check the config and report what is wired up")

    lint = sub.add_parser("lint", help="check one config for semantic errors")
    lint.add_argument("target")
    lint.add_argument("--json", action="store_true", help="machine-readable output")

    replay = sub.add_parser("replay", help="run one config once, with a fixed seed")
    replay.add_argument("target")
    replay.add_argument("--seed", type=int, default=0)
    replay.add_argument("--max-steps", type=int, default=0)
    replay.add_argument("--json", action="store_true")

    simulate = sub.add_parser("simulate", help="run a seeded batch and report the distribution")
    simulate.add_argument("target")
    simulate.add_argument("--runs", type=int, default=0, help="0 = the project default")
    simulate.add_argument("--seed", type=int, default=0, help="base seed; run i uses seed+i")
    simulate.add_argument(
        "--gate",
        action="store_true",
        help="exit 1 when a threshold is missed — this is the flag CI uses",
    )
    simulate.add_argument(
        "--check-determinism",
        action="store_true",
        help="re-run a sample of seeds and verify they reproduce exactly",
    )
    simulate.add_argument("--json", action="store_true")

    query = sub.add_parser("query", help="run one read-only SELECT")
    query.add_argument("sql")
    query.add_argument("--max-rows", type=int, default=0)
    query.add_argument("--json", action="store_true")

    return parser


def _load(args: argparse.Namespace) -> tuple[ProjectConfig, Toolkit]:
    config = ProjectConfig.load(args.config) if args.config else ProjectConfig.discover()
    return config, config.build()


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)

    try:
        config, kit = _load(args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ToolkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    limit = kit.max_output_chars

    try:
        if args.command == "targets":
            print(render_targets(kit.available_targets(), kit.subject_noun, limit=limit))
            return EXIT_OK

        if args.command == "doctor":
            return _doctor(config, kit)

        if args.command == "serve":
            from .server import serve

            serve(kit)
            return EXIT_OK

        if args.command == "schema":
            print(render_schema(kit.schema(), limit=limit))
            return EXIT_OK

        if args.command == "lint":
            report = kit.lint(args.target)
            print(
                json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
                if args.json
                else render_report(report, limit=limit)
            )
            return EXIT_FINDINGS if report.errors else EXIT_OK

        if args.command == "replay":
            trace = kit.replay(args.target, seed=args.seed, max_steps=args.max_steps or None)
            print(
                json.dumps(trace.to_dict(), ensure_ascii=False, indent=2)
                if args.json
                else render_trace(trace, limit=limit)
            )
            return EXIT_OK

        if args.command == "simulate":
            summary = kit.simulate(
                args.target,
                runs=args.runs or None,
                seed=args.seed,
                check_determinism=args.check_determinism,
            )
            print(
                json.dumps(summary.to_dict(), ensure_ascii=False, indent=2)
                if args.json
                else render_summary(summary, limit=limit)
            )
            if args.gate and not summary.passed:
                print(
                    "\ngate failed: see the FAIL lines above.",
                    file=sys.stderr,
                )
                return EXIT_FINDINGS
            return EXIT_OK

        if args.command == "query":
            result = kit.query(args.sql, args.max_rows or None)
            print(
                json.dumps(result.rows, ensure_ascii=False, indent=2, default=str)
                if args.json
                else render_query(result, limit=limit)
            )
            return EXIT_OK

    except QueryRejected as exc:
        print(f"rejected: {exc}", file=sys.stderr)
        return EXIT_ERROR
    except ToolkitError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return EXIT_ERROR

    return EXIT_ERROR


def _doctor(config: ProjectConfig, kit: Toolkit) -> int:
    """Say what is wired up and what is not, before anything needs debugging."""
    lines = [
        f"config:   {config.path}",
        f"toolkit:  {kit.name} v{kit.version} (subject noun: {kit.subject_noun})",
        "",
        "capabilities",
        f"  lint      {'yes' if kit.has_lint else 'no  — no rules and no @kit.validator'}"
        + (f"  ({len(kit.ruleset)} rules)" if kit.ruleset else ""),
        f"  replay    {'yes' if kit.has_runner else 'no  — register @kit.runner'}",
        f"  simulate  "
        f"{'yes' if kit.has_simulator else 'no  — register @kit.simulator or @kit.runner'}"
        + (f"  (default {kit.default_runs} runs, max {kit.max_runs})" if kit.has_simulator else ""),
        f"  query     {'yes' if kit.has_data else 'no  — add a [data] section'}",
        "",
        "thresholds",
    ]
    if kit.thresholds:
        lines.extend(f"  {t.metric} {t.band()}" for t in kit.thresholds)
    else:
        lines.append("  (none — `simulate --gate` will always pass)")

    if kit.has_data:
        available, reason = kit.source.available()
        lines.extend(
            [
                "",
                "data source",
                f"  driver:    {getattr(kit.source, 'driver', 'custom')}",
                f"  location:  {getattr(kit.source, 'location', '?')}",
                f"  reachable: {'yes' if available else 'no — ' + reason}",
            ]
        )

    try:
        from .server import _load_sdk

        _load_sdk()
        sdk_status = "installed"
    except ToolkitError:
        sdk_status = "missing — `pip install groundtruth-mcp[mcp]` to serve tools to an agent"
    lines.extend(["", f"mcp sdk:  {sdk_status}"])

    targets = kit.available_targets()
    lines.extend(
        [
            "",
            f"{kit.subject_noun}s: " + (", ".join(sorted(targets)) if targets else "(none listed)"),
        ]
    )

    print("\n".join(lines))
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
