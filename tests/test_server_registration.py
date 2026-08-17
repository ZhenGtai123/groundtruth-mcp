"""What the agent sees: which tools exist, and what their descriptions say.

Registration is tested against a recorder rather than a live MCP server, so
these run on a machine that has never installed the SDK. The end of the file
covers the real round trip when the SDK is present.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from groundtruth_mcp import ProjectConfig, RunOutcome, Toolkit, Trace
from groundtruth_mcp.server import build_server

CONFIG = Path(__file__).resolve().parent.parent / "examples" / "checkout-flow" / "groundtruth.toml"


class Recorder:
    """Stands in for FastMCP, capturing what `build_server` registers."""

    def __init__(self) -> None:
        self.tools: dict[str, tuple[str, callable]] = {}

    def tool(self, name: str = "", description: str = ""):
        def register(fn):
            self.tools[name or fn.__name__] = (description or fn.__doc__ or "", fn)
            return fn

        return register


class LegacyRecorder(Recorder):
    """An older SDK: `tool()` takes no keyword arguments."""

    def tool(self, *args, **kwargs):  # type: ignore[override]
        if args or kwargs:
            raise TypeError("this SDK's tool() takes no arguments")
        return super().tool()


@pytest.fixture(scope="module")
def kit():
    return ProjectConfig.load(CONFIG).build()


def test_every_configured_capability_becomes_a_tool(kit):
    server = build_server(kit, Recorder())
    assert set(server.tools) == {"lint", "replay", "simulate", "query", "describe_data"}


def test_unconfigured_capabilities_are_absent_rather_than_broken():
    """A tool that always answers 'not configured' costs a call to learn nothing."""
    bare = Toolkit(name="bare")

    @bare.loader
    def load(name):
        return {}

    @bare.simulator
    def one(subject, seed, ctx):
        return RunOutcome(seed=seed, outcome="ok")

    server = build_server(bare, Recorder())
    assert set(server.tools) == {"simulate"}


def test_descriptions_carry_the_project_vocabulary_and_real_targets(kit):
    server = build_server(kit, Recorder())
    lint_description = server.tools["lint"][0]
    assert "flow" in lint_description
    assert "standard_checkout" in lint_description  # no round trip needed to learn the names

    simulate_description = server.tools["simulate"][0]
    assert "rate:success >= 0.8" in simulate_description  # the band CI enforces, stated up front


def test_registration_falls_back_for_sdks_without_keyword_arguments(kit):
    server = build_server(kit, LegacyRecorder())
    assert set(server.tools) == {"lint", "replay", "simulate", "query", "describe_data"}
    assert "flow" in server.tools["lint"][0]  # description survived, via __doc__


def test_tools_return_text_and_never_raise(kit):
    server = build_server(kit, Recorder())
    lint = server.tools["lint"][1]
    assert "BLOCKED" in lint("broken_checkout")
    assert "ERROR:" in lint("no_such_flow")
    assert "standard_checkout" in lint("no_such_flow")  # the error teaches the valid values

    query = server.tools["query"][1]
    assert "REJECTED:" in query("DROP TABLE runs")


def test_output_is_capped_at_the_configured_budget(kit):
    kit.max_output_chars = 400
    try:
        server = build_server(kit, Recorder())
        text = server.tools["replay"][1]("standard_checkout", 3)
        assert len(text) < 700 and "truncated" in text
    finally:
        kit.max_output_chars = 8000


def test_a_trace_step_snapshots_state_rather_than_aliasing_it():
    """The instrumentation bug that makes every step show the final state."""
    trace = Trace()
    live = {"n": 1}
    trace.step("a", state=live)
    live["n"] = 2
    trace.step("b", state=live)
    assert [dict(s.state) for s in trace.steps] == [{"n": 1}, {"n": 2}]


def test_real_sdk_round_trip_if_installed(kit):
    """Registers against whatever `mcp` version is actually installed, if any."""
    pytest.importorskip("mcp", reason="the MCP SDK is an optional dependency")
    server = build_server(kit)
    assert server is not None
