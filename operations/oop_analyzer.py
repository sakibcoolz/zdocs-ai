"""Repository-wide OOP analysis: run the analyzers, then derive what needs
whole-repository context.

Per-file analyzers (:mod:`operations.languages`) can only see one file. Three
important relationships are invisible at that scope and are derived here, once
every file's symbols are known:

* **Go interface implementation** — Go is structural: a type implements an
  interface by having its method set. Confirmed by comparing normalized
  signatures, never by text search.
* **Python/TypeScript "implements"** — a base class that turns out to be a
  ``Protocol``/ABC/abstract class is an implementation relationship, not plain
  inheritance.
* **Method overriding** — a method that shadows one on an ancestor.

Everything derived here is marked
:attr:`~operations.schemas.DetectionMethod.DERIVED` and carries a confidence
that reflects how much of the evidence is structural versus name-based.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from operations.command_runner import CommandRunner
from operations.inventory import SUPPORTED_LANGUAGES, discover_files
from operations.languages import analyzer_for_path
from operations.policy import ExecutionPolicy
from operations.schemas import (
    Confidence,
    DetectionMethod,
    FileAnalysis,
    RelationshipInfo,
    RelationType,
    SymbolInfo,
    SymbolType,
)
from operations.symbol_search import read_text_file
from pydantic import BaseModel, Field

#: Symbol kinds that can act as a type in a relationship graph.
TYPE_KINDS: frozenset[SymbolType] = frozenset(
    {
        SymbolType.CLASS,
        SymbolType.ABSTRACT_CLASS,
        SymbolType.INTERFACE,
        SymbolType.STRUCT,
        SymbolType.ENUM,
        SymbolType.RECORD,
    }
)

#: Symbol kinds that are callable members of a type.
MEMBER_KINDS: frozenset[SymbolType] = frozenset(
    {SymbolType.METHOD, SymbolType.CONSTRUCTOR}
)

#: Kinds that behave as an abstraction others can implement.
ABSTRACTION_KINDS: frozenset[SymbolType] = frozenset(
    {SymbolType.INTERFACE, SymbolType.ABSTRACT_CLASS}
)


class RepositoryAnalysis(BaseModel):
    """Merged result of analyzing every supported file in a repository."""

    repository: str = ""
    files_analyzed: int = 0
    files_skipped: int = 0
    languages: list[str] = Field(default_factory=list)
    symbols: list[SymbolInfo] = Field(default_factory=list)
    relationships: list[RelationshipInfo] = Field(default_factory=list)
    imports_by_module: dict[str, list[str]] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    truncated: bool = False

    def types(self) -> list[SymbolInfo]:
        """Every class/interface/struct/enum/record declared in the repository."""
        return [symbol for symbol in self.symbols if symbol.symbol_type in TYPE_KINDS]

    def members_of(self, type_name: str) -> list[SymbolInfo]:
        """Methods and constructors owned by ``type_name``."""
        return [
            symbol
            for symbol in self.symbols
            if symbol.owner == type_name and symbol.symbol_type in MEMBER_KINDS
        ]

    def summary(self) -> dict[str, object]:
        """Counts by symbol kind, relationship type, visibility and language."""
        by_kind: dict[str, int] = defaultdict(int)
        by_visibility: dict[str, int] = defaultdict(int)
        by_language: dict[str, int] = defaultdict(int)
        for symbol in self.symbols:
            by_kind[symbol.symbol_type.value] += 1
            by_visibility[symbol.visibility.value] += 1
            by_language[symbol.language] += 1
        by_relation: dict[str, int] = defaultdict(int)
        by_confidence: dict[str, int] = defaultdict(int)
        for relationship in self.relationships:
            by_relation[relationship.relation.value] += 1
            by_confidence[relationship.confidence.value] += 1
        return {
            "files_analyzed": self.files_analyzed,
            "files_skipped": self.files_skipped,
            "symbol_count": len(self.symbols),
            "relationship_count": len(self.relationships),
            "symbols_by_kind": dict(sorted(by_kind.items())),
            "symbols_by_visibility": dict(sorted(by_visibility.items())),
            "symbols_by_language": dict(sorted(by_language.items())),
            "relationships_by_type": dict(sorted(by_relation.items())),
            "relationships_by_confidence": dict(sorted(by_confidence.items())),
        }

    def polymorphism(self) -> dict[str, list[str]]:
        """Abstraction → the concrete types found implementing it."""
        implementers: dict[str, set[str]] = defaultdict(set)
        for relationship in self.relationships:
            if relationship.relation is RelationType.IMPLEMENTS:
                implementers[relationship.target].add(relationship.source)
        return {
            abstraction: sorted(types)
            for abstraction, types in sorted(implementers.items())
        }


def analyze_repository(
    root: Path,
    policy: ExecutionPolicy,
    runner: CommandRunner | None = None,
    *,
    repository: str = "",
    languages: set[str] | None = None,
    subdir: str | None = None,
    max_files: int | None = None,
) -> RepositoryAnalysis:
    """Analyze every supported source file under ``root``.

    Args:
        root: Resolved repository root.
        policy: Active execution policy (file-size and count limits).
        runner: Optional command runner for the ripgrep file-discovery path.
        repository: Name recorded on the result.
        languages: Restrict analysis to these languages (default: all supported).
        subdir: Restrict analysis to a repository-relative subdirectory.
        max_files: Extra cap on analyzed files.

    Returns:
        A :class:`RepositoryAnalysis` with per-file findings merged and
        whole-repository relationships derived.
    """
    selected = (languages or set(SUPPORTED_LANGUAGES)) & set(SUPPORTED_LANGUAGES)
    files, truncated, _ = discover_files(
        root, policy, runner, subdir=subdir, languages=selected, limit=max_files
    )

    analysis = RepositoryAnalysis(repository=repository, truncated=truncated)
    imports: dict[str, list[str]] = defaultdict(list)
    seen_languages: set[str] = set()

    for repo_file in files:
        analyzer = analyzer_for_path(repo_file.path)
        if analyzer is None:
            analysis.files_skipped += 1
            continue
        source = read_text_file(root / repo_file.path, policy)
        if source is None:
            analysis.files_skipped += 1
            analysis.warnings.append(
                f"Skipped (binary or larger than {policy.max_file_bytes} bytes): "
                f"{repo_file.path}"
            )
            continue
        file_analysis = analyzer.analyze(repo_file.path, source)
        _merge(analysis, file_analysis, imports, seen_languages)

    analysis.imports_by_module = {
        module: sorted(set(paths)) for module, paths in sorted(imports.items())
    }
    analysis.languages = sorted(seen_languages)

    analysis.relationships.extend(derive_implementations(analysis))
    analysis.relationships.extend(derive_overrides(analysis))
    return analysis


def _merge(
    analysis: RepositoryAnalysis,
    file_analysis: FileAnalysis,
    imports: dict[str, list[str]],
    seen_languages: set[str],
) -> None:
    analysis.files_analyzed += 1
    analysis.symbols.extend(file_analysis.symbols)
    analysis.relationships.extend(file_analysis.relationships)
    analysis.errors.extend(file_analysis.errors)
    seen_languages.add(file_analysis.language)
    module = file_analysis.package or file_analysis.file_path
    imports[module].extend(file_analysis.imports)


# --------------------------------------------------------------------------
# Whole-repository derivations
# --------------------------------------------------------------------------


def derive_implementations(analysis: RepositoryAnalysis) -> list[RelationshipInfo]:
    """Derive IMPLEMENTS edges that no single file could establish."""
    derived: list[RelationshipInfo] = []
    derived.extend(_derive_structural_implementations(analysis))
    derived.extend(_derive_abstract_base_implementations(analysis))
    return derived


def _method_signatures(analysis: RepositoryAnalysis, owner: str) -> dict[str, str]:
    """``{method name: normalized signature}`` for one type."""
    return {
        symbol.name: symbol.signature
        for symbol in analysis.members_of(owner)
        if symbol.symbol_type is SymbolType.METHOD
    }


def _derive_structural_implementations(
    analysis: RepositoryAnalysis,
) -> list[RelationshipInfo]:
    """Go interface satisfaction, by comparing method sets.

    An interface is implemented when the candidate type has a method for every
    interface method. Confidence is ``HIGH`` when every normalized signature
    matches exactly and ``MEDIUM`` when only the method *names* line up (the
    lexical parser could not normalize one of the signatures identically).

    Empty interfaces (``interface{}``/``any``) are skipped: everything
    satisfies them, so the edges would be noise rather than information.
    """
    interfaces = [
        symbol
        for symbol in analysis.symbols
        if symbol.symbol_type is SymbolType.INTERFACE and symbol.language == "go"
    ]
    candidates = [
        symbol
        for symbol in analysis.symbols
        if symbol.language == "go" and symbol.symbol_type in (SymbolType.STRUCT, SymbolType.TYPE_ALIAS)
    ]
    derived: list[RelationshipInfo] = []
    for interface in interfaces:
        required = _method_signatures(analysis, interface.name)
        if not required:
            continue
        for candidate in candidates:
            provided = _method_signatures(analysis, candidate.name)
            if not set(required).issubset(provided):
                continue
            exact = all(required[name] == provided[name] for name in required)
            derived.append(
                RelationshipInfo(
                    source=candidate.name,
                    target=interface.name,
                    relation=RelationType.IMPLEMENTS,
                    file_path=candidate.file_path,
                    line=candidate.line,
                    language="go",
                    detection_method=DetectionMethod.DERIVED,
                    confidence=Confidence.HIGH if exact else Confidence.MEDIUM,
                    metadata={
                        "kind": "structural_satisfaction",
                        "interface_file": interface.file_path,
                        "interface_line": interface.line,
                        "methods": sorted(required),
                        "signature_match": "exact" if exact else "name_only",
                        "note": "Go has no `implements` keyword; satisfaction is "
                        "inferred from the method set",
                    },
                )
            )
    return derived


def _derive_abstract_base_implementations(
    analysis: RepositoryAnalysis,
) -> list[RelationshipInfo]:
    """Promote inheritance from an abstraction to an IMPLEMENTS edge.

    Only for languages without an explicit ``implements`` keyword (Python), and
    only when the base name resolves to exactly one declared abstraction — an
    ambiguous name is left alone rather than guessed at.
    """
    abstractions: dict[str, list[SymbolInfo]] = defaultdict(list)
    for symbol in analysis.symbols:
        if symbol.symbol_type in ABSTRACTION_KINDS:
            abstractions[symbol.name].append(symbol)

    derived: list[RelationshipInfo] = []
    for relationship in analysis.relationships:
        if relationship.relation is not RelationType.INHERITS:
            continue
        if relationship.language != "python":
            continue
        targets = abstractions.get(relationship.target.rsplit(".", 1)[-1], [])
        if len(targets) != 1:
            continue
        derived.append(
            RelationshipInfo(
                source=relationship.source,
                target=targets[0].name,
                relation=RelationType.IMPLEMENTS,
                file_path=relationship.file_path,
                line=relationship.line,
                language=relationship.language,
                detection_method=DetectionMethod.DERIVED,
                confidence=Confidence.MEDIUM,
                metadata={
                    "kind": "abstract_base",
                    "note": "base class is declared abstract/Protocol; "
                    "Python expresses implementation as inheritance",
                    "base_kind": targets[0].symbol_type.value,
                },
            )
        )
    return derived


def derive_overrides(analysis: RepositoryAnalysis) -> list[RelationshipInfo]:
    """Find methods that shadow a method on an ancestor type.

    The ancestor walk uses declared names only. A name that resolves to several
    types, or to none, is skipped — inventing an ancestry would produce
    confident-looking nonsense.
    """
    parents: dict[str, set[str]] = defaultdict(set)
    for relationship in analysis.relationships:
        if relationship.relation in (RelationType.INHERITS, RelationType.IMPLEMENTS):
            if relationship.metadata.get("kind") == "method_override":
                continue
            parents[relationship.source].add(relationship.target.rsplit(".", 1)[-1])

    methods_by_owner: dict[str, dict[str, SymbolInfo]] = defaultdict(dict)
    for symbol in analysis.symbols:
        if symbol.owner and symbol.symbol_type is SymbolType.METHOD:
            methods_by_owner[symbol.owner].setdefault(symbol.name, symbol)

    derived: list[RelationshipInfo] = []
    for owner, methods in sorted(methods_by_owner.items()):
        for ancestor in _ancestors(owner, parents):
            inherited = methods_by_owner.get(ancestor, {})
            for name, symbol in sorted(methods.items()):
                parent_symbol = inherited.get(name)
                if parent_symbol is None:
                    continue
                exact = parent_symbol.signature == symbol.signature
                derived.append(
                    RelationshipInfo(
                        source=f"{owner}.{name}",
                        target=f"{ancestor}.{name}",
                        relation=RelationType.INHERITS,
                        file_path=symbol.file_path,
                        line=symbol.line,
                        language=symbol.language,
                        detection_method=DetectionMethod.DERIVED,
                        confidence=Confidence.HIGH
                        if (exact or symbol.metadata.get("is_override"))
                        else Confidence.MEDIUM,
                        metadata={
                            "kind": "method_override",
                            "owner": owner,
                            "ancestor": ancestor,
                            "parent_file": parent_symbol.file_path,
                            "parent_line": parent_symbol.line,
                            "parent_is_abstract": parent_symbol.is_abstract,
                            "signature_match": "exact" if exact else "name_only",
                            "annotated_override": bool(symbol.metadata.get("is_override")),
                        },
                    )
                )
    return derived


def _ancestors(type_name: str, parents: dict[str, set[str]], depth: int = 8) -> list[str]:
    """Transitive parents of ``type_name``, breadth-first, cycle-safe."""
    seen: set[str] = set()
    frontier = list(parents.get(type_name, ()))
    ordered: list[str] = []
    while frontier and depth > 0:
        depth -= 1
        next_frontier: list[str] = []
        for name in frontier:
            if name in seen or name == type_name:
                continue
            seen.add(name)
            ordered.append(name)
            next_frontier.extend(parents.get(name, ()))
        frontier = next_frontier
    return ordered


__all__ = [
    "ABSTRACTION_KINDS",
    "MEMBER_KINDS",
    "RepositoryAnalysis",
    "TYPE_KINDS",
    "analyze_repository",
    "derive_implementations",
    "derive_overrides",
]
