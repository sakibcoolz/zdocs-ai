"""Tests for the optional tree-sitter backend.

Two things must hold, and they pull in opposite directions:

* **Parity** — the tree-sitter and lexical backends must agree on declarations
  and structural relationships. Otherwise installing an optional dependency
  would silently change what the platform reports.
* **Upgrade** — tree-sitter must actually add something: higher confidence, and
  call receivers resolved to concrete types.

Every test here skips cleanly when tree-sitter is not installed, so the suite
passes on a bare machine — the configuration the project promises to support.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.languages import (
    LEXICAL_ANALYZERS,
    TREE_SITTER_ANALYZERS,
    analyzer_backends,
    get_analyzer,
    get_lexical_analyzer,
)
from operations.languages.base import LanguageAnalyzer
from operations.languages.go_common import element_type, receiver_type
from operations.languages.tree_sitter_support import (
    GRAMMAR_MODULES,
    TsState,
    available_languages,
    bindings_available,
    get_parser,
    is_available,
    resolve_receiver,
)
from operations.oop_analyzer import analyze_repository
from operations.policy import ExecutionPolicy, resolve_repo_root
from operations.schemas import Confidence, DetectionMethod, RelationType, SymbolType

#: Language → the fixture file exercised for that language.
FIXTURE_FILES = {
    "go": "go_repo/store.go",
    "java": "java_repo/src/com/example/Shapes.java",
    "javascript": "js_repo/src/animals.js",
    "typescript": "ts_repo/src/shapes.ts",
}

requires_tree_sitter = pytest.mark.skipif(
    not bindings_available(), reason="tree-sitter bindings are not installed"
)


def _requires_grammar(language: str) -> None:
    if not is_available(language):
        pytest.skip(f"tree-sitter grammar for {language!r} is not installed")


def declarations(analysis) -> set[tuple[str, str]]:
    return {(item.symbol_type.value, item.qualified_name) for item in analysis.symbols}


def structural(analysis) -> set[tuple[str, str, str]]:
    """Relationships excluding calls, which the backends deliberately differ on."""
    return {
        (item.relation.value, item.source, item.target)
        for item in analysis.relationships
        if item.relation is not RelationType.CALLS
    }


def visibilities(analysis) -> set[tuple[str, str]]:
    return {(item.qualified_name, item.visibility.value) for item in analysis.symbols}


# --------------------------------------------------------------------------
# Availability and registry wiring
# --------------------------------------------------------------------------


def test_missing_bindings_degrade_instead_of_raising() -> None:
    # An unknown grammar must behave exactly like an uninstalled one.
    assert get_parser("cobol") is None
    assert is_available("cobol") is False


def test_registry_always_resolves_every_language() -> None:
    # With or without tree-sitter, every supported language has an analyzer.
    backends = analyzer_backends()
    assert set(backends) == set(LEXICAL_ANALYZERS)
    assert all(backend for backend in backends.values())


def test_python_never_uses_tree_sitter() -> None:
    # The standard library is a first-party parser; a grammar would add a
    # dependency for nothing.
    assert "python" not in GRAMMAR_MODULES
    assert "python" not in TREE_SITTER_ANALYZERS
    assert analyzer_backends()["python"] == DetectionMethod.PYTHON_AST.value


def test_lexical_analyzer_is_always_reachable() -> None:
    for language in LEXICAL_ANALYZERS:
        analyzer = get_lexical_analyzer(language)
        assert isinstance(analyzer, LanguageAnalyzer)
        assert analyzer.detection_method is not DetectionMethod.TREE_SITTER


@requires_tree_sitter
def test_registry_prefers_tree_sitter_when_available() -> None:
    backends = analyzer_backends()
    for language in TREE_SITTER_ANALYZERS:
        if is_available(language):
            assert backends[language] == DetectionMethod.TREE_SITTER.value


@requires_tree_sitter
def test_available_languages_are_a_subset_of_known_grammars() -> None:
    assert set(available_languages()) <= set(GRAMMAR_MODULES)


# --------------------------------------------------------------------------
# Parity between the two backends
# --------------------------------------------------------------------------


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_backends_agree_on_declarations(language: str, all_fixtures_repo: Path) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    source = path.read_text(encoding="utf-8")
    tree_sitter = TREE_SITTER_ANALYZERS[language]().analyze(path.name, source)
    lexical = LEXICAL_ANALYZERS[language]().analyze(path.name, source)
    assert declarations(tree_sitter) == declarations(lexical)


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_backends_agree_on_structural_relationships(
    language: str, all_fixtures_repo: Path
) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    source = path.read_text(encoding="utf-8")
    tree_sitter = TREE_SITTER_ANALYZERS[language]().analyze(path.name, source)
    lexical = LEXICAL_ANALYZERS[language]().analyze(path.name, source)
    assert structural(tree_sitter) == structural(lexical)


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_backends_agree_on_visibility(language: str, all_fixtures_repo: Path) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    source = path.read_text(encoding="utf-8")
    tree_sitter = TREE_SITTER_ANALYZERS[language]().analyze(path.name, source)
    lexical = LEXICAL_ANALYZERS[language]().analyze(path.name, source)
    assert visibilities(tree_sitter) == visibilities(lexical)


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_backends_agree_on_package_and_imports(
    language: str, all_fixtures_repo: Path
) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    source = path.read_text(encoding="utf-8")
    relative = FIXTURE_FILES[language]
    tree_sitter = TREE_SITTER_ANALYZERS[language]().analyze(relative, source)
    lexical = LEXICAL_ANALYZERS[language]().analyze(relative, source)
    assert tree_sitter.package == lexical.package
    assert tree_sitter.imports == lexical.imports


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_tree_sitter_reports_its_own_provenance(
    language: str, all_fixtures_repo: Path
) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    analysis = TREE_SITTER_ANALYZERS[language]().analyze(
        path.name, path.read_text(encoding="utf-8")
    )
    assert analysis.symbols
    assert all(
        symbol.detection_method is DetectionMethod.TREE_SITTER
        for symbol in analysis.symbols
    )
    assert all(symbol.confidence is Confidence.HIGH for symbol in analysis.symbols)


@pytest.mark.parametrize("language", sorted(TREE_SITTER_ANALYZERS))
def test_tree_sitter_is_deterministic(language: str, all_fixtures_repo: Path) -> None:
    _requires_grammar(language)
    path = all_fixtures_repo / FIXTURE_FILES[language]
    source = path.read_text(encoding="utf-8")
    analyzer = TREE_SITTER_ANALYZERS[language]()
    assert analyzer.analyze(path.name, source) == analyzer.analyze(path.name, source)


@pytest.mark.parametrize("language", sorted(FIXTURE_FILES))
@pytest.mark.parametrize("backend", ["tree_sitter", "lexical"])
def test_symbols_are_emitted_in_source_order(
    backend: str, language: str, all_fixtures_repo: Path
) -> None:
    # Both backends guarantee source order, so a class catalog reads top-to-
    # bottom and switching backends produces no spurious diff.
    if backend == "tree_sitter":
        _requires_grammar(language)
        analyzer = TREE_SITTER_ANALYZERS[language]()
    else:
        analyzer = LEXICAL_ANALYZERS[language]()
    path = all_fixtures_repo / FIXTURE_FILES[language]
    analysis = analyzer.analyze(path.name, path.read_text(encoding="utf-8"))
    lines = [symbol.line for symbol in analysis.symbols]
    assert lines == sorted(lines)


def test_python_symbols_are_also_source_ordered(python_repo: Path) -> None:
    analysis = LEXICAL_ANALYZERS["python"]().analyze(
        "shapes.py", (python_repo / "shapes.py").read_text(encoding="utf-8")
    )
    lines = [symbol.line for symbol in analysis.symbols]
    assert lines == sorted(lines)


# --------------------------------------------------------------------------
# What tree-sitter adds: receiver-typed calls
# --------------------------------------------------------------------------


def test_go_resolves_a_call_through_a_field_type(go_repo: Path) -> None:
    _requires_grammar("go")
    source = (go_repo / "store.go").read_text(encoding="utf-8")
    analysis = TREE_SITTER_ANALYZERS["go"]().analyze("store.go", source)
    calls = {
        (item.source, item.target): item
        for item in analysis.relationships
        if item.relation is RelationType.CALLS
    }
    # `m.auditor.Record(...)`: receiver `m` is the method's receiver
    # (*MemoryStore), whose `auditor` field is declared as an Auditor.
    edge = calls[("MemoryStore.Save", "Auditor.Record")]
    assert edge.metadata["receiver_resolution"] == "field"
    assert edge.confidence is Confidence.HIGH


def test_java_resolves_a_call_through_an_implicit_this_field(java_repo: Path) -> None:
    _requires_grammar("java")
    path = java_repo / "src" / "com" / "example" / "Shapes.java"
    analysis = TREE_SITTER_ANALYZERS["java"]().analyze(
        "Shapes.java", path.read_text(encoding="utf-8")
    )
    targets = {
        item.target
        for item in analysis.relationships
        if item.relation is RelationType.CALLS
    }
    # `logger.log(name)` with no `this.` still resolves through the field.
    assert "Logger.log" in targets


def test_typescript_resolves_a_call_through_this(ts_repo: Path) -> None:
    _requires_grammar("typescript")
    path = ts_repo / "src" / "shapes.ts"
    analysis = TREE_SITTER_ANALYZERS["typescript"]().analyze(
        "src/shapes.ts", path.read_text(encoding="utf-8")
    )
    calls = {
        (item.source, item.target)
        for item in analysis.relationships
        if item.relation is RelationType.CALLS
    }
    assert ("AbstractShape.draw", "Logger.log") in calls


def test_array_receivers_are_not_resolved_to_their_element_type(
    ts_repo: Path,
) -> None:
    _requires_grammar("typescript")
    path = ts_repo / "src" / "shapes.ts"
    analysis = TREE_SITTER_ANALYZERS["typescript"]().analyze(
        "src/shapes.ts", path.read_text(encoding="utf-8")
    )
    targets = {
        item.target
        for item in analysis.relationships
        if item.relation is RelationType.CALLS
    }
    # `shapes: Measurable[]` then `shapes.reduce(...)` is a call on the array.
    # Attributing `reduce` to `Measurable` would invent an interface method.
    assert "Measurable.reduce" not in targets
    assert "reduce" in targets


def test_typescript_records_instantiation(ts_repo: Path) -> None:
    _requires_grammar("typescript")
    path = ts_repo / "src" / "shapes.ts"
    analysis = TREE_SITTER_ANALYZERS["typescript"]().analyze(
        "src/shapes.ts", path.read_text(encoding="utf-8")
    )
    instantiations = {
        (item.source, item.target)
        for item in analysis.relationships
        if item.metadata.get("kind") == "instantiation"
    }
    assert ("Rectangle.constructor", "BoundingBox") in instantiations


def test_unresolved_receivers_are_reported_as_such(go_repo: Path) -> None:
    _requires_grammar("go")
    source = (go_repo / "store.go").read_text(encoding="utf-8")
    analysis = TREE_SITTER_ANALYZERS["go"]().analyze("store.go", source)
    unresolved = [
        item
        for item in analysis.relationships
        if item.relation is RelationType.CALLS
        and item.metadata.get("receiver_resolution") == "unresolved"
    ]
    assert unresolved, "expected stdlib calls (fmt.Sprintf) to stay unresolved"
    # A name-only call must not claim the confidence of a resolved one.
    assert all(item.confidence is Confidence.MEDIUM for item in unresolved)
    assert all("." not in item.target for item in unresolved)


# --------------------------------------------------------------------------
# Receiver resolution unit tests (no grammar needed)
# --------------------------------------------------------------------------


def make_state() -> TsState:
    return TsState(file_path="x", source=b"", root=None, language="go")


def test_resolve_receiver_via_self() -> None:
    state = make_state()
    assert resolve_receiver(state, "this", self_type="Foo", self_names=("this",)) == (
        "Foo",
        "self",
    )


def test_resolve_receiver_via_a_scope_variable() -> None:
    state = make_state()
    state.scope_types["store"] = "MemoryStore"
    assert resolve_receiver(state, "store", self_type=None) == ("MemoryStore", "variable")


def test_resolve_receiver_hops_through_one_field() -> None:
    state = make_state()
    state.scope_types["m"] = "MemoryStore"
    state.field_types["MemoryStore.auditor"] = "Auditor"
    assert resolve_receiver(state, "m.auditor", self_type=None) == ("Auditor", "field")


def test_resolve_receiver_stops_at_an_undeclared_field() -> None:
    state = make_state()
    state.scope_types["m"] = "MemoryStore"
    assert resolve_receiver(state, "m.mystery", self_type=None) == (None, "unresolved")


def test_resolve_receiver_refuses_an_unknown_head() -> None:
    state = make_state()
    assert resolve_receiver(state, "fmt", self_type=None) == (None, "unresolved")


def test_resolve_receiver_handles_an_empty_expression() -> None:
    assert resolve_receiver(make_state(), "", self_type="Foo") == (None, "unresolved")


@pytest.mark.parametrize(
    ("declared", "element", "receiver"),
    [
        ("*User", "User", "User"),
        ("[]*User", "User", None),
        ("map[string]*User", "User", None),
        ("map[string]string", None, None),
        ("chan Job", "Job", None),
        ("string", None, None),
        ("func(int) error", None, None),
        ("pkg.Thing", "pkg.Thing", "Thing"),
    ],
)
def test_go_element_and_receiver_types_differ_deliberately(
    declared: str, element: str | None, receiver: str | None
) -> None:
    # A `[]User` field composes Users; a `[]User` receiver is a slice. Conflating
    # the two invents methods on the element type.
    assert element_type(declared) == element
    assert receiver_type(declared) == receiver


# --------------------------------------------------------------------------
# Whole-repository behaviour with the active backend
# --------------------------------------------------------------------------


def analyze(path: Path):
    return analyze_repository(
        resolve_repo_root(path),
        ExecutionPolicy.repository_analysis(),
        StubCommandRunner(),  # type: ignore[arg-type]
        repository=path.name,
    )


def test_go_structural_satisfaction_survives_the_backend_switch(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    implementations = {
        (item.source, item.target)
        for item in analysis.relationships
        if item.relation is RelationType.IMPLEMENTS
    }
    assert ("MemoryStore", "Store") in implementations
    # The partial implementer must stay excluded under either backend.
    assert ("ReadOnlyStore", "Store") not in implementations


def test_repository_analysis_reports_the_active_detection_method(
    java_repo: Path,
) -> None:
    analysis = analyze(java_repo)
    expected = get_analyzer("java").detection_method
    types = [item for item in analysis.symbols if item.symbol_type is SymbolType.INTERFACE]
    assert types
    assert all(item.detection_method is expected for item in types)


def test_unparsable_source_is_reported_not_raised(tmp_path: Path) -> None:
    _requires_grammar("java")
    root = tmp_path / "broken"
    root.mkdir()
    (root / "Broken.java").write_text("public class { !!! ", encoding="utf-8")
    analysis = analyze(root)
    # tree-sitter recovers around the damage; we keep what parsed and say so.
    assert analysis.errors
    assert "syntax errors" in analysis.errors[0]
