"""Optional tree-sitter backend for the language analyzers.

The lexical analyzers in this package work on a stock Python install and are
what the project depends on. When the ``tree-sitter`` bindings *and* a grammar
for a language are installed, the analyzers below replace them: a real parser
produces a real syntax tree, so declarations stop being pattern matches and
become facts — which is why these analyzers report
:attr:`~operations.schemas.Confidence.HIGH` and
:attr:`~operations.schemas.DetectionMethod.TREE_SITTER`.

Availability is decided per language, at import time, and never fails loudly:
:func:`is_available` returning ``False`` simply means the registry keeps the
lexical analyzer for that language. Nothing here is a hard dependency, and the
test suite covers both backends.

Install the optional extras with::

    pip install -r requirements-analyzers.txt
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterator

from operations.languages.base import LanguageAnalyzer
from operations.schemas import (
    Confidence,
    DetectionMethod,
    FileAnalysis,
    RelationshipInfo,
    RelationType,
    SymbolInfo,
)

#: Language name → (grammar module, factory attribute on that module).
#: Python is deliberately absent: the standard library's ``ast`` module is a
#: first-party parser for it, so a grammar would add a dependency for nothing.
GRAMMAR_MODULES: dict[str, tuple[str, str]] = {
    "go": ("tree_sitter_go", "language"),
    "java": ("tree_sitter_java", "language"),
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
}


@lru_cache(maxsize=1)
def bindings_available() -> bool:
    """Whether the ``tree_sitter`` bindings themselves are importable."""
    try:
        import tree_sitter  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=None)
def get_parser(language: str) -> Any | None:
    """Cached tree-sitter ``Parser`` for ``language``, or ``None`` if absent.

    Returns ``None`` — rather than raising — for every failure mode (bindings
    missing, grammar missing, or an incompatible binding/grammar ABI pair), so
    a partially installed environment degrades to the lexical analyzers instead
    of breaking analysis.
    """
    entry = GRAMMAR_MODULES.get(language)
    if entry is None or not bindings_available():
        return None
    module_name, factory_name = entry
    try:
        from importlib import import_module

        from tree_sitter import Language, Parser

        grammar = import_module(module_name)
        return Parser(Language(getattr(grammar, factory_name)()))
    except Exception:  # noqa: BLE001 - any failure means "use the fallback"
        return None


def is_available(language: str) -> bool:
    """Whether a working tree-sitter parser exists for ``language``."""
    return get_parser(language) is not None


def available_languages() -> list[str]:
    """Languages with a usable tree-sitter grammar installed."""
    return sorted(name for name in GRAMMAR_MODULES if is_available(name))


# --------------------------------------------------------------------------
# Traversal helpers
# --------------------------------------------------------------------------


class Node:
    """Minimal typing shim — the real object is a ``tree_sitter.Node``."""


def text(source: bytes, node: Any) -> str:
    """UTF-8 source text covered by ``node``."""
    return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")


def line_of(node: Any) -> int:
    """1-based start line of ``node`` (tree-sitter rows are 0-based)."""
    return node.start_point[0] + 1


def end_line_of(node: Any) -> int:
    """1-based end line of ``node``."""
    return node.end_point[0] + 1


def child(node: Any, field_name: str) -> Any | None:
    """Named child of ``node`` under ``field_name``, or ``None``."""
    return node.child_by_field_name(field_name) if node is not None else None


def children_of_type(node: Any, *types: str) -> list[Any]:
    """Direct named children of ``node`` whose type is one of ``types``."""
    if node is None:
        return []
    wanted = set(types)
    return [item for item in node.named_children if item.type in wanted]


def first_of_type(node: Any, *types: str) -> Any | None:
    """First direct named child matching ``types``."""
    found = children_of_type(node, *types)
    return found[0] if found else None


def walk(node: Any, *, skip: set[str] | None = None) -> Iterator[Any]:
    """Depth-first walk over named nodes, optionally pruning whole subtrees.

    ``skip`` prunes by node type — used to stop a class-body walk from
    descending into a nested class and attributing its members to the outer one.
    """
    skipped = skip or set()
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        for candidate in reversed(current.named_children):
            if candidate.type not in skipped:
                stack.append(candidate)


def descendants_of_type(node: Any, *types: str, skip: set[str] | None = None) -> list[Any]:
    """All descendants of ``node`` (inclusive) whose type is in ``types``."""
    wanted = set(types)
    return [item for item in walk(node, skip=skip) if item.type in wanted]


def has_error(root: Any) -> bool:
    """Whether the parse produced an ERROR or MISSING node."""
    return bool(root.has_error)


# --------------------------------------------------------------------------
# Shared accumulator
# --------------------------------------------------------------------------


@dataclass
class TsState:
    """Mutable accumulator threaded through one file's tree-sitter parse."""

    file_path: str
    source: bytes
    root: Any
    language: str
    package: str = ""
    symbols: list[SymbolInfo] = field(default_factory=list)
    relationships: list[RelationshipInfo] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    call_count: int = 0
    #: Local variable / receiver name → declared type, used to resolve the
    #: receiver of a method call to a concrete type (see ``resolve_receiver``).
    scope_types: dict[str, str] = field(default_factory=dict)
    #: ``Type.field`` → declared field type, for the same purpose.
    field_types: dict[str, str] = field(default_factory=dict)

    def text(self, node: Any) -> str:
        return text(self.source, node)

    def add_symbol(self, **kwargs: object) -> SymbolInfo:
        symbol = SymbolInfo(
            file_path=self.file_path,
            language=self.language,
            package=self.package,
            detection_method=DetectionMethod.TREE_SITTER,
            confidence=Confidence.HIGH,
            **kwargs,  # type: ignore[arg-type]
        )
        self.symbols.append(symbol)
        return symbol

    def add_relationship(self, **kwargs: object) -> None:
        kwargs.setdefault("detection_method", DetectionMethod.TREE_SITTER)
        kwargs.setdefault("confidence", Confidence.HIGH)
        self.relationships.append(
            RelationshipInfo(
                file_path=self.file_path,
                language=self.language,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    def add_import(self, module: str, line: int) -> None:
        if not module:
            return
        self.imports.append(module)
        self.add_relationship(
            source=self.package or self.file_path,
            target=module,
            relation=RelationType.IMPORTS,
            line=line,
            metadata={"kind": "import"},
        )

    def result(self) -> FileAnalysis:
        return FileAnalysis(
            file_path=self.file_path,
            language=self.language,
            package=self.package,
            symbols=self.symbols,
            relationships=self.relationships,
            imports=sorted(set(self.imports)),
            errors=self.errors,
        )


class TreeSitterAnalyzer(LanguageAnalyzer):
    """Base for analyzers backed by a real tree-sitter grammar.

    Subclasses implement :meth:`extract`, which walks the parsed tree and fills
    a :class:`TsState`. A parse failure (no grammar at load time, or a tree
    riddled with ERROR nodes) is reported in ``FileAnalysis.errors`` rather than
    raised, so one unparsable file never aborts a repository analysis.
    """

    detection_method = DetectionMethod.TREE_SITTER
    base_confidence = Confidence.HIGH
    #: Grammar key in :data:`GRAMMAR_MODULES` (defaults to ``language``).
    grammar: str = ""

    @property
    def grammar_name(self) -> str:
        return self.grammar or self.language

    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        parser = get_parser(self.grammar_name)
        if parser is None:  # pragma: no cover - registry never selects us then
            return self.empty(
                file_path,
                f"tree-sitter grammar for {self.grammar_name!r} is not installed",
            )
        raw = source.encode("utf-8")
        tree = parser.parse(raw)
        state = TsState(
            file_path=file_path, source=raw, root=tree.root_node, language=self.language
        )
        if has_error(tree.root_node):
            # Partial trees are still useful: tree-sitter recovers around the
            # broken region, so we extract what parsed and say so.
            state.errors.append(
                f"tree-sitter reported syntax errors in {file_path}; "
                f"results for this file may be incomplete"
            )
        self.extract(state)
        return state.result()

    def extract(self, state: TsState) -> None:
        """Walk ``state.root`` and populate ``state``."""
        raise NotImplementedError


# --------------------------------------------------------------------------
# Receiver resolution (shared by the Go/Java/JS-TS extractors)
# --------------------------------------------------------------------------


def resolve_receiver(
    state: TsState,
    receiver: str,
    *,
    self_type: str | None,
    self_names: tuple[str, ...] = ("this", "self"),
) -> tuple[str | None, str]:
    """Resolve a call receiver expression to a declared type.

    Turns ``m.auditor.Record()`` into ``Auditor.Record`` when ``m`` is a
    receiver of type ``MemoryStore`` and ``MemoryStore.auditor`` is declared as
    an ``Auditor``. Returns ``(type_name, how)`` where ``how`` is one of
    ``"self"``, ``"variable"``, ``"field"`` or ``"unresolved"``.

    Only *declared* types are followed — one hop through a field at a time, no
    guessing. An unresolved receiver yields ``(None, "unresolved")`` and the
    caller records a name-only call rather than inventing a type.
    """
    parts = [part for part in receiver.split(".") if part]
    if not parts:
        return None, "unresolved"

    head, *rest = parts
    if head in self_names and self_type:
        current, how = self_type, "self"
    elif head in state.scope_types:
        current, how = state.scope_types[head], "variable"
    else:
        return None, "unresolved"

    for attribute in rest:
        field_type = state.field_types.get(f"{current}.{attribute}")
        if field_type is None:
            return None, "unresolved"
        current, how = field_type, "field"
    return current, how


__all__ = [
    "GRAMMAR_MODULES",
    "TreeSitterAnalyzer",
    "TsState",
    "available_languages",
    "bindings_available",
    "child",
    "children_of_type",
    "descendants_of_type",
    "end_line_of",
    "first_of_type",
    "get_parser",
    "has_error",
    "is_available",
    "line_of",
    "resolve_receiver",
    "text",
    "walk",
]
