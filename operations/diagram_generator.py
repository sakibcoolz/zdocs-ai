"""Mermaid diagram generation from a :class:`~operations.relationship_graph.RelationshipGraph`.

Three problems this module exists to solve:

**Validity.** Symbol names from real repositories contain characters Mermaid
treats as syntax (``<>``, ``.``, ``-``, ``#``, quotes). Every identifier is
sanitized to a safe token and de-duplicated, and every free-text label is
escaped, so generated diagrams parse.

**Honesty.** An edge that was derived or resolved ambiguously is labelled as
such in the diagram, rather than being drawn identically to a fact read
straight off a declaration.

**Readability at scale.** A 400-class repository rendered as one class diagram
is unusable. Diagrams are node- and edge-capped, split per package when asked,
and always report exactly what was left out.
"""

from __future__ import annotations

import re
from typing import Iterable

from operations.policy import redact
from operations.relationship_graph import GraphEdge, GraphNode, RelationshipGraph
from operations.schemas import Confidence, RelationType, SymbolType, Visibility
from pydantic import BaseModel, Field

#: Default caps. Beyond roughly this size a single diagram stops being readable.
DEFAULT_MAX_NODES = 60
DEFAULT_MAX_EDGES = 150
DEFAULT_MAX_MEMBERS = 12

_UNSAFE = re.compile(r"[^0-9A-Za-z_]")

_VISIBILITY_MARKER = {
    Visibility.PUBLIC: "+",
    Visibility.PRIVATE: "-",
    Visibility.PROTECTED: "#",
    Visibility.PACKAGE: "~",
    Visibility.INTERNAL: "~",
    Visibility.UNKNOWN: "+",
}

_STEREOTYPE = {
    SymbolType.INTERFACE: "interface",
    SymbolType.ABSTRACT_CLASS: "abstract",
    SymbolType.STRUCT: "struct",
    SymbolType.ENUM: "enumeration",
    SymbolType.RECORD: "record",
}


class Diagram(BaseModel):
    """One generated Mermaid diagram plus what it had to leave out."""

    title: str
    kind: str
    mermaid: str
    filename: str
    node_count: int = 0
    edge_count: int = 0
    omitted_nodes: list[str] = Field(default_factory=list)
    omitted_edge_count: int = 0
    warnings: list[str] = Field(default_factory=list)


class _Namer:
    """Assigns unique, Mermaid-safe identifiers to node ids."""

    def __init__(self) -> None:
        self._by_node: dict[str, str] = {}
        self._used: set[str] = set()
        self.renamed: dict[str, str] = {}

    def get(self, node_id: str, display: str) -> str:
        existing = self._by_node.get(node_id)
        if existing is not None:
            return existing
        safe = sanitize_identifier(display)
        candidate = safe
        suffix = 2
        while candidate in self._used:
            candidate = f"{safe}_{suffix}"
            suffix += 1
        self._used.add(candidate)
        self._by_node[node_id] = candidate
        if candidate != display:
            self.renamed[display] = candidate
        return candidate


def sanitize_identifier(name: str) -> str:
    """Reduce a symbol name to a Mermaid-safe identifier.

    Non-alphanumeric characters become ``_``; a leading digit is prefixed; an
    empty result becomes ``unnamed``. Length is capped so one pathological name
    cannot blow up a diagram.
    """
    safe = _UNSAFE.sub("_", (name or "").strip())
    safe = re.sub(r"_{2,}", "_", safe).strip("_")
    if not safe:
        return "unnamed"
    if safe[0].isdigit():
        safe = f"n_{safe}"
    return safe[:64]


def escape_label(text: str) -> str:
    """Make free text safe inside a Mermaid label."""
    cleaned = redact(text or "").replace("\\", "/").replace('"', "'")
    cleaned = cleaned.replace("\n", " ").replace("`", "'")
    cleaned = re.sub(r"[{}<>|;]", " ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
    return cleaned[:80]


def _edge_note(edge: GraphEdge) -> str:
    """Short qualifier shown on an edge that is not a plain confirmed fact."""
    parts: list[str] = []
    kind = edge.metadata.get("kind")
    if isinstance(kind, str) and kind not in ("extends", "implements", "base_class"):
        parts.append(kind.replace("_", " "))
    if edge.confidence is not Confidence.HIGH:
        parts.append(f"{edge.confidence.value} confidence")
    if edge.resolution != "resolved":
        parts.append(edge.resolution)
    return escape_label(", ".join(parts))


def _members_by_owner(graph: RelationshipGraph) -> dict[str, list[GraphNode]]:
    grouped: dict[str, list[GraphNode]] = {}
    for node in graph.nodes:
        if node.kind not in (SymbolType.METHOD, SymbolType.CONSTRUCTOR, SymbolType.FIELD):
            continue
        owner, _, _member = node.name.partition(".")
        if not owner:
            continue
        grouped.setdefault(f"{node.package}:{owner}", []).append(node)
    return grouped


def _class_block(
    node: GraphNode, alias: str, members: list[GraphNode], max_members: int
) -> list[str]:
    """Mermaid ``class`` block for one type, with a capped member list."""
    lines = [f"    class {alias} {{"]
    stereotype = _STEREOTYPE.get(node.kind)
    if stereotype:
        lines.append(f"        <<{stereotype}>>")
    ordered = sorted(
        members,
        key=lambda member: (
            0 if member.kind is SymbolType.CONSTRUCTOR else 1 if member.kind is SymbolType.METHOD else 2,
            member.name,
        ),
    )
    for member in ordered[:max_members]:
        marker = _VISIBILITY_MARKER.get(member.visibility, "+")
        member_name = escape_label(member.name.split(".", 1)[-1])
        if member.kind is SymbolType.FIELD:
            lines.append(f"        {marker}{member_name}")
        else:
            lines.append(f"        {marker}{member_name}()")
    hidden = len(ordered) - max_members
    if hidden > 0:
        lines.append(f"        +{hidden} more members not shown")
    lines.append("    }")
    if node.name != alias:
        # Keep the true name visible when sanitizing changed it.
        lines.append(f'    note for {alias} "declared as {escape_label(node.name)}"')
    return lines


_RELATION_ARROW = {
    RelationType.INHERITS: "<|--",
    RelationType.IMPLEMENTS: "<|..",
    RelationType.CONTAINS: "*--",
    RelationType.USES: "..>",
}


def generate_class_diagram(
    graph: RelationshipGraph,
    *,
    title: str = "Class diagram",
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    max_members: int = DEFAULT_MAX_MEMBERS,
    filename: str = "class-diagram.mmd",
) -> Diagram:
    """Mermaid ``classDiagram`` of types and their structural relationships."""
    structural = graph.filtered(
        [
            RelationType.INHERITS,
            RelationType.IMPLEMENTS,
            RelationType.CONTAINS,
            RelationType.USES,
        ]
    )
    type_ids = {node.id for node in graph.nodes if node.is_type and not node.external}
    edges = [
        edge
        for edge in structural.edges
        if edge.source_id in type_ids
        and edge.target_id in type_ids
        and edge.metadata.get("kind") != "method_override"
    ]
    connected = {edge.source_id for edge in edges} | {
        edge.target_id for edge in edges if edge.target_id
    }
    keep = connected or type_ids
    scoped = graph.subgraph(keep | _member_ids(graph, keep)).limited(
        max_nodes=max_nodes + len(_member_ids(graph, keep)), max_edges=max_edges
    )
    kept_types = [node for node in scoped.nodes if node.id in keep]
    if len(kept_types) > max_nodes:
        kept_types = kept_types[:max_nodes]
    kept_ids = {node.id for node in kept_types}

    namer = _Namer()
    members = _members_by_owner(graph)
    lines = ["classDiagram", f"    %% {escape_label(title)}"]
    for node in sorted(kept_types, key=lambda item: (item.package, item.name)):
        alias = namer.get(node.id, node.name)
        lines.extend(
            _class_block(node, alias, members.get(f"{node.package}:{node.name}", []), max_members)
        )

    drawn = 0
    for edge in edges:
        if edge.source_id not in kept_ids or edge.target_id not in kept_ids:
            continue
        if drawn >= max_edges:
            break
        arrow = _RELATION_ARROW.get(edge.relation)
        if arrow is None:
            continue
        source = namer.get(edge.source_id, edge.source_name)
        target = namer.get(edge.target_id, edge.target_name)
        # `A <|-- B` reads "B inherits A", so the target is written first.
        left, right = (target, source) if arrow.startswith("<|") else (source, target)
        note = _edge_note(edge)
        lines.append(f"    {left} {arrow} {right}" + (f" : {note}" if note else ""))
        drawn += 1

    omitted = sorted(
        node.name for node in graph.nodes if node.id in type_ids and node.id not in kept_ids
    )
    warnings = list(graph.warnings)
    if omitted:
        warnings.append(
            f"{len(omitted)} type(s) omitted from this diagram to keep it readable; "
            f"see omitted_nodes for the full list."
        )
    if namer.renamed:
        warnings.append(
            f"{len(namer.renamed)} symbol name(s) were sanitized for Mermaid; "
            f"original names are preserved in diagram notes."
        )
    return Diagram(
        title=title,
        kind="class",
        mermaid="\n".join(lines),
        filename=filename,
        node_count=len(kept_types),
        edge_count=drawn,
        omitted_nodes=omitted,
        omitted_edge_count=max(0, len(edges) - drawn),
        warnings=warnings,
    )


def _member_ids(graph: RelationshipGraph, owner_ids: set[str]) -> set[str]:
    """Member node ids belonging to the given type node ids."""
    owners = {
        (node.package, node.name) for node in graph.nodes if node.id in owner_ids
    }
    return {
        node.id
        for node in graph.nodes
        if node.kind in (SymbolType.METHOD, SymbolType.CONSTRUCTOR, SymbolType.FIELD)
        and (node.package, node.name.split(".", 1)[0]) in owners
    }


def generate_class_diagrams(
    graph: RelationshipGraph,
    *,
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    split_by_package: bool = False,
    max_members: int = DEFAULT_MAX_MEMBERS,
) -> list[Diagram]:
    """One class diagram, or several package-scoped ones for a large graph.

    Splitting kicks in when explicitly requested or when the type count exceeds
    ``max_nodes`` — the alternative being a single unreadable diagram.
    """
    type_count = len(graph.type_nodes())
    if not split_by_package and type_count <= max_nodes:
        return [
            generate_class_diagram(
                graph, max_nodes=max_nodes, max_edges=max_edges, max_members=max_members
            )
        ]

    diagrams: list[Diagram] = []
    for package, chunk in graph.split_by_package(max_nodes=max_nodes):
        if not chunk.type_nodes():
            continue
        slug = sanitize_identifier(package).lower() or "root"
        diagrams.append(
            generate_class_diagram(
                chunk,
                title=f"Class diagram — {package}",
                max_nodes=max_nodes,
                max_edges=max_edges,
                max_members=max_members,
                filename=f"class-diagram-{slug}.mmd",
            )
        )
    if not diagrams:
        diagrams.append(
            generate_class_diagram(graph, max_nodes=max_nodes, max_edges=max_edges)
        )
    return diagrams


def generate_inheritance_diagram(
    graph: RelationshipGraph,
    *,
    title: str = "Inheritance and interface implementation",
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    filename: str = "inheritance-diagram.mmd",
) -> Diagram:
    """Mermaid ``classDiagram`` restricted to INHERITS/IMPLEMENTS edges."""
    type_ids = {node.id for node in graph.nodes if node.is_type and not node.external}
    edges = [
        edge
        for edge in graph.edges_of([RelationType.INHERITS, RelationType.IMPLEMENTS])
        if edge.source_id in type_ids
        and edge.target_id in type_ids
        and edge.metadata.get("kind") != "method_override"
    ]
    involved = {edge.source_id for edge in edges} | {
        edge.target_id for edge in edges if edge.target_id
    }
    nodes = [node for node in graph.nodes if node.id in involved]
    nodes.sort(key=lambda node: (node.package, node.name))
    kept = nodes[:max_nodes]
    kept_ids = {node.id for node in kept}

    namer = _Namer()
    lines = ["classDiagram", f"    %% {escape_label(title)}"]
    for node in kept:
        alias = namer.get(node.id, node.name)
        stereotype = _STEREOTYPE.get(node.kind)
        if stereotype:
            lines.append(f"    class {alias} {{\n        <<{stereotype}>>\n    }}")
        else:
            lines.append(f"    class {alias}")

    drawn = 0
    for edge in edges:
        if edge.source_id not in kept_ids or edge.target_id not in kept_ids:
            continue
        if drawn >= max_edges:
            break
        arrow = _RELATION_ARROW[edge.relation]
        source = namer.get(edge.source_id, edge.source_name)
        target = namer.get(edge.target_id, edge.target_name)
        note = _edge_note(edge)
        lines.append(f"    {target} {arrow} {source}" + (f" : {note}" if note else ""))
        drawn += 1

    omitted = sorted(node.name for node in nodes[max_nodes:])
    return Diagram(
        title=title,
        kind="inheritance",
        mermaid="\n".join(lines),
        filename=filename,
        node_count=len(kept),
        edge_count=drawn,
        omitted_nodes=omitted,
        omitted_edge_count=max(0, len(edges) - drawn),
        warnings=[f"{len(omitted)} type(s) omitted"] if omitted else [],
    )


def generate_package_dependency_diagram(
    graph: RelationshipGraph,
    *,
    title: str = "Package dependencies",
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    include_external: bool = False,
    filename: str = "package-dependency.mmd",
) -> Diagram:
    """Mermaid ``flowchart`` of module/package import dependencies."""
    internal = set(graph.packages())
    weighted = graph.package_dependency_edges()
    if not include_external:
        weighted = [
            (source, target, weight)
            for source, target, weight in weighted
            if target in internal
        ]

    namer = _Namer()
    lines = ["flowchart LR", f"    %% {escape_label(title)}"]
    seen: set[str] = set()
    drawn = 0
    omitted_edges = 0
    for source, target, weight in weighted:
        if drawn >= max_edges or len(seen) >= max_nodes * 2:
            omitted_edges += 1
            continue
        for package in (source, target):
            if package in seen:
                continue
            seen.add(package)
            alias = namer.get(f"pkg:{package}", package)
            external_marker = "" if package in internal else ":::external"
            lines.append(f'    {alias}["{escape_label(package)}"]{external_marker}')
        source_alias = namer.get(f"pkg:{source}", source)
        target_alias = namer.get(f"pkg:{target}", target)
        label = f"|{weight}|" if weight > 1 else ""
        lines.append(f"    {source_alias} -->{label} {target_alias}")
        drawn += 1
    if not include_external and any(node.external for node in graph.nodes):
        lines.append("    %% external/third-party modules excluded")
    lines.append("    classDef external stroke-dasharray: 4 3")

    return Diagram(
        title=title,
        # Matches the request vocabulary in `api_operations.DIAGRAM_OPERATIONS`,
        # so a caller can correlate a returned diagram with what it asked for.
        kind="dependency",
        mermaid="\n".join(lines),
        filename=filename,
        node_count=len(seen),
        edge_count=drawn,
        omitted_nodes=[],
        omitted_edge_count=omitted_edges,
        warnings=(
            [f"{omitted_edges} dependency edge(s) omitted to keep the diagram readable"]
            if omitted_edges
            else []
        ),
    )


def generate_component_diagram(
    graph: RelationshipGraph,
    *,
    title: str = "Components",
    max_nodes: int = DEFAULT_MAX_NODES,
    max_edges: int = DEFAULT_MAX_EDGES,
    filename: str = "component-diagram.mmd",
) -> Diagram:
    """Mermaid ``flowchart`` grouping types into per-package subgraphs."""
    namer = _Namer()
    lines = ["flowchart TB", f"    %% {escape_label(title)}"]
    kept_ids: set[str] = set()
    for package, ids in graph.packages().items():
        types = [
            node
            for node in graph.nodes
            if node.id in set(ids) and node.is_type and not node.external
        ]
        if not types:
            continue
        if len(kept_ids) >= max_nodes:
            break
        group_alias = sanitize_identifier(f"pkg_{package}")
        lines.append(f'    subgraph {group_alias}["{escape_label(package)}"]')
        for node in types:
            if len(kept_ids) >= max_nodes:
                break
            alias = namer.get(node.id, node.name)
            shape = f'{alias}(["{escape_label(node.name)}"])' if node.kind is SymbolType.INTERFACE else f'{alias}["{escape_label(node.name)}"]'
            lines.append(f"        {shape}")
            kept_ids.add(node.id)
        lines.append("    end")

    drawn = 0
    for edge in graph.edges_of([RelationType.INHERITS, RelationType.IMPLEMENTS, RelationType.USES]):
        if edge.source_id not in kept_ids or edge.target_id not in kept_ids:
            continue
        if drawn >= max_edges:
            break
        source = namer.get(edge.source_id, edge.source_name)
        target = namer.get(edge.target_id, edge.target_name)
        arrow = "-.->" if edge.relation is RelationType.USES else "-->"
        lines.append(f"    {source} {arrow}|{escape_label(edge.relation.value.lower())}| {target}")
        drawn += 1

    omitted = sorted(
        node.name
        for node in graph.nodes
        if node.is_type and not node.external and node.id not in kept_ids
    )
    return Diagram(
        title=title,
        kind="component",
        mermaid="\n".join(lines),
        filename=filename,
        node_count=len(kept_ids),
        edge_count=drawn,
        omitted_nodes=omitted,
        omitted_edge_count=0,
        warnings=[f"{len(omitted)} component(s) omitted"] if omitted else [],
    )


def generate_sequence_diagram(
    graph: RelationshipGraph,
    *,
    start_symbol: str,
    max_steps: int = 25,
    max_depth: int = 4,
    title: str | None = None,
    filename: str = "sequence-diagram.mmd",
) -> Diagram:
    """Mermaid ``sequenceDiagram`` following CALLS edges from ``start_symbol``.

    Returns a diagram with a ``warnings`` entry and no steps when the call
    evidence is too thin to be worth drawing — an empty sequence diagram is
    more honest than an invented one.
    """
    heading = title or f"Call sequence from {start_symbol}"
    calls_by_source: dict[str, list[GraphEdge]] = {}
    for edge in graph.edges_of([RelationType.CALLS]):
        calls_by_source.setdefault(edge.source_name, []).append(edge)

    matches = [name for name in calls_by_source if name == start_symbol]
    if not matches:
        matches = [
            name for name in calls_by_source if name.rsplit(".", 1)[-1] == start_symbol
        ]
    if not matches:
        return Diagram(
            title=heading,
            kind="sequence",
            mermaid=f"sequenceDiagram\n    %% {escape_label(heading)}\n"
            f"    note over Analysis: no call evidence found for "
            f"{escape_label(start_symbol)}",
            filename=filename,
            warnings=[
                f"No CALLS evidence for {start_symbol!r}; "
                f"no sequence diagram was generated."
            ],
        )

    lines = ["sequenceDiagram", f"    %% {escape_label(heading)}"]
    participants: dict[str, str] = {}
    namer = _Namer()

    def participant(name: str) -> str:
        owner = name.rsplit(".", 1)[0] if "." in name else name
        alias = participants.get(owner)
        if alias is None:
            alias = namer.get(f"participant:{owner}", owner)
            participants[owner] = alias
            lines.append(f'    participant {alias} as {escape_label(owner)}')
        return alias

    steps = 0
    visited: set[str] = set()
    frontier = [(matches[0], 0)]
    while frontier and steps < max_steps:
        current, depth = frontier.pop(0)
        if current in visited or depth >= max_depth:
            continue
        visited.add(current)
        source_alias = participant(current)
        for edge in calls_by_source.get(current, [])[: max_steps - steps]:
            target_alias = participant(edge.target_name)
            note = "" if edge.confidence is Confidence.HIGH else " (inferred)"
            method = escape_label(edge.target_name.rsplit(".", 1)[-1])
            lines.append(f"    {source_alias} ->> {target_alias}: {method}(){note}")
            steps += 1
            frontier.append((edge.target_name, depth + 1))
            if steps >= max_steps:
                break

    warnings: list[str] = []
    if steps == 0:
        warnings.append(
            f"{start_symbol!r} was found but makes no recorded calls; the diagram is empty."
        )
    elif frontier:
        warnings.append(f"Sequence truncated at {max_steps} steps.")
    return Diagram(
        title=heading,
        kind="sequence",
        mermaid="\n".join(lines),
        filename=filename,
        node_count=len(participants),
        edge_count=steps,
        warnings=warnings,
    )


def diagrams_to_markdown(diagrams: Iterable[Diagram]) -> str:
    """Render diagrams as a Markdown document with fenced ``mermaid`` blocks."""
    parts: list[str] = []
    for diagram in diagrams:
        parts.append(f"### {diagram.title}\n")
        parts.append(f"```mermaid\n{diagram.mermaid}\n```\n")
        if diagram.omitted_nodes:
            preview = ", ".join(diagram.omitted_nodes[:20])
            more = (
                f" (+{len(diagram.omitted_nodes) - 20} more)"
                if len(diagram.omitted_nodes) > 20
                else ""
            )
            parts.append(f"> Omitted from this diagram: {preview}{more}\n")
        for warning in diagram.warnings:
            parts.append(f"> {warning}\n")
    return "\n".join(parts)


__all__ = [
    "DEFAULT_MAX_EDGES",
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_MAX_NODES",
    "Diagram",
    "diagrams_to_markdown",
    "escape_label",
    "generate_class_diagram",
    "generate_class_diagrams",
    "generate_component_diagram",
    "generate_inheritance_diagram",
    "generate_package_dependency_diagram",
    "generate_sequence_diagram",
    "sanitize_identifier",
]
