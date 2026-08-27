"""Typed relationship graph over a repository's symbols.

Analyzers emit relationships between *names* (``Circle`` inherits ``Shape``).
This module turns those into a graph of resolved nodes and edges, which is what
diagram generation and the higher-level agents consume.

Name resolution is deliberately conservative. An edge whose target matches
exactly one declared symbol is ``resolved``; a name matching several symbols is
``ambiguous`` (the candidates are recorded, no arbitrary winner is picked); a
name declared nowhere in the repository is ``unresolved`` and becomes an
external node. Resolution status is tracked *separately* from detection
confidence — failing to resolve ``fmt`` does not make the detection of the
import less certain.
"""

from __future__ import annotations

from collections import defaultdict
from typing import Iterable, Literal

from operations.oop_analyzer import TYPE_KINDS, RepositoryAnalysis
from operations.schemas import (
    Confidence,
    DetectionMethod,
    RelationType,
    SymbolType,
    Visibility,
)
from pydantic import BaseModel, Field

Resolution = Literal["resolved", "ambiguous", "unresolved"]

#: Relationship kinds that describe type structure — the class-diagram set.
STRUCTURAL_RELATIONS: frozenset[RelationType] = frozenset(
    {
        RelationType.INHERITS,
        RelationType.IMPLEMENTS,
        RelationType.CONTAINS,
        RelationType.USES,
    }
)


class GraphNode(BaseModel):
    """One symbol (or external module) in the graph."""

    id: str
    name: str
    kind: SymbolType
    language: str = ""
    package: str = ""
    file_path: str = ""
    line: int | None = None
    visibility: Visibility = Visibility.UNKNOWN
    is_abstract: bool = False
    external: bool = False
    """True for modules referenced but not declared in this repository."""

    @property
    def is_type(self) -> bool:
        return self.kind in TYPE_KINDS


class GraphEdge(BaseModel):
    """One typed relationship between graph nodes."""

    source_id: str
    target_id: str | None
    source_name: str
    target_name: str
    relation: RelationType
    resolution: Resolution = "resolved"
    candidates: list[str] = Field(default_factory=list)
    """Node ids considered when ``resolution`` is ``ambiguous``."""
    confidence: Confidence = Confidence.MEDIUM
    detection_method: DetectionMethod = DetectionMethod.LEXICAL_PARSE
    file_path: str = ""
    line: int | None = None
    metadata: dict[str, object] = Field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.source_id, self.target_id or self.target_name, self.relation.value)


class RelationshipGraph(BaseModel):
    """Nodes plus typed edges, with the bookkeeping diagrams need."""

    repository: str = ""
    nodes: list[GraphNode] = Field(default_factory=list)
    edges: list[GraphEdge] = Field(default_factory=list)
    omitted_nodes: list[str] = Field(default_factory=list)
    omitted_edge_count: int = 0
    warnings: list[str] = Field(default_factory=list)

    # -- lookups -----------------------------------------------------------

    def node_map(self) -> dict[str, GraphNode]:
        return {node.id: node for node in self.nodes}

    def type_nodes(self) -> list[GraphNode]:
        """Declared class/interface/struct/enum/record nodes."""
        return [node for node in self.nodes if node.is_type and not node.external]

    def edges_of(self, relations: Iterable[RelationType]) -> list[GraphEdge]:
        wanted = set(relations)
        return [edge for edge in self.edges if edge.relation in wanted]

    def degree(self) -> dict[str, int]:
        """Edge count touching each node id."""
        counts: dict[str, int] = defaultdict(int)
        for edge in self.edges:
            counts[edge.source_id] += 1
            if edge.target_id:
                counts[edge.target_id] += 1
        return counts

    def packages(self) -> dict[str, list[str]]:
        """Package/module name → ids of the nodes declared in it."""
        grouped: dict[str, list[str]] = defaultdict(list)
        for node in self.nodes:
            if node.external:
                continue
            grouped[node.package or "(root)"].append(node.id)
        return {package: sorted(ids) for package, ids in sorted(grouped.items())}

    # -- derived views -----------------------------------------------------

    def subgraph(self, node_ids: Iterable[str], *, keep_dangling: bool = False) -> RelationshipGraph:
        """Graph restricted to ``node_ids`` (and the edges between them)."""
        keep = set(node_ids)
        nodes = [node for node in self.nodes if node.id in keep]
        edges = [
            edge
            for edge in self.edges
            if edge.source_id in keep
            and (edge.target_id in keep or (keep_dangling and edge.target_id is None))
        ]
        dropped = [node.id for node in self.nodes if node.id not in keep]
        return RelationshipGraph(
            repository=self.repository,
            nodes=nodes,
            edges=edges,
            omitted_nodes=sorted(self.omitted_nodes + dropped),
            omitted_edge_count=self.omitted_edge_count + (len(self.edges) - len(edges)),
            warnings=list(self.warnings),
        )

    def filtered(self, relations: Iterable[RelationType]) -> RelationshipGraph:
        """Graph keeping only the given relationship kinds and their endpoints."""
        edges = self.edges_of(relations)
        touched = {edge.source_id for edge in edges}
        touched |= {edge.target_id for edge in edges if edge.target_id}
        nodes = [node for node in self.nodes if node.id in touched]
        return RelationshipGraph(
            repository=self.repository,
            nodes=nodes,
            edges=edges,
            omitted_nodes=list(self.omitted_nodes),
            omitted_edge_count=self.omitted_edge_count,
            warnings=list(self.warnings),
        )

    def limited(self, *, max_nodes: int, max_edges: int) -> RelationshipGraph:
        """Trim to the most connected core, recording exactly what was dropped.

        Nodes are ranked by degree (ties broken by id, so the result is
        deterministic) — a diagram of the twenty busiest types is far more
        useful than an arbitrary twenty. Everything removed is reported in
        ``omitted_nodes``/``omitted_edge_count`` rather than silently vanishing.
        """
        if len(self.nodes) <= max_nodes and len(self.edges) <= max_edges:
            return self
        degrees = self.degree()
        ranked = sorted(self.nodes, key=lambda node: (-degrees.get(node.id, 0), node.id))
        keep = {node.id for node in ranked[:max_nodes]}
        trimmed = self.subgraph(keep)
        if len(trimmed.edges) > max_edges:
            dropped = len(trimmed.edges) - max_edges
            ordered = sorted(
                trimmed.edges,
                key=lambda edge: (
                    _CONFIDENCE_ORDER[edge.confidence],
                    _RELATION_ORDER.get(edge.relation, 99),
                    edge.source_name,
                    edge.target_name,
                ),
            )
            trimmed = RelationshipGraph(
                repository=trimmed.repository,
                nodes=trimmed.nodes,
                edges=ordered[:max_edges],
                omitted_nodes=trimmed.omitted_nodes,
                omitted_edge_count=trimmed.omitted_edge_count + dropped,
                warnings=trimmed.warnings,
            )
        return trimmed

    def split_by_package(self, *, max_nodes: int) -> list[tuple[str, RelationshipGraph]]:
        """Split into per-package subgraphs, chunked to ``max_nodes`` each.

        This is the answer to "one unreadable diagram": a large repository
        becomes several package-scoped diagrams instead of a single hairball.
        """
        chunks: list[tuple[str, RelationshipGraph]] = []
        for package, ids in self.packages().items():
            for index in range(0, len(ids), max_nodes):
                window = ids[index : index + max_nodes]
                suffix = "" if len(ids) <= max_nodes else f" ({index // max_nodes + 1})"
                chunks.append((f"{package}{suffix}", self.subgraph(window)))
        return chunks

    def package_dependency_edges(self) -> list[tuple[str, str, int]]:
        """Aggregate IMPORTS edges into ``(from_package, to_package, weight)``."""
        nodes = self.node_map()
        weights: dict[tuple[str, str], int] = defaultdict(int)
        for edge in self.edges:
            if edge.relation is not RelationType.IMPORTS:
                continue
            source = nodes.get(edge.source_id)
            if source is None:
                continue
            source_package = source.package or source.name or "(root)"
            target = nodes.get(edge.target_id) if edge.target_id else None
            target_package = (
                (target.package or target.name) if target else edge.target_name
            )
            if not target_package or source_package == target_package:
                continue
            weights[(source_package, target_package)] += 1
        return [
            (source, target, weight)
            for (source, target), weight in sorted(
                weights.items(), key=lambda item: (-item[1], item[0])
            )
        ]

    def stats(self) -> dict[str, object]:
        """Node/edge counts broken down by kind, relation and resolution."""
        by_kind: dict[str, int] = defaultdict(int)
        for node in self.nodes:
            by_kind[node.kind.value] += 1
        by_relation: dict[str, int] = defaultdict(int)
        by_resolution: dict[str, int] = defaultdict(int)
        for edge in self.edges:
            by_relation[edge.relation.value] += 1
            by_resolution[edge.resolution] += 1
        return {
            "node_count": len(self.nodes),
            "edge_count": len(self.edges),
            "nodes_by_kind": dict(sorted(by_kind.items())),
            "edges_by_relation": dict(sorted(by_relation.items())),
            "edges_by_resolution": dict(sorted(by_resolution.items())),
            "omitted_node_count": len(self.omitted_nodes),
            "omitted_edge_count": self.omitted_edge_count,
        }


_CONFIDENCE_ORDER = {Confidence.HIGH: 0, Confidence.MEDIUM: 1, Confidence.LOW: 2}
_RELATION_ORDER = {
    RelationType.INHERITS: 0,
    RelationType.IMPLEMENTS: 1,
    RelationType.CONTAINS: 2,
    RelationType.USES: 3,
    RelationType.IMPORTS: 4,
    RelationType.CALLS: 5,
}


# --------------------------------------------------------------------------
# Construction
# --------------------------------------------------------------------------


def _module_parts(module: str) -> list[str]:
    """Split a dotted or slashed module name into comparable path segments."""
    return [part for part in module.replace("/", ".").split(".") if part]


def _shared_prefix(left: list[str], right: list[str]) -> int:
    """Number of leading path segments two module names have in common."""
    shared = 0
    for first, second in zip(left, right):
        if first != second:
            break
        shared += 1
    return shared


def _type_id(package: str, name: str) -> str:
    return f"type:{package}:{name}"


def _member_id(package: str, owner: str, name: str) -> str:
    return f"member:{package}:{owner}.{name}"


def _function_id(package: str, name: str) -> str:
    return f"func:{package}:{name}"


def _external_id(name: str) -> str:
    return f"external:{name}"


def build_graph(
    analysis: RepositoryAnalysis,
    *,
    include_calls: bool = True,
    include_field_access: bool = False,
) -> RelationshipGraph:
    """Build a :class:`RelationshipGraph` from a repository analysis.

    Args:
        analysis: Merged per-file analysis.
        include_calls: Keep CALLS edges (large; needed for call graphs and
            sequence diagrams, noise for class diagrams).
        include_field_access: Keep READS/WRITES edges on fields.
    """
    graph = RelationshipGraph(repository=analysis.repository)
    nodes: dict[str, GraphNode] = {}
    by_name: dict[str, list[str]] = defaultdict(list)
    by_qualified: dict[str, list[str]] = defaultdict(list)

    def add_node(node: GraphNode, *, index_names: tuple[str, ...]) -> None:
        if node.id in nodes:
            return
        nodes[node.id] = node
        for index_name in index_names:
            by_name[index_name].append(node.id)

    for symbol in analysis.symbols:
        if symbol.symbol_type in TYPE_KINDS:
            node = GraphNode(
                id=_type_id(symbol.package, symbol.name),
                name=symbol.name,
                kind=symbol.symbol_type,
                language=symbol.language,
                package=symbol.package,
                file_path=symbol.file_path,
                line=symbol.line,
                visibility=symbol.visibility,
                is_abstract=symbol.is_abstract,
            )
            add_node(node, index_names=(symbol.name,))
        elif symbol.owner:
            node = GraphNode(
                id=_member_id(symbol.package, symbol.owner, symbol.name),
                name=f"{symbol.owner}.{symbol.name}",
                kind=symbol.symbol_type,
                language=symbol.language,
                package=symbol.package,
                file_path=symbol.file_path,
                line=symbol.line,
                visibility=symbol.visibility,
                is_abstract=symbol.is_abstract,
            )
            add_node(node, index_names=())
            by_qualified[f"{symbol.owner}.{symbol.name}"].append(node.id)
        elif symbol.symbol_type in (SymbolType.FUNCTION, SymbolType.CONSTRUCTOR):
            node = GraphNode(
                id=_function_id(symbol.package, symbol.name),
                name=symbol.name,
                kind=symbol.symbol_type,
                language=symbol.language,
                package=symbol.package,
                file_path=symbol.file_path,
                line=symbol.line,
                visibility=symbol.visibility,
            )
            add_node(node, index_names=(symbol.name,))

    # Module nodes let IMPORTS edges hang off something concrete.
    for module in analysis.imports_by_module:
        module_id = f"module:{module}"
        add_node(
            GraphNode(
                id=module_id,
                name=module,
                kind=SymbolType.MODULE,
                package=module,
            ),
            index_names=(module,),
        )

    module_ids = {
        node.name: node.id for node in nodes.values() if node.kind is SymbolType.MODULE
    }

    def resolve_module(
        name: str, source_module: str
    ) -> tuple[str | None, Resolution, list[str]]:
        """Match an import specifier against a declared module.

        An import is written relative to the importing project's module root
        (``from shapes import Circle``, ``require('./logger')``) while module
        nodes are named from the repository root (``python_repo.shapes``,
        ``js_repo/src/logger``). Matching on a trailing path segment bridges
        the two.

        When several modules share that trailing segment, the one sharing the
        longest leading path with the *importing* module wins — which is how
        the language itself resolves the import. If even that leaves a tie, the
        edge stays ``ambiguous``: a coin flip would be worse than an honest
        "could be either".
        """
        needle = name.strip().lstrip("./").replace("\\", "/")
        if not needle:
            return None, "unresolved", []
        dotted = needle.replace("/", ".")
        matches = {
            module_name: module_id
            for module_name, module_id in module_ids.items()
            if module_name == needle
            or module_name.replace("/", ".").endswith(f".{dotted}")
        }
        if not matches:
            return None, "unresolved", []
        if len(matches) == 1:
            return next(iter(matches.values())), "resolved", []

        source_parts = _module_parts(source_module)
        scored = sorted(
            (
                (-_shared_prefix(source_parts, _module_parts(module_name)), module_name)
                for module_name in matches
            )
        )
        best_score = scored[0][0]
        winners = [name_ for score, name_ in scored if score == best_score]
        if len(winners) == 1 and best_score < 0:
            return matches[winners[0]], "resolved", []
        return None, "ambiguous", sorted(matches.values())

    def resolve(
        name: str, *, as_module: bool = False, source_module: str = ""
    ) -> tuple[str | None, Resolution, list[str]]:
        """Map a symbol name to a node id, reporting ambiguity honestly."""
        for key, index in ((name, by_qualified), (name, by_name)):
            candidates = index.get(key) or []
            if len(candidates) == 1:
                return candidates[0], "resolved", []
            if len(candidates) > 1:
                return None, "ambiguous", sorted(candidates)
        simple = name.rsplit(".", 1)[-1]
        if simple != name:
            candidates = by_name.get(simple) or []
            if len(candidates) == 1:
                return candidates[0], "resolved", []
            if len(candidates) > 1:
                return None, "ambiguous", sorted(candidates)
        if as_module:
            return resolve_module(name, source_module)
        return None, "unresolved", []

    skipped_relations: set[RelationType] = set()
    if not include_calls:
        skipped_relations.add(RelationType.CALLS)
    if not include_field_access:
        skipped_relations |= {RelationType.READS, RelationType.WRITES}

    seen_edges: set[tuple[str, str, str]] = set()
    for relationship in analysis.relationships:
        if relationship.relation in skipped_relations:
            continue
        source_id, source_resolution, _ = resolve(relationship.source)
        if source_id is None:
            # An edge with no anchor cannot be drawn or reasoned about.
            continue
        target_id, target_resolution, candidates = resolve(
            relationship.target,
            as_module=relationship.relation is RelationType.IMPORTS,
            source_module=relationship.source,
        )
        if target_id is None and target_resolution == "unresolved":
            external = GraphNode(
                id=_external_id(relationship.target),
                name=relationship.target,
                kind=SymbolType.MODULE
                if relationship.relation is RelationType.IMPORTS
                else SymbolType.UNKNOWN,
                package=relationship.target,
                external=True,
            )
            add_node(external, index_names=())
            target_id = external.id

        edge = GraphEdge(
            source_id=source_id,
            target_id=target_id,
            source_name=relationship.source,
            target_name=relationship.target,
            relation=relationship.relation,
            resolution="resolved"
            if target_resolution == "resolved" and source_resolution == "resolved"
            else target_resolution,
            candidates=candidates,
            confidence=relationship.confidence,
            detection_method=relationship.detection_method,
            file_path=relationship.file_path,
            line=relationship.line,
            metadata=dict(relationship.metadata),
        )
        if edge.key in seen_edges:
            continue
        seen_edges.add(edge.key)
        graph.edges.append(edge)

    graph.nodes = sorted(nodes.values(), key=lambda node: node.id)
    ambiguous = sum(1 for edge in graph.edges if edge.resolution == "ambiguous")
    if ambiguous:
        graph.warnings.append(
            f"{ambiguous} edge(s) reference a name declared by more than one "
            f"symbol; they are marked 'ambiguous' and left unresolved."
        )
    return graph


__all__ = [
    "GraphEdge",
    "GraphNode",
    "RelationshipGraph",
    "STRUCTURAL_RELATIONS",
    "build_graph",
]
