"""Selector language, graph properties, and the declarative rule engine."""

from __future__ import annotations

import pytest

from groundtruth_mcp.checks import RuleError, RuleSet, resolve, resolve_one
from groundtruth_mcp.checks import graph as graphlib
from groundtruth_mcp.checks.selectors import SelectorError

DOC = {
    "start": "a",
    "states": [
        {"id": "a", "kind": "step", "transitions": [{"to": "b"}, {"to": "c"}]},
        {"id": "b", "kind": "step", "transitions": [{"to": "c"}]},
        {"id": "c", "kind": "terminal"},
    ],
}


# -- selectors --------------------------------------------------------------


def test_selector_flattens_nested_lists_and_records_indices():
    matches = resolve(DOC, "states[].transitions[].to")
    assert [m.value for m in matches] == ["b", "c", "c"]
    assert [m.path for m in matches] == [
        "states[0].transitions[0].to",
        "states[0].transitions[1].to",
        "states[1].transitions[0].to",
    ]


def test_selector_on_missing_key_yields_nothing_rather_than_raising():
    assert resolve(DOC, "states[].nonexistent[].field") == []
    assert resolve_one(DOC, "missing", default="fallback") == "fallback"


@pytest.mark.parametrize("expr", ["", "states[", "states..id", "9lives"])
def test_malformed_selectors_raise_at_parse_time(expr):
    with pytest.raises(SelectorError):
        resolve(DOC, expr)


# -- graph ------------------------------------------------------------------


def test_reachability_dead_ends_and_self_loops():
    graph = graphlib.build([("a", "b"), ("b", "c"), ("d", "d")], nodes=["a", "b", "c", "d", "e"])
    assert graphlib.unreachable(graph, "a") == ["d", "e"]
    assert graphlib.dead_ends(graph, terminals=["c"]) == ["e"]
    assert graphlib.self_loops(graph) == ["d"]


def test_cycles_finds_multi_node_rings_but_leaves_self_loops_alone():
    graph = graphlib.build([("a", "b"), ("b", "c"), ("c", "a"), ("d", "d")])
    assert graphlib.cycles(graph) == [["a", "b", "c"]]


def test_cycle_detection_survives_a_graph_deeper_than_the_recursion_limit():
    # 20k nodes in a line: recursive Tarjan dies here, the iterative one does not.
    pairs = [(f"n{i}", f"n{i + 1}") for i in range(20_000)]
    graph = graphlib.build(pairs)
    assert graphlib.cycles(graph) == []
    # 20_000, not 20_001: an edge whose target was never declared as a node
    # does not conjure one into the graph. Dangling targets are `ref_exists`'s
    # job to report, and inventing them here would hide it.
    assert len(graphlib.reachable_from(graph, "n0")) == 20_000


def test_unknown_start_reports_everything_unreachable():
    graph = graphlib.build([("a", "b")])
    assert graphlib.reachable_from(graph, "nope") == set()


# -- rules ------------------------------------------------------------------


def test_ref_exists_flags_the_dangling_edge_with_its_path():
    broken = {
        "start": "a",
        "states": [{"id": "a", "transitions": [{"to": "typo"}]}],
    }
    rules = RuleSet.from_dicts(
        [
            {
                "type": "ref_exists",
                "select": "states[].transitions[].to",
                "collection": "states[]",
                "key": "id",
            }
        ]
    )
    (issue,) = rules.run(broken)
    assert issue.code == "DANGLING_REF"
    assert issue.path == "states[0].transitions[0].to"
    assert "typo" in issue.message


def test_unique_key_points_at_the_second_declaration():
    doc = {"states": [{"id": "a"}, {"id": "a"}]}
    rules = RuleSet.from_dicts([{"type": "unique_key", "select": "states[]", "key": "id"}])
    (issue,) = rules.run(doc)
    assert issue.path == "states[1]"
    assert "states[0]" in issue.message


def test_no_cycle_honours_an_explicitly_allowed_ring():
    doc = {
        "states": [
            {"id": "a", "transitions": [{"to": "b"}]},
            {"id": "b", "transitions": [{"to": "a"}]},
        ]
    }
    spec = {
        "type": "no_cycle",
        "collection": "states[]",
        "key": "id",
        "edges": "transitions[].to",
    }
    assert len(RuleSet.from_dicts([spec]).run(doc)) == 1
    allowed = dict(spec, allow=[["b", "a"]])  # order must not matter
    assert RuleSet.from_dicts([allowed]).run(doc) == []


def test_range_and_enum_report_the_offending_value():
    doc = {"policy": {"rate": 1.4}, "states": [{"kind": "bogus"}]}
    rules = RuleSet.from_dicts(
        [
            {"type": "range", "select": "policy.rate", "min": 0, "max": 1},
            {"type": "enum", "select": "states[].kind", "values": ["step"]},
        ]
    )
    issues = rules.run(doc)
    assert {i.code for i in issues} == {"OUT_OF_RANGE", "INVALID_VALUE"}
    assert any("1.4" in i.message for i in issues)


def test_rule_errors_surface_at_load_time_not_check_time():
    with pytest.raises(RuleError, match="unknown check type"):
        RuleSet.from_dicts([{"type": "no_such_check"}])
    with pytest.raises(RuleError, match="missing required field"):
        RuleSet.from_dicts([{"type": "unique_key", "select": "states[]"}])
    with pytest.raises(RuleError, match="severity"):
        RuleSet.from_dicts([{"type": "not_empty", "select": "states", "severity": "warn"}])
    with pytest.raises(SelectorError):
        RuleSet.from_dicts([{"type": "not_empty", "select": "states[[]"}])


def test_severity_and_code_are_overridable_per_rule():
    doc = {"states": [{"id": "orphan"}], "start": "missing"}
    rules = RuleSet.from_dicts(
        [
            {
                "type": "reachable",
                "collection": "states[]",
                "key": "id",
                "edges": "transitions[].to",
                "start": "start",
                "code": "MY_CODE",
                "severity": "info",
            }
        ]
    )
    (issue,) = rules.run(doc)
    assert (issue.code, issue.severity) == ("MY_CODE", "info")
