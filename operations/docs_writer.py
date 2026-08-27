"""Write analysis artefacts to ``generated-docs/``.

Output is namespaced per repository (``generated-docs/<repository>/``) because
several repositories can be staged at once and flat filenames would collide.
The root is configurable so tests write to a temporary directory.

Nothing here ever writes inside a staged repository: the analysis profile is
read-only with respect to the code it analyzes. Repository names are
re-validated before use as a directory component even though they arrive from
the stage registry — defence in depth against a future caller that does not.

Secrets are redacted from every fragment written, so a credential committed to
an analyzed repository cannot reach generated documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from operations.diagram_generator import Diagram, diagrams_to_markdown
from operations.errors import PathEscapeError
from operations.oop_analyzer import RepositoryAnalysis
from operations.policy import redact
from operations.relationship_graph import RelationshipGraph
from operations.schemas import RelationType

DEFAULT_DOCS_DIRNAME = "generated-docs"

_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def safe_component(name: str) -> str:
    """Validate a single path component (no separators, no ``..``)."""
    candidate = (name or "").strip()
    if not candidate or candidate in (".", "..") or not _SAFE_NAME.match(candidate):
        raise PathEscapeError(f"Unsafe output directory name: {name!r}")
    return candidate


class DocsWriter:
    """Creates the ``generated-docs/<repository>/`` tree and writes into it."""

    def __init__(self, output_root: str | Path, repository: str) -> None:
        self.output_root = Path(output_root).resolve()
        self.repository = safe_component(repository)
        self.repo_dir = self.output_root / self.repository
        self.diagrams_dir = self.repo_dir / "diagrams"

    def ensure_dirs(self) -> None:
        self.diagrams_dir.mkdir(parents=True, exist_ok=True)

    # -- writes ------------------------------------------------------------

    def write_diagram(self, diagram: Diagram) -> Path:
        """Write one ``.mmd`` file and return its path."""
        self.ensure_dirs()
        path = self.diagrams_dir / safe_component(diagram.filename)
        path.write_text(redact(diagram.mermaid) + "\n", encoding="utf-8")
        return path

    def write_markdown(self, filename: str, content: str) -> Path:
        """Write one Markdown document and return its path."""
        self.ensure_dirs()
        path = self.repo_dir / safe_component(filename)
        path.write_text(redact(content), encoding="utf-8")
        return path

    def write_bundle(
        self,
        analysis: RepositoryAnalysis,
        graph: RelationshipGraph,
        diagrams: list[Diagram],
    ) -> list[Path]:
        """Write the full documentation set for one analysis run."""
        written = [
            self.write_markdown("OOP_ANALYSIS.md", oop_analysis_markdown(analysis, graph, diagrams)),
            self.write_markdown("CLASS_CATALOG.md", class_catalog_markdown(analysis)),
            self.write_markdown(
                "INTERFACE_IMPLEMENTATIONS.md", implementations_markdown(analysis)
            ),
            self.write_markdown("FUNCTION_CALL_GRAPH.md", call_graph_markdown(analysis, graph)),
        ]
        written.extend(self.write_diagram(diagram) for diagram in diagrams)
        return written

    # -- reads -------------------------------------------------------------

    def list_diagrams(self) -> list[dict[str, object]]:
        """Metadata for every ``.mmd`` already generated for this repository."""
        if not self.diagrams_dir.is_dir():
            return []
        entries = []
        for path in sorted(self.diagrams_dir.glob("*.mmd")):
            stat = path.stat()
            entries.append(
                {
                    "filename": path.name,
                    "path": str(path),
                    "relative_path": f"{self.repository}/diagrams/{path.name}",
                    "bytes": stat.st_size,
                    "modified_epoch": int(stat.st_mtime),
                }
            )
        return entries

    def list_documents(self) -> list[str]:
        """Names of the Markdown documents generated for this repository."""
        if not self.repo_dir.is_dir():
            return []
        return sorted(path.name for path in self.repo_dir.glob("*.md"))


# --------------------------------------------------------------------------
# Markdown rendering
# --------------------------------------------------------------------------


def _confidence_note() -> str:
    return (
        "Confidence key: **high** — read from a real parser or an explicit "
        "declaration; **medium** — lexical parse or structural inference; "
        "**low** — text-search candidate only. Relationships marked *derived* "
        "were inferred across files and are evidence, not conclusions.\n"
    )


def oop_analysis_markdown(
    analysis: RepositoryAnalysis, graph: RelationshipGraph, diagrams: list[Diagram]
) -> str:
    """Top-level OOP report: counts, languages, diagrams, limitations."""
    summary = analysis.summary()
    lines = [
        f"# OOP analysis — {analysis.repository or 'repository'}",
        "",
        _confidence_note(),
        "## Scope",
        "",
        f"- Files analyzed: **{summary['files_analyzed']}** "
        f"(skipped: {summary['files_skipped']})",
        f"- Languages: {', '.join(analysis.languages) or 'none detected'}",
        f"- Symbols: **{summary['symbol_count']}**, "
        f"relationships: **{summary['relationship_count']}**",
        "",
        "## Symbols by kind",
        "",
        "| Kind | Count |",
        "| --- | ---: |",
    ]
    for kind, count in summary["symbols_by_kind"].items():  # type: ignore[union-attr]
        lines.append(f"| {kind} | {count} |")

    lines += ["", "## Relationships by type", "", "| Relationship | Count |", "| --- | ---: |"]
    for relation, count in summary["relationships_by_type"].items():  # type: ignore[union-attr]
        lines.append(f"| {relation} | {count} |")

    lines += ["", "## Encapsulation", "", "| Visibility | Symbols |", "| --- | ---: |"]
    for visibility, count in summary["symbols_by_visibility"].items():  # type: ignore[union-attr]
        lines.append(f"| {visibility} | {count} |")

    polymorphism = analysis.polymorphism()
    lines += ["", "## Polymorphic abstractions", ""]
    if polymorphism:
        lines += ["| Abstraction | Implementations |", "| --- | --- |"]
        for abstraction, implementers in polymorphism.items():
            lines.append(f"| `{abstraction}` | {', '.join(f'`{name}`' for name in implementers)} |")
    else:
        lines.append("_No interface/abstract-base implementations were detected._")

    if diagrams:
        lines += ["", "## Diagrams", "", diagrams_to_markdown(diagrams)]

    lines += ["", "## Graph statistics", "", "```json", _json_block(graph.stats()), "```"]

    if analysis.warnings or analysis.errors or graph.warnings:
        lines += ["", "## Limitations of this run", ""]
        for note in [*analysis.errors, *analysis.warnings, *graph.warnings]:
            lines.append(f"- {note}")
    return "\n".join(lines) + "\n"


def class_catalog_markdown(analysis: RepositoryAnalysis) -> str:
    """Every declared type, where it lives and what it contains."""
    lines = [
        f"# Class catalog — {analysis.repository or 'repository'}",
        "",
        _confidence_note(),
        "| Type | Kind | Language | Package | File:line | Visibility | Members |",
        "| --- | --- | --- | --- | --- | --- | ---: |",
    ]
    for symbol in sorted(analysis.types(), key=lambda item: (item.language, item.package, item.name)):
        members = len(analysis.members_of(symbol.name))
        lines.append(
            f"| `{symbol.name}` | {symbol.symbol_type.value} | {symbol.language} | "
            f"`{symbol.package or '-'}` | `{symbol.file_path}:{symbol.line}` | "
            f"{symbol.visibility.value} | {members} |"
        )
    if len(lines) == 5:
        lines.append("| _no types found_ |  |  |  |  |  |  |")
    return "\n".join(lines) + "\n"


def implementations_markdown(analysis: RepositoryAnalysis) -> str:
    """Interface/abstract-base implementation evidence, with detection method."""
    rows = [
        relationship
        for relationship in analysis.relationships
        if relationship.relation is RelationType.IMPLEMENTS
    ]
    lines = [
        f"# Interface implementations — {analysis.repository or 'repository'}",
        "",
        _confidence_note(),
        "| Implementation | Interface | Evidence | Detection | Confidence | Notes |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for relationship in sorted(rows, key=lambda item: (item.target, item.source)):
        note = str(relationship.metadata.get("note") or relationship.metadata.get("kind") or "")
        lines.append(
            f"| `{relationship.source}` | `{relationship.target}` | "
            f"`{relationship.file_path}:{relationship.line}` | "
            f"{relationship.detection_method.value} | {relationship.confidence.value} | "
            f"{note} |"
        )
    if not rows:
        lines.append("| _none detected_ |  |  |  |  |  |")
    return "\n".join(lines) + "\n"


def call_graph_markdown(analysis: RepositoryAnalysis, graph: RelationshipGraph) -> str:
    """Call relationships grouped by caller, resolved ones first."""
    calls = [
        relationship
        for relationship in analysis.relationships
        if relationship.relation is RelationType.CALLS
    ]
    declared = {symbol.name for symbol in analysis.symbols} | {
        symbol.qualified_name for symbol in analysis.symbols
    }
    internal = [call for call in calls if call.target.rsplit(".", 1)[-1] in declared]

    lines = [
        f"# Function call graph — {analysis.repository or 'repository'}",
        "",
        _confidence_note(),
        f"- Total call sites recorded: **{len(calls)}**",
        f"- Calls resolving to a symbol declared in this repository: **{len(internal)}**",
        "",
        "Only repository-internal calls are listed; calls into third-party or "
        "standard-library code are counted above but not enumerated.",
        "",
        "| Caller | Callee | Evidence | Confidence |",
        "| --- | --- | --- | --- |",
    ]
    for call in sorted(internal, key=lambda item: (item.source, item.target, item.line))[:500]:
        lines.append(
            f"| `{call.source}` | `{call.target}` | "
            f"`{call.file_path}:{call.line}` | {call.confidence.value} |"
        )
    if len(internal) > 500:
        lines.append(f"| _… {len(internal) - 500} more call sites not listed_ |  |  |  |")
    if not internal:
        lines.append("| _no internal calls detected_ |  |  |  |")
    return "\n".join(lines) + "\n"


def _json_block(payload: dict[str, object]) -> str:
    import json

    return json.dumps(payload, indent=2, sort_keys=True, default=str)


__all__ = [
    "DEFAULT_DOCS_DIRNAME",
    "DocsWriter",
    "call_graph_markdown",
    "class_catalog_markdown",
    "implementations_markdown",
    "oop_analysis_markdown",
    "safe_component",
]
