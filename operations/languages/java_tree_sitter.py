"""Java analyzer backed by a tree-sitter grammar.

Produces the same :class:`~operations.schemas.FileAnalysis` as the lexical
fallback in :mod:`operations.languages.java_analyzer`, read from a real syntax
tree. Java already states its OOP relationships outright, so the gain here is
not *new* relationships but reliability: nested and inner classes, generic
signatures, annotations and text blocks are handled by the grammar rather than
by a brace scanner.

The one genuinely new capability is receiver-typed calls: ``logger.log(name)``
inside ``AbstractShape`` resolves through the field's declared type to
``Logger.log`` instead of a bare ``log``.
"""

from __future__ import annotations

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
from operations.schemas import Confidence, RelationType, SymbolType, Visibility

#: Types that carry no design information as composition targets.
JAVA_BUILTINS: frozenset[str] = frozenset(
    {
        "byte", "short", "int", "long", "float", "double", "boolean", "char",
        "void", "var", "String", "Object", "Integer", "Long", "Double",
        "Boolean", "Character", "Byte", "Short", "Float", "Number",
    }
)

#: Annotations that mark a member as injected by a DI container.
INJECTION_ANNOTATIONS: frozenset[str] = frozenset(
    {"Autowired", "Inject", "Resource", "Value"}
)

_TYPE_NODES = (
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
)

_SYMBOL_TYPE_FOR_NODE = {
    "class_declaration": SymbolType.CLASS,
    "interface_declaration": SymbolType.INTERFACE,
    "annotation_type_declaration": SymbolType.INTERFACE,
    "enum_declaration": SymbolType.ENUM,
    "record_declaration": SymbolType.RECORD,
}

_MAX_CALLS_PER_FILE = 400


class JavaTreeSitterAnalyzer(TreeSitterAnalyzer):
    """Extract Java types, members and relationships from a tree-sitter tree."""

    language = "java"
    extensions = (".java",)

    def extract(self, state: TsState) -> None:
        for node in descendants_of_type(state.root, "package_declaration"):
            state.package = state.text(node.named_children[0]) if node.named_children else ""
            break
        for node in descendants_of_type(state.root, "import_declaration"):
            named = [item for item in node.named_children if item.type != "asterisk"]
            if named:
                state.add_import(state.text(named[0]), line_of(node))

        # Pass 1: every declared type and its members, so field types are known
        # before pass 2 resolves call receivers against them.
        for node in descendants_of_type(state.root, *_TYPE_NODES):
            self._type_declaration(state, node)
        for node in descendants_of_type(state.root, *_TYPE_NODES):
            self._bodies(state, node)
        state.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))

    # -- declarations ------------------------------------------------------

    def _type_declaration(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        modifiers_node = _modifiers_child(node)
        modifiers = state.text(modifiers_node) if modifiers_node is not None else ""
        annotations = _annotations(state, modifiers_node)

        symbol_type = _SYMBOL_TYPE_FOR_NODE[node.type]
        is_abstract = "abstract" in modifiers or symbol_type is SymbolType.INTERFACE
        if symbol_type is SymbolType.CLASS and is_abstract:
            symbol_type = SymbolType.ABSTRACT_CLASS

        state.add_symbol(
            name=name,
            symbol_type=symbol_type,
            line=line_of(node),
            end_line=end_line_of(node),
            visibility=_visibility(modifiers, default=Visibility.PACKAGE),
            is_abstract=is_abstract,
            is_static="static" in modifiers,
            signature=f"{node.type.removesuffix('_declaration')} {name}",
            metadata={"annotations": annotations, "modifiers": modifiers.split()},
        )

        superclass = child(node, "superclass")
        for target in _type_names(state, superclass):
            state.add_relationship(
                source=name,
                target=target,
                relation=RelationType.INHERITS,
                line=line_of(node),
                metadata={"kind": "extends"},
            )
        # `interface X extends Y` is interface inheritance, not implementation.
        for target in _type_names(state, _named_child(node, "extends_interfaces")):
            state.add_relationship(
                source=name,
                target=target,
                relation=RelationType.INHERITS,
                line=line_of(node),
                metadata={"kind": "extends"},
            )
        for target in _type_names(state, child(node, "interfaces")):
            state.add_relationship(
                source=name,
                target=target,
                relation=RelationType.IMPLEMENTS,
                line=line_of(node),
                metadata={"kind": "implements"},
            )

    def _bodies(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        body = child(node, "body")
        if name_node is None or body is None:
            return
        owner = state.text(name_node)
        is_interface = node.type in ("interface_declaration", "annotation_type_declaration")

        # Only *this* type's members: a nested type gets its own pass.
        for member in body.named_children:
            if member.type == "field_declaration":
                self._field(state, owner, member, is_interface)
            elif member.type == "method_declaration":
                self._method(state, owner, member, is_interface)
            elif member.type == "constructor_declaration":
                self._constructor(state, owner, member)

    def _field(self, state: TsState, owner: str, node, is_interface: bool) -> None:
        type_node = child(node, "type")
        modifiers_node = _modifiers_child(node)
        modifiers = state.text(modifiers_node) if modifiers_node is not None else ""
        annotations = _annotations(state, modifiers_node)
        declared = state.text(type_node) if type_node is not None else ""
        target = _element_type(declared)

        for declarator in children_of_type(node, "variable_declarator"):
            declarator_name = child(declarator, "name")
            if declarator_name is None:
                continue
            field_name = state.text(declarator_name)
            state.add_symbol(
                name=field_name,
                symbol_type=SymbolType.FIELD,
                line=line_of(declarator),
                owner=owner,
                visibility=_visibility(
                    modifiers,
                    default=Visibility.PUBLIC if is_interface else Visibility.PACKAGE,
                ),
                is_static="static" in modifiers,
                signature=f"{declared} {field_name}",
                metadata={"annotations": annotations, "field_type": declared},
            )
            if target is None:
                continue
            receiver = _receiver_type(declared)
            if receiver:
                state.field_types[f"{owner}.{field_name}"] = receiver
            injected = bool(set(annotations) & INJECTION_ANNOTATIONS)
            state.add_relationship(
                source=owner,
                target=target,
                relation=RelationType.USES if injected else RelationType.CONTAINS,
                line=line_of(declarator),
                confidence=Confidence.MEDIUM,
                metadata={
                    "kind": "dependency_injection" if injected else "composition",
                    "field": field_name,
                },
            )

    def _method(self, state: TsState, owner: str, node, is_interface: bool) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        modifiers_node = _modifiers_child(node)
        modifiers = state.text(modifiers_node) if modifiers_node is not None else ""
        annotations = _annotations(state, modifiers_node)
        body = child(node, "body")
        parameters = _parameters(state, child(node, "parameters"))

        state.add_symbol(
            name=name,
            symbol_type=SymbolType.METHOD,
            line=line_of(node),
            end_line=end_line_of(node),
            owner=owner,
            visibility=_visibility(
                modifiers,
                default=Visibility.PUBLIC if is_interface else Visibility.PACKAGE,
            ),
            is_abstract="abstract" in modifiers or body is None,
            is_static="static" in modifiers,
            signature=f"{name}({','.join(declared for declared, _ in parameters)})",
            metadata={
                "annotations": annotations,
                "return_type": state.text(child(node, "type")) if child(node, "type") else "",
                "is_override": "Override" in annotations,
            },
        )
        if set(annotations) & INJECTION_ANNOTATIONS:
            self._inject(state, owner, node, parameters, annotations, via="annotation")
        if body is not None:
            self._calls(state, owner, f"{owner}.{name}", node, body, parameters)

    def _constructor(self, state: TsState, owner: str, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        modifiers_node = _modifiers_child(node)
        modifiers = state.text(modifiers_node) if modifiers_node is not None else ""
        annotations = _annotations(state, modifiers_node)
        parameters = _parameters(state, child(node, "parameters"))

        state.add_symbol(
            name=state.text(name_node),
            symbol_type=SymbolType.CONSTRUCTOR,
            line=line_of(node),
            end_line=end_line_of(node),
            owner=owner,
            visibility=_visibility(modifiers, default=Visibility.PACKAGE),
            signature=f"{owner}({','.join(declared for declared, _ in parameters)})",
            metadata={"annotations": annotations},
        )
        self._inject(state, owner, node, parameters, annotations, via="constructor")
        body = child(node, "body")
        if body is not None:
            self._calls(state, owner, f"{owner}.{state.text(name_node)}", node, body, parameters)

    def _inject(
        self,
        state: TsState,
        owner: str,
        node,
        parameters: list[tuple[str, str]],
        annotations: list[str],
        *,
        via: str,
    ) -> None:
        for declared, _parameter_name in parameters:
            target = _element_type(declared)
            if target is None:
                continue
            state.add_relationship(
                source=owner,
                target=target,
                relation=RelationType.USES,
                line=line_of(node),
                confidence=Confidence.MEDIUM,
                metadata={
                    "kind": "dependency_injection",
                    "via": via,
                    "annotations": annotations,
                },
            )

    # -- calls -------------------------------------------------------------

    def _calls(
        self,
        state: TsState,
        owner: str,
        source: str,
        node,
        body,
        parameters: list[tuple[str, str]],
    ) -> None:
        prefix = f"{owner}."
        scope = {
            key[len(prefix) :]: value
            for key, value in state.field_types.items()
            if key.startswith(prefix)
        }
        scope.update(
            {
                name: resolved
                for declared, name in parameters
                if (resolved := _receiver_type(declared))
            }
        )
        scope.update(_local_variable_types(state, body))
        previous, state.scope_types = state.scope_types, scope
        try:
            for call in descendants_of_type(body, "method_invocation"):
                if state.call_count >= _MAX_CALLS_PER_FILE:
                    return
                name_node = child(call, "name")
                if name_node is None:
                    continue
                callee = state.text(name_node)
                object_node = child(call, "object")
                receiver = state.text(object_node) if object_node is not None else ""
                if object_node is None:
                    # An unqualified call is a method on the enclosing type.
                    resolved, how = owner, "implicit_this"
                else:
                    resolved, how = resolve_receiver(
                        state, receiver, self_type=owner, self_names=("this",)
                    )
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


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _named_child(node, node_type: str):
    """First direct child of ``node_type`` (for fields the grammar leaves unnamed)."""
    for candidate in node.named_children:
        if candidate.type == node_type:
            return candidate
    return None


def _modifiers_child(node):
    return _named_child(node, "modifiers")


def _annotations(state: TsState, modifiers_node) -> list[str]:
    """Annotation names on a declaration, without the ``@``."""
    if modifiers_node is None:
        return []
    names = []
    for annotation in descendants_of_type(
        modifiers_node, "annotation", "marker_annotation"
    ):
        name_node = child(annotation, "name")
        if name_node is not None:
            names.append(state.text(name_node).lstrip("@"))
    return sorted(set(names))


def _visibility(modifiers: str, *, default: Visibility) -> Visibility:
    if "private" in modifiers:
        return Visibility.PRIVATE
    if "protected" in modifiers:
        return Visibility.PROTECTED
    if "public" in modifiers:
        return Visibility.PUBLIC
    return default


def _type_names(state: TsState, node) -> list[str]:
    """Simple type names inside a ``superclass``/``super_interfaces`` clause."""
    if node is None:
        return []
    names = []
    for candidate in descendants_of_type(node, "type_identifier", "scoped_type_identifier"):
        names.append(state.text(candidate).rsplit(".", 1)[-1])
    return names


def _parameters(state: TsState, parameters_node) -> list[tuple[str, str]]:
    """``[(declared type, parameter name)]`` for a formal parameter list."""
    if parameters_node is None:
        return []
    collected = []
    for parameter in children_of_type(
        parameters_node, "formal_parameter", "spread_parameter"
    ):
        type_node = child(parameter, "type")
        name_node = child(parameter, "name")
        collected.append(
            (
                state.text(type_node) if type_node is not None else "",
                state.text(name_node) if name_node is not None else "",
            )
        )
    return collected


def _local_variable_types(state: TsState, body) -> dict[str, str]:
    """Declared types of local variables, for receiver resolution."""
    types: dict[str, str] = {}
    for declaration in descendants_of_type(body, "local_variable_declaration"):
        type_node = child(declaration, "type")
        resolved = _receiver_type(state.text(type_node)) if type_node is not None else None
        if resolved is None:
            continue
        for declarator in children_of_type(declaration, "variable_declarator"):
            name_node = child(declarator, "name")
            if name_node is not None:
                types[state.text(name_node)] = resolved
    return types


def _receiver_type(declared: str) -> str | None:
    """Type of a *call receiver*, or ``None`` when it has no single named type.

    ``User[] users`` makes ``users.length`` a call on the array, not on
    ``User`` — so arrays resolve to nothing rather than to their element type.
    ``List<User>`` resolves to ``List``, which is what the receiver actually is.
    """
    text = (declared or "").strip()
    if not text or text.endswith("[]") or text.endswith("..."):
        return None
    cut = text.find("<")
    if cut != -1:
        text = text[:cut]
    text = text.strip().rsplit(".", 1)[-1]
    if not text or not text.replace("_", "a").isalnum() or text in JAVA_BUILTINS:
        return None
    return text


def _element_type(declared: str) -> str | None:
    """Meaningful type name of a Java type expression, or ``None``.

    ``List<User>`` → ``List``; ``User[]`` → ``User``; ``int`` → ``None``
    (primitive); ``String`` → ``None`` (ubiquitous, carries no design signal).
    """
    text = (declared or "").strip().removesuffix("...")
    while text.endswith("[]"):
        text = text[:-2].strip()
    cut = text.find("<")
    if cut != -1:
        text = text[:cut]
    text = text.strip().rsplit(".", 1)[-1]
    if not text or not text.replace("_", "a").isalnum():
        return None
    if text in JAVA_BUILTINS:
        return None
    return text


__all__ = ["INJECTION_ANNOTATIONS", "JAVA_BUILTINS", "JavaTreeSitterAnalyzer"]
