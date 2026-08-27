"""Python analyzer built on the standard-library :mod:`ast` module.

Because CPython's own parser produces the tree, everything reported here is a
*confirmed* syntactic fact about the file: declarations, base classes,
decorators and call sites are read from the AST, never guessed from text. That
is why this analyzer reports :attr:`~operations.schemas.Confidence.HIGH` for
declarations and explicit inheritance.

What remains inference — and is therefore reported as ``MEDIUM`` — is anything
that depends on *meaning* rather than syntax: whether an attribute assignment
models composition, and whether a typed constructor parameter is dependency
injection. Cross-file name resolution is not attempted here at all; it happens
in :mod:`operations.relationship_graph`, which can see every file at once.
"""

from __future__ import annotations

import ast

from operations.languages.base import LanguageAnalyzer, python_visibility
from operations.schemas import (
    Confidence,
    DetectionMethod,
    FileAnalysis,
    RelationshipInfo,
    RelationType,
    SymbolInfo,
    SymbolType,
)

#: Bases that mark a class as an interface-like contract rather than a parent.
_PROTOCOL_BASES = {"Protocol", "typing.Protocol", "typing_extensions.Protocol"}
_ABSTRACT_BASES = {"ABC", "abc.ABC", "ABCMeta", "abc.ABCMeta"}
_ABSTRACT_DECORATORS = {
    "abstractmethod", "abc.abstractmethod",
    "abstractproperty", "abc.abstractproperty",
}
_STATIC_DECORATORS = {"staticmethod", "classmethod"}

#: Cap on self-attribute READS/WRITES per file. These are cheap to produce and
#: numerous; the cap keeps a single large module from dominating a graph.
_MAX_FIELD_ACCESS_PER_FILE = 200


def expression_name(node: ast.expr | None) -> str | None:
    """Best-effort dotted name for an expression used in a type position."""
    if node is None:
        return None
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = expression_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Subscript):
        return expression_name(node.value)
    if isinstance(node, ast.Call):
        return expression_name(node.func)
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        # Forward reference: `x: "Foo"`.
        return node.value.strip() or None
    return None


class PythonAnalyzer(LanguageAnalyzer):
    """Extract symbols and relationships from Python source."""

    language = "python"
    extensions = (".py", ".pyi")
    detection_method = DetectionMethod.PYTHON_AST
    base_confidence = Confidence.HIGH

    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        try:
            tree = ast.parse(source, filename=file_path)
        except SyntaxError as exc:
            return self.empty(file_path, f"Python syntax error: {exc}")
        visitor = _PythonVisitor(file_path=file_path, module=module_name(file_path))
        visitor.visit(tree)
        return visitor.result()


def module_name(file_path: str) -> str:
    """Dotted module path for a repository-relative ``.py`` file."""
    stem = file_path.removesuffix(".pyi").removesuffix(".py")
    parts = [part for part in stem.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _PythonVisitor(ast.NodeVisitor):
    """Walks a module, recording declarations and typed relationships."""

    def __init__(self, *, file_path: str, module: str) -> None:
        self.file_path = file_path
        self.module = module
        self.symbols: list[SymbolInfo] = []
        self.relationships: list[RelationshipInfo] = []
        self.imports: list[str] = []
        self._class_stack: list[str] = []
        self._function_stack: list[str] = []
        self._field_accesses = 0

    # -- result ------------------------------------------------------------

    def result(self) -> FileAnalysis:
        self.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))
        return FileAnalysis(
            file_path=self.file_path,
            language="python",
            package=self.module,
            symbols=self.symbols,
            relationships=self.relationships,
            imports=sorted(set(self.imports)),
        )

    # -- helpers -----------------------------------------------------------

    @property
    def _owner(self) -> str | None:
        return self._class_stack[-1] if self._class_stack else None

    @property
    def _scope(self) -> str:
        """Innermost named scope, used as the source of CALLS/READS/WRITES."""
        if self._function_stack:
            return self._function_stack[-1]
        if self._class_stack:
            return self._class_stack[-1]
        return self.module or self.file_path

    def _add_symbol(self, **kwargs: object) -> None:
        self.symbols.append(
            SymbolInfo(
                file_path=self.file_path,
                language="python",
                package=self.module,
                detection_method=DetectionMethod.PYTHON_AST,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    def _add_relationship(
        self,
        *,
        source: str,
        target: str,
        relation: RelationType,
        line: int,
        end_line: int | None = None,
        confidence: Confidence = Confidence.HIGH,
        metadata: dict[str, object] | None = None,
    ) -> None:
        if not target:
            return
        self.relationships.append(
            RelationshipInfo(
                source=source,
                target=target,
                relation=relation,
                file_path=self.file_path,
                line=line,
                end_line=end_line,
                language="python",
                detection_method=DetectionMethod.PYTHON_AST,
                confidence=confidence,
                metadata=metadata or {},
            )
        )

    # -- imports -----------------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:  # noqa: N802
        for alias in node.names:
            self.imports.append(alias.name)
            self._add_relationship(
                source=self.module or self.file_path,
                target=alias.name,
                relation=RelationType.IMPORTS,
                line=node.lineno,
                metadata={"kind": "import"},
            )
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:  # noqa: N802
        module = node.module or ""
        if node.level:  # relative import: keep the dots so it is not mistaken
            module = "." * node.level + module  # for an absolute module path
        if module:
            self.imports.append(module)
            self._add_relationship(
                source=self.module or self.file_path,
                target=module,
                relation=RelationType.IMPORTS,
                line=node.lineno,
                metadata={
                    "kind": "from_import",
                    "names": [alias.name for alias in node.names],
                },
            )
        self.generic_visit(node)

    # -- classes -----------------------------------------------------------

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        bases = [name for name in (expression_name(b) for b in node.bases) if name]
        keywords = {
            kw.arg: expression_name(kw.value) for kw in node.keywords if kw.arg
        }
        decorators = {expression_name(d) or "" for d in node.decorator_list}

        is_protocol = any(base in _PROTOCOL_BASES for base in bases)
        has_abstract_member = any(
            isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                (expression_name(d) or "") in _ABSTRACT_DECORATORS
                for d in child.decorator_list
            )
            for child in node.body
        )
        is_abstract = (
            has_abstract_member
            or any(base in _ABSTRACT_BASES for base in bases)
            or keywords.get("metaclass") in _ABSTRACT_BASES
        )

        if is_protocol:
            symbol_type = SymbolType.INTERFACE
        elif is_abstract:
            symbol_type = SymbolType.ABSTRACT_CLASS
        else:
            symbol_type = SymbolType.CLASS

        self._add_symbol(
            name=node.name,
            symbol_type=symbol_type,
            line=node.lineno,
            end_line=getattr(node, "end_lineno", None),
            visibility=python_visibility(node.name),
            is_abstract=is_abstract or is_protocol,
            signature=f"class {node.name}({', '.join(bases)})",
            confidence=Confidence.HIGH,
        )

        for base in bases:
            # `Protocol`/`ABC` are markers, not real parents; recording them as
            # inheritance would clutter every diagram with a synthetic root.
            if base in _PROTOCOL_BASES or base in _ABSTRACT_BASES:
                continue
            relation = (
                RelationType.IMPLEMENTS if is_protocol else RelationType.INHERITS
            )
            self._add_relationship(
                source=node.name,
                target=base,
                relation=relation,
                line=node.lineno,
                confidence=Confidence.HIGH,
                metadata={"kind": "base_class", "decorators": sorted(decorators)},
            )

        self._class_stack.append(node.name)
        for child in node.body:
            self.visit(child)
        self._class_stack.pop()

    # -- class-level annotated fields -------------------------------------

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        owner = self._owner
        target_name = (
            node.target.id if isinstance(node.target, ast.Name) else None
        )
        if owner and target_name and not self._function_stack:
            annotation = expression_name(node.annotation)
            self._add_symbol(
                name=target_name,
                symbol_type=SymbolType.FIELD,
                line=node.lineno,
                end_line=getattr(node, "end_lineno", None),
                owner=owner,
                visibility=python_visibility(target_name),
                signature=f"{target_name}: {annotation or '?'}",
                confidence=Confidence.HIGH,
            )
            if annotation:
                self._add_relationship(
                    source=owner,
                    target=annotation,
                    relation=RelationType.CONTAINS,
                    line=node.lineno,
                    confidence=Confidence.MEDIUM,
                    metadata={"kind": "composition", "field": target_name},
                )
        self.generic_visit(node)

    # -- functions ---------------------------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._handle_function(node)

    def _handle_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self._owner
        decorators = {expression_name(d) or "" for d in node.decorator_list}
        is_abstract = bool(decorators & _ABSTRACT_DECORATORS)
        is_static = bool(decorators & _STATIC_DECORATORS)

        if owner and node.name == "__init__":
            symbol_type = SymbolType.CONSTRUCTOR
        elif owner:
            symbol_type = SymbolType.METHOD
        else:
            symbol_type = SymbolType.FUNCTION

        parameters = self._parameters(node)
        self._add_symbol(
            name=node.name,
            symbol_type=symbol_type,
            line=node.lineno,
            end_line=getattr(node, "end_lineno", None),
            owner=owner,
            visibility=python_visibility(node.name),
            is_abstract=is_abstract,
            is_static=is_static,
            signature=f"{node.name}({', '.join(name for name, _ in parameters)})",
            confidence=Confidence.HIGH,
        )

        # Typed constructor parameters are the standard Python DI shape.
        if owner and node.name == "__init__":
            for parameter_name, annotation in parameters:
                if parameter_name in ("self", "cls") or not annotation:
                    continue
                self._add_relationship(
                    source=owner,
                    target=annotation,
                    relation=RelationType.USES,
                    line=node.lineno,
                    confidence=Confidence.MEDIUM,
                    metadata={
                        "kind": "dependency_injection",
                        "parameter": parameter_name,
                    },
                )

        qualified = f"{owner}.{node.name}" if owner else node.name
        self._function_stack.append(qualified)
        for child in node.body:
            self.visit(child)
        self._function_stack.pop()

    def _parameters(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> list[tuple[str, str | None]]:
        args = node.args
        collected = [*args.posonlyargs, *args.args, *args.kwonlyargs]
        if args.vararg:
            collected.append(args.vararg)
        if args.kwarg:
            collected.append(args.kwarg)
        return [(arg.arg, expression_name(arg.annotation)) for arg in collected]

    # -- statements inside functions --------------------------------------

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        owner = self._owner
        in_constructor = bool(
            self._function_stack and self._function_stack[-1].endswith(".__init__")
        )
        if owner and in_constructor:
            for target in node.targets:
                if not (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                ):
                    continue
                self._add_symbol(
                    name=target.attr,
                    symbol_type=SymbolType.FIELD,
                    line=node.lineno,
                    owner=owner,
                    visibility=python_visibility(target.attr),
                    signature=f"self.{target.attr}",
                    confidence=Confidence.HIGH,
                )
                if isinstance(node.value, ast.Call):
                    # `self.x = Foo(...)` — the object is created and owned here.
                    constructed = expression_name(node.value.func)
                    if constructed:
                        self._add_relationship(
                            source=owner,
                            target=constructed,
                            relation=RelationType.CONTAINS,
                            line=node.lineno,
                            confidence=Confidence.MEDIUM,
                            metadata={"kind": "composition", "field": target.attr},
                        )
                elif isinstance(node.value, ast.Name):
                    # `self.x = dep` — held, not created: aggregation at most.
                    self._add_relationship(
                        source=owner,
                        target=node.value.id,
                        relation=RelationType.USES,
                        line=node.lineno,
                        confidence=Confidence.LOW,
                        metadata={
                            "kind": "aggregation",
                            "field": target.attr,
                            "note": "assigned from a parameter or local; "
                            "target is a variable name, not a resolved type",
                        },
                    )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        callee = expression_name(node.func)
        if callee and (self._function_stack or self._class_stack):
            self._add_relationship(
                source=self._scope,
                target=callee,
                relation=RelationType.CALLS,
                line=node.lineno,
                confidence=Confidence.HIGH,
                metadata={"kind": "call", "arguments": len(node.args)},
            )
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:  # noqa: N802
        # `self.field` reads/writes inside a method: cheap encapsulation signal.
        if (
            self._owner
            and self._function_stack
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and self._field_accesses < _MAX_FIELD_ACCESS_PER_FILE
        ):
            relation = (
                RelationType.WRITES
                if isinstance(node.ctx, (ast.Store, ast.Del))
                else RelationType.READS
            )
            self._field_accesses += 1
            self._add_relationship(
                source=self._scope,
                target=f"{self._owner}.{node.attr}",
                relation=relation,
                line=node.lineno,
                confidence=Confidence.HIGH,
                metadata={"kind": "field_access", "visibility": python_visibility(node.attr).value},
            )
        self.generic_visit(node)


__all__ = ["PythonAnalyzer", "module_name", "expression_name"]
