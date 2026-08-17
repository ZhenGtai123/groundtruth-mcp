"""Graph properties every config-as-a-graph gets wrong the same four ways.

Workflows, state machines, dialogue trees, build pipelines, onboarding flows,
retry policies — they are all "nodes plus edges plus a start", and they all
rot in the same directions: a node nobody can reach, an edge pointing at
nothing, a node with no way out, a loop that was supposed to be a retry and
became a hang.

Kept free of any notion of what a node *means* so it can serve all of them.
Iterative rather than recursive throughout: a generated config with 50k nodes
is not exotic, and blowing the Python stack inside a lint tool is a bad way to
learn that.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence


@dataclass
class Graph:
    """Adjacency built from a config, keeping insertion order for stable output."""

    nodes: list[str] = field(default_factory=list)
    edges: dict[str, list[str]] = field(default_factory=dict)

    def add_node(self, key: str) -> None:
        if key not in self.edges:
            self.nodes.append(key)
            self.edges[key] = []

    def add_edge(self, source: str, target: str) -> None:
        self.add_node(source)
        self.edges[source].append(target)

    def successors(self, key: str) -> list[str]:
        return self.edges.get(key, [])

    def __contains__(self, key: object) -> bool:
        return key in self.edges

    def __len__(self) -> int:
        return len(self.nodes)


def build(pairs: Iterable[tuple[str, str]], nodes: Sequence[str] = ()) -> Graph:
    """A graph from `(source, target)` pairs, seeded with every declared node.

    Declared-but-unconnected nodes must exist in the graph or the reachability
    check has nothing to report them as.
    """
    graph = Graph()
    for key in nodes:
        graph.add_node(key)
    for source, target in pairs:
        graph.add_edge(source, target)
    return graph


def reachable_from(graph: Graph, start: str) -> set[str]:
    """Breadth-first closure over `start`. Unknown start yields the empty set."""
    if start not in graph:
        return set()
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for target in graph.successors(current):
            if target not in seen and target in graph:
                seen.add(target)
                queue.append(target)
    return seen


def unreachable(graph: Graph, start: str) -> list[str]:
    seen = reachable_from(graph, start)
    return [n for n in graph.nodes if n not in seen]


def dead_ends(graph: Graph, terminals: Iterable[str] = ()) -> list[str]:
    """Nodes with no outgoing edge that were not declared terminal.

    A terminal node with no exits is the point. A non-terminal one is a run
    that stops mid-flight, which in production reads as a hang, not an error.
    """
    allowed = set(terminals)
    return [n for n in graph.nodes if not graph.successors(n) and n not in allowed]


def self_loops(graph: Graph) -> list[str]:
    return [n for n in graph.nodes if n in graph.successors(n)]


def cycles(graph: Graph) -> list[list[str]]:
    """Every multi-node cycle, via Tarjan's strongly-connected components.

    Iterative Tarjan, not the textbook recursive one: same algorithm, an
    explicit stack instead of the interpreter's. Self-loops are excluded here
    because `self_loops()` reports them with a better message — a config where
    one node points at itself is a different authoring mistake from three
    nodes handing control around in a ring.
    """
    index_of: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    counter = 0
    found: list[list[str]] = []

    for root in graph.nodes:
        if root in index_of:
            continue
        # (node, iterator-position) frames, standing in for the recursion.
        work: list[tuple[str, int]] = [(root, 0)]
        index_of[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)

        while work:
            node, position = work[-1]
            successors = graph.successors(node)
            if position < len(successors):
                work[-1] = (node, position + 1)
                target = successors[position]
                if target not in graph:
                    continue
                if target not in index_of:
                    index_of[target] = low[target] = counter
                    counter += 1
                    stack.append(target)
                    on_stack.add(target)
                    work.append((target, 0))
                elif target in on_stack:
                    low[node] = min(low[node], index_of[target])
                continue

            work.pop()
            if work:
                parent = work[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index_of[node]:
                component: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1:
                    found.append(sorted(component))

    return found


def terminals_of(
    nodes: Iterable[Mapping[str, object]],
    key_field: str,
    kind_field: str,
    terminal_kinds: Iterable[str],
) -> list[str]:
    """Node keys whose `kind_field` is one of `terminal_kinds`."""
    wanted = set(terminal_kinds)
    out: list[str] = []
    for node in nodes:
        if not isinstance(node, Mapping):
            continue
        if str(node.get(kind_field, "")) in wanted:
            key = node.get(key_field)
            if isinstance(key, str):
                out.append(key)
    return out
