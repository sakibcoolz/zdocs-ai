"""Per-language analyzer tests, run against the fixture repositories.

Each language gets the same battery: does it find the declared types, the
declared members, and the relationships it is supposed to find — and does it
*avoid* claiming relationships that are not there (the Go fixture contains a
type that implements only half an interface precisely to test that).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import StubCommandRunner
from operations.languages import (
    analyzer_for_path,
    available_languages,
    get_analyzer,
)
from operations.errors import UnsupportedLanguage
from operations.oop_analyzer import RepositoryAnalysis, analyze_repository
from operations.policy import ExecutionPolicy, resolve_repo_root
from operations.schemas import (
    Confidence,
    DetectionMethod,
    RelationType,
    SymbolType,
    Visibility,
)


def analyze(path: Path) -> RepositoryAnalysis:
    """Analyze a fixture repository with no external tools available."""
    return analyze_repository(
        resolve_repo_root(path),
        ExecutionPolicy.repository_analysis(),
        StubCommandRunner(),  # type: ignore[arg-type]
        repository=path.name,
    )


def names(analysis: RepositoryAnalysis, kind: SymbolType) -> set[str]:
    return {symbol.name for symbol in analysis.symbols if symbol.symbol_type is kind}


def relations(
    analysis: RepositoryAnalysis, relation: RelationType, *, kind: str | None = None
) -> set[tuple[str, str]]:
    return {
        (item.source, item.target)
        for item in analysis.relationships
        if item.relation is relation
        and (kind is None or item.metadata.get("kind") == kind)
    }


# --------------------------------------------------------------------------
# Registry
# --------------------------------------------------------------------------


def test_registry_lists_the_supported_languages() -> None:
    assert available_languages() == ["go", "java", "javascript", "python", "typescript"]


@pytest.mark.parametrize(
    ("path", "language"),
    [
        ("a.py", "python"),
        ("a.go", "go"),
        ("A.java", "java"),
        ("a.js", "javascript"),
        ("a.mjs", "javascript"),
        ("a.ts", "typescript"),
        ("a.tsx", "typescript"),
    ],
)
def test_analyzer_is_selected_by_extension(path: str, language: str) -> None:
    analyzer = analyzer_for_path(path)
    assert analyzer is not None
    assert analyzer.language == language


def test_unsupported_extension_has_no_analyzer() -> None:
    assert analyzer_for_path("main.rb") is None


def test_unknown_language_raises_a_clear_error() -> None:
    with pytest.raises(UnsupportedLanguage) as excinfo:
        get_analyzer("cobol")
    assert "cobol" in str(excinfo.value)


def test_analyzers_are_deterministic(python_repo: Path) -> None:
    first, second = analyze(python_repo), analyze(python_repo)
    assert first.model_dump() == second.model_dump()


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------


def test_python_finds_classes_and_abstractions(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    assert names(analysis, SymbolType.CLASS) >= {"Circle", "Square", "BoundingBox", "ShapeRegistry"}
    assert names(analysis, SymbolType.ABSTRACT_CLASS) == {"Shape"}
    assert names(analysis, SymbolType.INTERFACE) == {"Renderer"}


def test_python_finds_functions_methods_and_constructors(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    assert names(analysis, SymbolType.FUNCTION) >= {"total_area", "build_default"}
    assert names(analysis, SymbolType.CONSTRUCTOR) == {"__init__"}
    assert {"area", "describe", "add", "biggest"} <= names(analysis, SymbolType.METHOD)


def test_python_inheritance_is_high_confidence(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    assert ("Circle", "Shape") in relations(analysis, RelationType.INHERITS)
    assert ("Square", "Shape") in relations(analysis, RelationType.INHERITS)
    inheritance = [
        item
        for item in analysis.relationships
        if item.relation is RelationType.INHERITS
        and item.metadata.get("kind") == "base_class"
    ]
    assert all(item.confidence is Confidence.HIGH for item in inheritance)
    assert all(item.detection_method is DetectionMethod.PYTHON_AST for item in inheritance)


def test_python_abstract_base_becomes_a_derived_implements(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    derived = [
        item
        for item in analysis.relationships
        if item.relation is RelationType.IMPLEMENTS and item.source == "Circle"
    ]
    assert derived, "expected Circle to be derived as implementing Shape"
    assert derived[0].target == "Shape"
    # Inferred, not declared — Python has no `implements` keyword.
    assert derived[0].detection_method is DetectionMethod.DERIVED
    assert derived[0].confidence is Confidence.MEDIUM


def test_python_marker_bases_are_not_reported_as_parents(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    targets = {target for _, target in relations(analysis, RelationType.INHERITS)}
    assert "ABC" not in targets
    assert "Protocol" not in targets


def test_python_detects_composition_and_injection(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    assert ("Circle", "BoundingBox") in relations(
        analysis, RelationType.CONTAINS, kind="composition"
    )
    assert ("Shape", "Renderer") in relations(
        analysis, RelationType.USES, kind="dependency_injection"
    )


def test_python_visibility_follows_convention(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    by_name = {symbol.name: symbol for symbol in analysis.symbols if symbol.owner == "Shape"}
    assert by_name["name"].visibility is Visibility.PUBLIC
    assert by_name["_renderer"].visibility is Visibility.PROTECTED
    assert by_name["__secret"].visibility is Visibility.PRIVATE


def test_python_records_imports(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    # Modules are named relative to the analyzed root, which here is the
    # fixture repository itself.
    assert analysis.imports_by_module["shapes"] == ["__future__", "abc", "typing"]
    assert analysis.imports_by_module["registry"] == ["__future__", "shapes"]


def test_python_syntax_error_is_reported_not_raised(tmp_path: Path) -> None:
    root = tmp_path / "broken"
    root.mkdir()
    (root / "bad.py").write_text("def (:\n", encoding="utf-8")
    analysis = analyze(root)
    assert analysis.errors
    assert "syntax error" in analysis.errors[0].lower()


def test_python_method_override_is_derived(python_repo: Path) -> None:
    analysis = analyze(python_repo)
    overrides = {
        (item.source, item.target)
        for item in analysis.relationships
        if item.metadata.get("kind") == "method_override"
    }
    assert ("Circle.area", "Shape.area") in overrides


# --------------------------------------------------------------------------
# Go
# --------------------------------------------------------------------------


def test_go_finds_interfaces_structs_and_methods(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    assert names(analysis, SymbolType.INTERFACE) == {"Store", "Auditor"}
    assert names(analysis, SymbolType.STRUCT) == {"Base", "MemoryStore", "ReadOnlyStore"}
    assert {"Get", "Save", "Record"} <= names(analysis, SymbolType.METHOD)
    assert names(analysis, SymbolType.CONSTRUCTOR) == {"NewMemoryStore"}


def test_go_struct_embedding_is_reported_as_inheritance(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    embedding = [
        item
        for item in analysis.relationships
        if item.metadata.get("kind") == "struct_embedding"
    ]
    assert ("MemoryStore", "Base") in {(item.source, item.target) for item in embedding}
    # Embedding is promotion, not classical inheritance — the note must say so.
    assert "not classical inheritance" in embedding[0].metadata["note"]


def test_go_interface_implementation_is_structural(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    implementations = [
        item
        for item in analysis.relationships
        if item.relation is RelationType.IMPLEMENTS
    ]
    assert ("MemoryStore", "Store") in {
        (item.source, item.target) for item in implementations
    }
    match = next(item for item in implementations if item.source == "MemoryStore")
    assert match.detection_method is DetectionMethod.DERIVED
    assert match.confidence is Confidence.HIGH
    assert match.metadata["signature_match"] == "exact"
    assert sorted(match.metadata["methods"]) == ["Get", "Save"]


def test_go_partial_implementer_is_not_claimed(go_repo: Path) -> None:
    # ReadOnlyStore has Get but not Save. Reporting it would be a false positive.
    analysis = analyze(go_repo)
    assert ("ReadOnlyStore", "Store") not in relations(analysis, RelationType.IMPLEMENTS)


def test_go_visibility_follows_capitalisation(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    by_name = {
        symbol.name: symbol
        for symbol in analysis.symbols
        if symbol.owner == "MemoryStore"
    }
    assert by_name["Get"].visibility is Visibility.PUBLIC
    assert by_name["values"].visibility is Visibility.PACKAGE


def test_go_detects_composition_and_injection(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    assert ("MemoryStore", "Auditor") in relations(
        analysis, RelationType.CONTAINS, kind="composition"
    )
    assert ("MemoryStore", "Auditor") in relations(
        analysis, RelationType.USES, kind="dependency_injection"
    )


def test_go_primitive_fields_are_not_composition(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    targets = {target for _, target in relations(analysis, RelationType.CONTAINS)}
    assert "string" not in targets
    assert "error" not in targets


def test_go_imports_are_recorded(go_repo: Path) -> None:
    analysis = analyze(go_repo)
    assert analysis.imports_by_module["storage"] == ["errors", "fmt"]


def test_go_ignores_declarations_inside_comments(tmp_path: Path) -> None:
    root = tmp_path / "go"
    root.mkdir()
    (root / "a.go").write_text(
        'package a\n\n// type Ghost struct {\n/* type Phantom interface { } */\n'
        'const s = "type Spectre struct {"\n',
        encoding="utf-8",
    )
    analysis = analyze(root)
    assert not {"Ghost", "Phantom", "Spectre"} & {s.name for s in analysis.symbols}


# --------------------------------------------------------------------------
# Java
# --------------------------------------------------------------------------


def test_java_finds_types(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    assert names(analysis, SymbolType.INTERFACE) == {"Measurable", "Drawable"}
    assert names(analysis, SymbolType.ABSTRACT_CLASS) == {"AbstractShape"}
    assert names(analysis, SymbolType.CLASS) == {"Rectangle", "Logger"}


def test_java_inheritance_and_implementation_are_declared(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    assert ("Rectangle", "AbstractShape") in relations(analysis, RelationType.INHERITS)
    assert ("Drawable", "Measurable") in relations(analysis, RelationType.INHERITS)
    assert ("AbstractShape", "Drawable") in relations(analysis, RelationType.IMPLEMENTS)
    assert ("Rectangle", "Drawable") in relations(analysis, RelationType.IMPLEMENTS)
    declared = [
        item
        for item in analysis.relationships
        if item.relation is RelationType.IMPLEMENTS and item.metadata.get("kind") == "implements"
    ]
    assert all(item.confidence is Confidence.HIGH for item in declared)


def test_java_finds_constructors_and_methods(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    assert names(analysis, SymbolType.CONSTRUCTOR) == {"AbstractShape", "Rectangle"}
    assert {"area", "draw", "log"} <= names(analysis, SymbolType.METHOD)


def test_java_records_override_annotations(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    draw = next(
        symbol
        for symbol in analysis.symbols
        if symbol.owner == "AbstractShape" and symbol.name == "draw"
    )
    assert draw.metadata["is_override"] is True
    assert "Override" in draw.metadata["annotations"]


def test_java_visibility_modifiers(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    by_name = {
        symbol.name: symbol
        for symbol in analysis.symbols
        if symbol.owner == "AbstractShape"
    }
    assert by_name["name"].visibility is Visibility.PROTECTED
    assert by_name["logger"].visibility is Visibility.PRIVATE
    assert by_name["draw"].visibility is Visibility.PUBLIC


def test_java_constructor_injection(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    assert ("AbstractShape", "Logger") in relations(
        analysis, RelationType.USES, kind="dependency_injection"
    )


def test_java_package_and_imports(java_repo: Path) -> None:
    analysis = analyze(java_repo)
    assert all(symbol.package == "com.example" for symbol in analysis.types())
    assert "java.util.List" in analysis.imports_by_module["com.example"]


# --------------------------------------------------------------------------
# TypeScript
# --------------------------------------------------------------------------


def test_typescript_finds_types(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    assert names(analysis, SymbolType.INTERFACE) == {"Measurable", "Drawable"}
    assert names(analysis, SymbolType.ABSTRACT_CLASS) == {"AbstractShape"}
    assert {"Rectangle", "BoundingBox", "Logger"} <= names(analysis, SymbolType.CLASS)
    assert names(analysis, SymbolType.TYPE_ALIAS) == {"Maybe"}


def test_typescript_class_inheritance(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    assert ("Rectangle", "AbstractShape") in relations(analysis, RelationType.INHERITS)
    extends = next(
        item
        for item in analysis.relationships
        if item.relation is RelationType.INHERITS and item.source == "Rectangle"
    )
    assert extends.confidence is Confidence.HIGH


def test_typescript_interface_implementation_and_extension(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    assert ("AbstractShape", "Drawable") in relations(analysis, RelationType.IMPLEMENTS)
    assert ("Rectangle", "Drawable") in relations(analysis, RelationType.IMPLEMENTS)
    assert ("Drawable", "Measurable") in relations(analysis, RelationType.INHERITS)


def test_typescript_visibility(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    by_name = {
        symbol.name: symbol
        for symbol in analysis.symbols
        if symbol.owner == "AbstractShape"
    }
    assert by_name["label"].visibility is Visibility.PROTECTED
    assert by_name["#hidden"].visibility is Visibility.PRIVATE


def test_typescript_parameter_property_injection(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    injections = [
        item
        for item in analysis.relationships
        if item.metadata.get("kind") == "dependency_injection" and item.source == "AbstractShape"
    ]
    assert injections
    assert injections[0].target == "Logger"
    assert injections[0].metadata["via"] == "parameter_property"


def test_typescript_functions_and_imports(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    assert "totalArea" in names(analysis, SymbolType.FUNCTION)
    assert "./logger" in analysis.imports_by_module["src/shapes"]


def test_typescript_builtin_types_are_not_composition(ts_repo: Path) -> None:
    analysis = analyze(ts_repo)
    targets = {target for _, target in relations(analysis, RelationType.CONTAINS)}
    assert "number" not in targets
    assert "string" not in targets


# --------------------------------------------------------------------------
# JavaScript
# --------------------------------------------------------------------------


def test_javascript_class_inheritance_and_require(js_repo: Path) -> None:
    analysis = analyze(js_repo)
    assert names(analysis, SymbolType.CLASS) == {"Animal", "Dog", "Logger"}
    assert ("Dog", "Animal") in relations(analysis, RelationType.INHERITS)
    assert "./logger" in analysis.imports_by_module["src/animals"]


def test_javascript_finds_methods_and_functions(js_repo: Path) -> None:
    analysis = analyze(js_repo)
    assert {"speak", "log"} <= names(analysis, SymbolType.METHOD)
    assert "makeDog" in names(analysis, SymbolType.FUNCTION)
    assert names(analysis, SymbolType.CONSTRUCTOR) == {"constructor"}


def test_javascript_override_is_derived(js_repo: Path) -> None:
    analysis = analyze(js_repo)
    overrides = {
        (item.source, item.target)
        for item in analysis.relationships
        if item.metadata.get("kind") == "method_override"
    }
    assert ("Dog.speak", "Animal.speak") in overrides


# --------------------------------------------------------------------------
# Cross-language behaviour
# --------------------------------------------------------------------------


def test_unsupported_language_files_are_skipped_not_guessed(tmp_path: Path) -> None:
    root = tmp_path / "mixed"
    root.mkdir()
    (root / "a.py").write_text("class A:\n    pass\n", encoding="utf-8")
    (root / "b.rb").write_text("class B\nend\n", encoding="utf-8")
    analysis = analyze(root)
    assert {symbol.name for symbol in analysis.types()} == {"A"}
    assert analysis.languages == ["python"]


def test_oversized_files_are_skipped_with_a_warning(tmp_path: Path) -> None:
    root = tmp_path / "big"
    root.mkdir()
    (root / "huge.py").write_text("x = 1\n" * 5000, encoding="utf-8")
    analysis = analyze_repository(
        resolve_repo_root(root),
        ExecutionPolicy.repository_analysis().with_overrides(max_file_bytes=100),
        None,
        repository="big",
    )
    assert analysis.files_skipped == 1
    assert any("larger than" in warning for warning in analysis.warnings)


def test_summary_counts_are_consistent(all_fixtures_repo: Path) -> None:
    analysis = analyze(all_fixtures_repo)
    summary = analysis.summary()
    assert summary["symbol_count"] == len(analysis.symbols)
    assert summary["relationship_count"] == len(analysis.relationships)
    assert sum(summary["symbols_by_kind"].values()) == len(analysis.symbols)
    assert set(analysis.languages) == {"go", "java", "javascript", "python", "typescript"}
