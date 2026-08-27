"""Go analyzer backed by a tree-sitter grammar.

Same output as :mod:`operations.languages.go_analyzer` — the lexical fallback —
but read from a real syntax tree, so declarations are confirmed rather than
pattern-matched and are reported at
:attr:`~operations.schemas.Confidence.HIGH`.

Two things the tree gives us that the lexical parser cannot:

* **Exact method signatures**, taken from the grammar's ``parameters``/
  ``result`` fields rather than reconstructed from text — which makes the
  structural interface-satisfaction check in :mod:`operations.oop_analyzer`
  more reliable.
* **Receiver-typed calls.** ``m.auditor.Record()`` inside a method on
  ``*MemoryStore`` resolves through the receiver and the field's declared type
  to ``Auditor.Record``, instead of a bare name ``Record`` that collides with
  every other ``Record`` in the repository.
"""

from __future__ import annotations

from operations.languages.base import go_visibility, strip_generics
from operations.languages.go_common import (
    element_type,
    normalize_signature,
    receiver_type,
)
from operations.languages.tree_sitter_support import (
    TreeSitterAnalyzer,
    TsState,
    child,
    children_of_type,
    descendants_of_type,
    end_line_of,
    line_of,
    resolve_receiver,
)
from operations.schemas import Confidence, RelationType, SymbolType

_MAX_CALLS_PER_FILE = 400


class GoTreeSitterAnalyzer(TreeSitterAnalyzer):
    """Extract Go declarations and relationships from a tree-sitter tree."""

    language = "go"
    extensions = (".go",)

    def extract(self, state: TsState) -> None:
        package = child(state.root, "package") or None
        for node in children_of_type(state.root, "package_clause"):
            identifier = node.named_children[0] if node.named_children else None
            if identifier is not None:
                state.package = state.text(identifier)
                break

        self._imports(state)
        self._types(state)          # fills field_types, needed by _functions
        self._functions(state)
        # Symbols are emitted in source order: the walk visits declarations
        # and members in separate passes, which would otherwise interleave them
        # confusingly in catalogs and diffs.
        state.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))

    # -- imports -----------------------------------------------------------

    def _imports(self, state: TsState) -> None:
        for spec in descendants_of_type(state.root, "import_spec"):
            path = child(spec, "path") or spec.named_children[-1]
            state.add_import(state.text(path).strip('"`'), line_of(spec))

    # -- type declarations -------------------------------------------------

    def _types(self, state: TsState) -> None:
        for spec in descendants_of_type(state.root, "type_spec"):
            name_node = child(spec, "name")
            type_node = child(spec, "type")
            if name_node is None or type_node is None:
                continue
            name = state.text(name_node)

            if type_node.type == "struct_type":
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.STRUCT,
                    line=line_of(spec),
                    end_line=end_line_of(spec),
                    visibility=go_visibility(name),
                    signature=f"type {name} struct",
                )
                self._struct_fields(state, name, type_node)
            elif type_node.type == "interface_type":
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.INTERFACE,
                    line=line_of(spec),
                    end_line=end_line_of(spec),
                    visibility=go_visibility(name),
                    is_abstract=True,
                    signature=f"type {name} interface",
                )
                self._interface_members(state, name, type_node)
            else:
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.TYPE_ALIAS,
                    line=line_of(spec),
                    end_line=end_line_of(spec),
                    visibility=go_visibility(name),
                    signature=f"type {name} {state.text(type_node)}"[:120],
                )

    def _struct_fields(self, state: TsState, owner: str, struct_node) -> None:
        body = children_of_type(struct_node, "field_declaration_list")
        if not body:
            return
        for declaration in children_of_type(body[0], "field_declaration"):
            type_node = child(declaration, "type")
            names = [
                state.text(item)
                for item in children_of_type(declaration, "field_identifier")
            ]
            if type_node is None:
                continue
            field_type = normalize_type(state.text(type_node))

            if not names:
                # A type with no field name: Go struct embedding, the closest
                # thing the language has to inheritance.
                embedded = strip_generics(field_type.lstrip("*"))
                simple = embedded.rsplit(".", 1)[-1]
                state.add_symbol(
                    name=simple,
                    symbol_type=SymbolType.FIELD,
                    line=line_of(declaration),
                    owner=owner,
                    visibility=go_visibility(simple),
                    signature=field_type,
                )
                state.field_types[f"{owner}.{simple}"] = simple
                state.add_relationship(
                    source=owner,
                    target=embedded,
                    relation=RelationType.INHERITS,
                    line=line_of(declaration),
                    confidence=Confidence.HIGH,
                    metadata={
                        "kind": "struct_embedding",
                        "note": "Go embedding promotes the embedded type's "
                        "methods; it is not classical inheritance",
                    },
                )
                continue

            bare = element_type(field_type)
            for field_name in names:
                state.add_symbol(
                    name=field_name,
                    symbol_type=SymbolType.FIELD,
                    line=line_of(declaration),
                    owner=owner,
                    visibility=go_visibility(field_name),
                    signature=f"{field_name} {field_type}",
                )
                if bare:
                    declared_receiver = receiver_type(field_type)
                    if declared_receiver:
                        state.field_types[f"{owner}.{field_name}"] = declared_receiver
                    state.add_relationship(
                        source=owner,
                        target=bare,
                        relation=RelationType.CONTAINS,
                        line=line_of(declaration),
                        confidence=Confidence.HIGH,
                        metadata={"kind": "composition", "field": field_name},
                    )

    def _interface_members(self, state: TsState, owner: str, interface_node) -> None:
        for member in interface_node.named_children:
            if member.type in ("method_elem", "method_spec"):
                name_node = child(member, "name")
                if name_node is None:
                    continue
                name = state.text(name_node)
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.METHOD,
                    line=line_of(member),
                    end_line=end_line_of(member),
                    owner=owner,
                    visibility=go_visibility(name),
                    is_abstract=True,
                    signature=signature_of(state, name, member),
                )
            elif member.type in ("type_identifier", "qualified_type"):
                state.add_relationship(
                    source=owner,
                    target=strip_generics(state.text(member)),
                    relation=RelationType.INHERITS,
                    line=line_of(member),
                    metadata={"kind": "interface_embedding"},
                )

    # -- functions and methods --------------------------------------------

    def _functions(self, state: TsState) -> None:
        for node in descendants_of_type(
            state.root, "function_declaration", "method_declaration"
        ):
            name_node = child(node, "name")
            if name_node is None:
                continue
            name = state.text(name_node)
            owner, receiver_name = self._receiver(state, node)
            results = result_types(state, node)
            signature = signature_of(state, name, node)

            if owner:
                symbol_type = SymbolType.METHOD
            elif name.startswith("New") and len(name) > 3:
                # `NewFoo(...) *Foo` is the near-universal Go constructor idiom.
                symbol_type = SymbolType.CONSTRUCTOR
            else:
                symbol_type = SymbolType.FUNCTION

            state.add_symbol(
                name=name,
                symbol_type=symbol_type,
                line=line_of(node),
                end_line=end_line_of(node),
                owner=owner,
                visibility=go_visibility(name),
                signature=signature,
            )

            if symbol_type is SymbolType.CONSTRUCTOR:
                self._constructor_relationships(state, node, name, results)

            body = child(node, "body")
            if body is not None:
                self._calls(state, node, body, owner, receiver_name, name)

    def _receiver(self, state: TsState, node) -> tuple[str | None, str | None]:
        """``(owner type, receiver variable name)`` for a method declaration."""
        receiver = child(node, "receiver")
        if receiver is None:
            return None, None
        declarations = children_of_type(receiver, "parameter_declaration")
        if not declarations:
            return None, None
        declaration = declarations[0]
        type_node = child(declaration, "type")
        name_node = child(declaration, "name")
        if type_node is None:
            return None, None
        owner = strip_generics(normalize_type(state.text(type_node)).lstrip("*"))
        return owner, state.text(name_node) if name_node is not None else None

    def _constructor_relationships(
        self, state: TsState, node, name: str, results: list[str]
    ) -> None:
        constructed = strip_generics(results[0].lstrip("*")) if results else ""
        if constructed:
            state.add_relationship(
                source=name,
                target=constructed,
                relation=RelationType.CONTAINS,
                line=line_of(node),
                confidence=Confidence.HIGH,
                metadata={"kind": "constructor_function"},
            )
        for declaration in children_of_type(
            child(node, "parameters"), "parameter_declaration"
        ):
            type_node = child(declaration, "type")
            name_node = child(declaration, "name")
            if type_node is None or name_node is None:
                continue
            dependency = element_type(state.text(type_node))
            if dependency is None:
                continue
            state.add_relationship(
                source=constructed or name,
                target=dependency,
                relation=RelationType.USES,
                line=line_of(node),
                confidence=Confidence.MEDIUM,
                metadata={
                    "kind": "dependency_injection",
                    "parameter": state.text(name_node),
                },
            )

    # -- calls -------------------------------------------------------------

    def _calls(
        self,
        state: TsState,
        function_node,
        body,
        owner: str | None,
        receiver_name: str | None,
        function_name: str,
    ) -> None:
        """Record CALLS edges, resolving the receiver to a type where declared."""
        scope: dict[str, str] = {}
        if receiver_name and owner:
            scope[receiver_name] = owner
        scope.update(self._parameter_types(state, function_node))
        scope.update(self._local_types(state, body))
        previous, state.scope_types = state.scope_types, scope

        source = f"{owner}.{function_name}" if owner else function_name
        try:
            for call in descendants_of_type(body, "call_expression"):
                if state.call_count >= _MAX_CALLS_PER_FILE:
                    return
                function = child(call, "function")
                if function is None:
                    continue
                if function.type == "selector_expression":
                    operand = child(function, "operand")
                    field_node = child(function, "field")
                    if field_node is None:
                        continue
                    callee = state.text(field_node)
                    receiver = state.text(operand) if operand is not None else ""
                    resolved, how = resolve_receiver(
                        state, receiver, self_type=owner, self_names=()
                    )
                elif function.type == "identifier":
                    # A bare `foo(...)`: a package-level function, with no
                    # receiver to resolve.
                    callee, receiver, resolved, how = (
                        state.text(function),
                        "",
                        None,
                        "unqualified",
                    )
                else:
                    continue

                state.call_count += 1
                state.add_relationship(
                    source=source,
                    target=f"{resolved}.{callee}" if resolved else callee,
                    relation=RelationType.CALLS,
                    line=line_of(call),
                    confidence=Confidence.HIGH if resolved else Confidence.MEDIUM,
                    metadata={
                        "kind": "call",
                        "callee": callee,
                        "receiver": receiver,
                        "receiver_resolution": how,
                    },
                )
        finally:
            state.scope_types = previous

    def _parameter_types(self, state: TsState, function_node) -> dict[str, str]:
        types: dict[str, str] = {}
        for declaration in children_of_type(
            child(function_node, "parameters"), "parameter_declaration"
        ):
            type_node = child(declaration, "type")
            if type_node is None:
                continue
            declared = receiver_type(state.text(type_node))
            if declared is None:
                continue
            for name_node in children_of_type(declaration, "identifier"):
                types[state.text(name_node)] = declared
        return types

    def _local_types(self, state: TsState, body) -> dict[str, str]:
        """Types of locals declared as ``x := NewFoo()`` or ``var x Foo``."""
        types: dict[str, str] = {}
        for declaration in descendants_of_type(body, "var_spec"):
            type_node = child(declaration, "type")
            if type_node is None:
                continue
            declared = receiver_type(state.text(type_node))
            if declared:
                for name_node in children_of_type(declaration, "identifier"):
                    types[state.text(name_node)] = declared
        return types


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def normalize_type(raw: str) -> str:
    """Collapse whitespace in a type expression so signatures compare equal."""
    return " ".join(raw.split())


def parameter_types(state: TsState, list_node) -> list[str]:
    """Declared parameter types, expanding grouped names (``a, b int``)."""
    if list_node is None:
        return []
    types: list[str] = []
    for declaration in children_of_type(list_node, "parameter_declaration", "variadic_parameter_declaration"):
        type_node = child(declaration, "type")
        if type_node is None:
            continue
        declared = normalize_type(state.text(type_node))
        names = children_of_type(declaration, "identifier")
        types.extend([declared] * max(1, len(names)))
    return types


def result_types(state: TsState, node) -> list[str]:
    """Declared result types of a function/method declaration."""
    result = child(node, "result")
    if result is None:
        return []
    if result.type == "parameter_list":
        return parameter_types(state, result)
    return [normalize_type(state.text(result))]


def signature_of(state: TsState, name: str, node) -> str:
    """``name(paramtypes) (resulttypes)`` — the form used for method matching.

    Built with the shared :func:`~operations.languages.go_common.normalize_signature`
    so a method set parsed here compares equal to one parsed lexically.
    """
    return normalize_signature(
        name,
        parameter_types(state, child(node, "parameters")),
        result_types(state, node),
    )


__all__ = ["GoTreeSitterAnalyzer", "signature_of"]
