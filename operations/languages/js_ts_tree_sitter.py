"""JavaScript and TypeScript analyzers backed by tree-sitter grammars.

One extractor serves both: TypeScript is a superset, and the extra nodes it
adds (``interface_declaration``, ``implements_clause``, ``abstract_class_declaration``,
``accessibility_modifier``) are simply absent from a JavaScript tree.

Compared with the lexical fallback in
:mod:`operations.languages.js_ts_analyzer`, the grammar removes the two things
a brace scanner cannot do reliably: distinguishing a regex literal from a
comment, and telling a class field initialised with a call
(``handler = make()``) from a method. It also enables receiver-typed calls —
``this.logger.log()`` resolves through the field's declared type to
``Logger.log``.

Dynamic patterns (``Object.assign`` mixins, prototype manipulation, HOC
wrapping) remain undetected: a syntax tree does not make them statically
knowable, and guessing would be worse than reporting nothing.
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
JS_BUILTINS: frozenset[str] = frozenset(
    {
        "string", "number", "boolean", "any", "unknown", "never", "void",
        "object", "symbol", "bigint", "null", "undefined", "Array", "Promise",
        "Record", "Map", "Set", "Date", "RegExp", "Function", "Object",
        "String", "Number", "Boolean",
    }
)

#: Decorators that mark a class as container-managed (Angular/NestJS style).
INJECTABLE_DECORATORS: frozenset[str] = frozenset(
    {"Injectable", "Component", "Controller", "Directive", "Pipe", "Module"}
)

_CLASS_NODES = ("class_declaration", "abstract_class_declaration", "class")
_MAX_CALLS_PER_FILE = 400


class JsTsTreeSitterAnalyzer(TreeSitterAnalyzer):
    """Shared extractor for the JavaScript and TypeScript tree-sitter backends."""

    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")

    def extract(self, state: TsState) -> None:
        state.package = module_path(state.file_path)
        self._imports(state)
        for node in descendants_of_type(state.root, "interface_declaration"):
            self._interface(state, node)
        for node in descendants_of_type(state.root, "type_alias_declaration"):
            self._type_alias(state, node)
        # Classes first (they fill field_types), then anything left at module level.
        class_nodes = descendants_of_type(state.root, *_CLASS_NODES)
        for node in class_nodes:
            self._class(state, node)
        for node in class_nodes:
            self._class_body(state, node)
        self._functions(state, class_nodes)
        state.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))

    # -- imports -----------------------------------------------------------

    def _imports(self, state: TsState) -> None:
        for node in descendants_of_type(state.root, "import_statement", "export_statement"):
            source = child(node, "source")
            if source is not None:
                state.add_import(_string_value(state, source), line_of(node))
        for call in descendants_of_type(state.root, "call_expression"):
            function = child(call, "function")
            if function is None or state.text(function) != "require":
                continue
            arguments = child(call, "arguments")
            strings = descendants_of_type(arguments, "string") if arguments else []
            if strings:
                state.add_import(_string_value(state, strings[0]), line_of(call))

    # -- interfaces and aliases -------------------------------------------

    def _interface(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        state.add_symbol(
            name=name,
            symbol_type=SymbolType.INTERFACE,
            line=line_of(node),
            end_line=end_line_of(node),
            visibility=Visibility.PUBLIC,
            is_abstract=True,
            signature=f"interface {name}",
        )
        for clause in descendants_of_type(node, "extends_type_clause"):
            for target in descendants_of_type(clause, "type_identifier"):
                state.add_relationship(
                    source=name,
                    target=state.text(target),
                    relation=RelationType.INHERITS,
                    line=line_of(node),
                    metadata={"kind": "interface_extends"},
                )
        body = child(node, "body")
        for member in body.named_children if body is not None else []:
            member_name = child(member, "name")
            if member_name is None:
                continue
            state.add_symbol(
                name=state.text(member_name),
                symbol_type=SymbolType.METHOD
                if member.type in ("method_signature", "call_signature")
                else SymbolType.FIELD,
                line=line_of(member),
                owner=name,
                visibility=Visibility.PUBLIC,
                is_abstract=True,
                signature=state.text(member)[:120],
            )

    def _type_alias(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        state.add_symbol(
            name=state.text(name_node),
            symbol_type=SymbolType.TYPE_ALIAS,
            line=line_of(node),
            end_line=end_line_of(node),
            visibility=Visibility.PUBLIC,
            signature=f"type {state.text(name_node)}",
        )

    # -- classes -----------------------------------------------------------

    def _class(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        is_abstract = node.type == "abstract_class_declaration"
        decorators = _decorators(state, node)
        state.add_symbol(
            name=name,
            symbol_type=SymbolType.ABSTRACT_CLASS if is_abstract else SymbolType.CLASS,
            line=line_of(node),
            end_line=end_line_of(node),
            visibility=Visibility.PUBLIC,
            is_abstract=is_abstract,
            signature=f"class {name}",
            metadata={"decorators": decorators},
        )
        for clause in children_of_type(node, "class_heritage"):
            # The two grammars disagree: TypeScript wraps the superclass in an
            # `extends_clause`, JavaScript hangs the identifier directly off
            # `class_heritage`. Handle both, or JS inheritance vanishes.
            extends_clauses = children_of_type(clause, "extends_clause")
            superclasses = []
            for extends in extends_clauses:
                value = child(extends, "value")
                superclasses.extend(
                    [value] if value is not None
                    else descendants_of_type(extends, "identifier", "member_expression")
                )
            if not extends_clauses:
                superclasses.extend(
                    children_of_type(clause, "identifier", "member_expression")
                )
            for target in superclasses:
                state.add_relationship(
                    source=name,
                    target=state.text(target).rsplit(".", 1)[-1],
                    relation=RelationType.INHERITS,
                    line=line_of(node),
                    metadata={"kind": "extends"},
                )
            for implements in children_of_type(clause, "implements_clause"):
                for target in descendants_of_type(implements, "type_identifier"):
                    state.add_relationship(
                        source=name,
                        target=state.text(target),
                        relation=RelationType.IMPLEMENTS,
                        line=line_of(node),
                        metadata={"kind": "implements"},
                    )

    def _class_body(self, state: TsState, node) -> None:
        name_node = child(node, "name")
        body = child(node, "body")
        if name_node is None or body is None:
            return
        owner = state.text(name_node)
        injectable = bool(set(_decorators(state, node)) & INJECTABLE_DECORATORS)

        for member in body.named_children:
            if member.type in ("public_field_definition", "field_definition"):
                self._field(state, owner, member)
            elif member.type in ("method_definition", "abstract_method_signature"):
                self._method(state, owner, member, injectable)

    def _field(self, state: TsState, owner: str, node) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        modifiers = [state.text(item) for item in children_of_type(node, "accessibility_modifier")]
        declared = _annotation_type(state, child(node, "type"))
        state.add_symbol(
            name=name,
            symbol_type=SymbolType.FIELD,
            line=line_of(node),
            owner=owner,
            visibility=_member_visibility(name, modifiers),
            is_static=any(item.type == "static" for item in node.children),
            signature=f"{name}: {declared}" if declared else name,
            metadata={"modifiers": modifiers, "field_type": declared},
        )
        target = _element_type(declared)
        if target is None:
            return
        receiver = _receiver_type(declared)
        if receiver:
            state.field_types[f"{owner}.{name}"] = receiver
        state.add_relationship(
            source=owner,
            target=target,
            relation=RelationType.CONTAINS,
            line=line_of(node),
            confidence=Confidence.MEDIUM,
            metadata={"kind": "composition", "field": name},
        )

    def _method(self, state: TsState, owner: str, node, injectable: bool) -> None:
        name_node = child(node, "name")
        if name_node is None:
            return
        name = state.text(name_node)
        modifiers = [state.text(item) for item in children_of_type(node, "accessibility_modifier")]
        is_constructor = name == "constructor"
        parameters = _parameters(state, child(node, "parameters"))
        body = child(node, "body")

        state.add_symbol(
            name=name,
            symbol_type=SymbolType.CONSTRUCTOR if is_constructor else SymbolType.METHOD,
            line=line_of(node),
            end_line=end_line_of(node),
            owner=owner,
            visibility=_member_visibility(name, modifiers),
            is_abstract=node.type == "abstract_method_signature",
            is_static=any(item.type == "static" for item in node.children),
            signature=f"{name}({', '.join(item.name for item in parameters)})",
            metadata={
                "modifiers": modifiers,
                "decorators": _decorators(state, node),
                "is_override": "override" in state.text(node)[:60],
            },
        )

        if is_constructor:
            for parameter in parameters:
                target = _element_type(parameter.declared)
                if target is None:
                    continue
                if parameter.is_property:
                    # `constructor(private readonly logger: Logger)` declares a
                    # field as well as a parameter.
                    receiver = _receiver_type(parameter.declared)
                    if receiver:
                        state.field_types[f"{owner}.{parameter.name}"] = receiver
                state.add_relationship(
                    source=owner,
                    target=target,
                    relation=RelationType.USES,
                    line=line_of(node),
                    confidence=Confidence.MEDIUM,
                    metadata={
                        "kind": "dependency_injection",
                        "via": "parameter_property" if parameter.is_property else "constructor",
                        "injectable_class": injectable,
                    },
                )

        if body is not None:
            self._calls(state, owner, f"{owner}.{name}", body, parameters)

    # -- functions ---------------------------------------------------------

    def _functions(self, state: TsState, class_nodes: list) -> None:
        spans = [(node.start_byte, node.end_byte) for node in class_nodes]

        def inside_class(node) -> bool:
            return any(start <= node.start_byte < end for start, end in spans)

        for node in descendants_of_type(
            state.root, "function_declaration", "generator_function_declaration"
        ):
            name_node = child(node, "name")
            if name_node is None or inside_class(node):
                continue
            name = state.text(name_node)
            parameters = _parameters(state, child(node, "parameters"))
            state.add_symbol(
                name=name,
                symbol_type=SymbolType.FUNCTION,
                line=line_of(node),
                end_line=end_line_of(node),
                visibility=Visibility.PUBLIC,
                signature=f"{name}({', '.join(item.name for item in parameters)})",
                metadata={"form": "function"},
            )
            body = child(node, "body")
            if body is not None:
                self._calls(state, None, name, body, parameters)

        for declarator in descendants_of_type(state.root, "variable_declarator"):
            value = child(declarator, "value")
            name_node = child(declarator, "name")
            if (
                value is None
                or name_node is None
                or value.type not in ("arrow_function", "function_expression", "function")
                or inside_class(declarator)
            ):
                continue
            name = state.text(name_node)
            state.add_symbol(
                name=name,
                symbol_type=SymbolType.FUNCTION,
                line=line_of(declarator),
                end_line=end_line_of(declarator),
                visibility=Visibility.PUBLIC,
                signature=f"{name}(...)",
                metadata={"form": "arrow"},
            )
            body = child(value, "body")
            if body is not None:
                self._calls(state, None, name, body, _parameters(state, child(value, "parameters")))

    # -- calls -------------------------------------------------------------

    def _calls(
        self,
        state: TsState,
        owner: str | None,
        source: str,
        body,
        parameters: list[_Parameter],
    ) -> None:
        scope = {
            parameter.name: resolved
            for parameter in parameters
            if (resolved := _receiver_type(parameter.declared))
        }
        previous, state.scope_types = state.scope_types, scope
        try:
            for call in descendants_of_type(body, "call_expression"):
                if state.call_count >= _MAX_CALLS_PER_FILE:
                    return
                function = child(call, "function")
                if function is None:
                    continue
                if function.type == "member_expression":
                    property_node = child(function, "property")
                    object_node = child(function, "object")
                    if property_node is None:
                        continue
                    callee = state.text(property_node)
                    receiver = state.text(object_node) if object_node is not None else ""
                    resolved, how = resolve_receiver(
                        state, receiver, self_type=owner, self_names=("this",)
                    )
                elif function.type == "identifier":
                    callee, receiver, resolved, how = (
                        state.text(function),
                        "",
                        None,
                        "unqualified",
                    )
                else:
                    continue
                if callee in ("require", "super"):
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
            for instantiation in descendants_of_type(body, "new_expression"):
                if state.call_count >= _MAX_CALLS_PER_FILE:
                    return
                constructor = child(instantiation, "constructor")
                if constructor is None or constructor.type not in (
                    "identifier",
                    "member_expression",
                ):
                    continue
                constructed = state.text(constructor).rsplit(".", 1)[-1]
                state.call_count += 1
                state.add_relationship(
                    source=source,
                    target=constructed,
                    relation=RelationType.CALLS,
                    line=line_of(instantiation),
                    metadata={"kind": "instantiation", "callee": constructed},
                )
        finally:
            state.scope_types = previous


class JavaScriptTreeSitterAnalyzer(JsTsTreeSitterAnalyzer):
    """Analyzer for ``.js``/``.jsx``/``.mjs``/``.cjs`` sources."""

    language = "javascript"
    grammar = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")


class TypeScriptTreeSitterAnalyzer(JsTsTreeSitterAnalyzer):
    """Analyzer for ``.ts``/``.tsx``/``.mts``/``.cts`` sources."""

    language = "typescript"
    grammar = "typescript"
    extensions = (".ts", ".mts", ".cts", ".tsx")

    def analyze(self, file_path: str, source: str):
        # `.tsx` needs the TSX grammar: the plain TypeScript grammar parses JSX
        # as a comparison chain and produces nonsense.
        self.grammar = "tsx" if file_path.endswith(".tsx") else "typescript"
        try:
            return super().analyze(file_path, source)
        finally:
            self.grammar = "typescript"


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


class _Parameter:
    """One parameter: its name, its declared type, and whether it declares a field."""

    __slots__ = ("name", "declared", "is_property")

    def __init__(self, name: str, declared: str, is_property: bool) -> None:
        self.name = name
        self.declared = declared
        self.is_property = is_property


def module_path(file_path: str) -> str:
    """Module identity for a JS/TS file: its path without the extension."""
    for extension in (".d.ts", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".mts", ".cts"):
        if file_path.endswith(extension):
            return file_path[: -len(extension)]
    return file_path


def _string_value(state: TsState, node) -> str:
    fragments = descendants_of_type(node, "string_fragment")
    if fragments:
        return state.text(fragments[0])
    return state.text(node).strip("'\"`")


def _decorators(state: TsState, node) -> list[str]:
    """Decorator names attached to a declaration, without the ``@``."""
    names: list[str] = []
    parent = node.parent
    candidates = list(children_of_type(node, "decorator"))
    if parent is not None:
        candidates += children_of_type(parent, "decorator")
    for decorator in candidates:
        text = state.text(decorator).lstrip("@")
        names.append(text.split("(")[0].strip().rsplit(".", 1)[-1])
    return sorted(set(names))


def _annotation_type(state: TsState, type_node) -> str:
    """Text of a ``: Type`` annotation, without the colon."""
    if type_node is None:
        return ""
    return state.text(type_node).lstrip(":").strip()


def _parameters(state: TsState, parameters_node) -> list[_Parameter]:
    """Parameters of a function/method, including TypeScript parameter properties."""
    if parameters_node is None:
        return []
    collected: list[_Parameter] = []
    for parameter in children_of_type(
        parameters_node, "required_parameter", "optional_parameter"
    ):
        pattern = child(parameter, "pattern")
        name = state.text(pattern) if pattern is not None else ""
        declared = _annotation_type(state, child(parameter, "type"))
        is_property = bool(children_of_type(parameter, "accessibility_modifier")) or (
            "readonly" in state.text(parameter)[:40]
        )
        collected.append(_Parameter(name, declared, is_property))
    for parameter in children_of_type(parameters_node, "identifier"):
        collected.append(_Parameter(state.text(parameter), "", False))
    return collected


def _member_visibility(name: str, modifiers: list[str]) -> Visibility:
    """TypeScript modifiers win; otherwise ``#name`` is private, the rest public."""
    if "private" in modifiers:
        return Visibility.PRIVATE
    if "protected" in modifiers:
        return Visibility.PROTECTED
    if "public" in modifiers:
        return Visibility.PUBLIC
    if name.startswith("#"):
        return Visibility.PRIVATE
    return Visibility.PUBLIC


def _receiver_type(declared: str) -> str | None:
    """Type of a *call receiver*, or ``None`` when it has no single named type.

    ``shapes: Measurable[]`` makes ``shapes.reduce()`` a call on the array, not
    on ``Measurable`` — resolving it to the element type would invent a method
    the interface never declares.
    """
    text = (declared or "").strip()
    if not text or text.endswith("[]") or text.startswith(("Array<", "ReadonlyArray<")):
        return None
    for part in text.split("|"):
        candidate = part.strip()
        if candidate and candidate not in ("null", "undefined"):
            text = candidate
            break
    if text.endswith("[]"):
        return None
    cut = text.find("<")
    if cut != -1:
        text = text[:cut]
    text = text.strip().rsplit(".", 1)[-1]
    if not text or not text.replace("_", "a").replace("$", "a").isalnum():
        return None
    if text in JS_BUILTINS:
        return None
    return text


def _element_type(declared: str) -> str | None:
    """Meaningful type name of a TypeScript annotation, or ``None``.

    ``Measurable[]`` → ``Measurable``; ``Map<string, number>`` → ``None``
    (builtin); ``Logger | null`` → ``Logger``; ``number`` → ``None``.
    """
    text = (declared or "").strip()
    if not text:
        return None
    # A union: take the first non-nullish member.
    for part in text.split("|"):
        candidate = part.strip()
        if candidate and candidate not in ("null", "undefined"):
            text = candidate
            break
    while text.endswith("[]"):
        text = text[:-2].strip()
    cut = text.find("<")
    if cut != -1:
        text = text[:cut]
    text = text.strip().rsplit(".", 1)[-1]
    if not text or not text.replace("_", "a").replace("$", "a").isalnum():
        return None
    if text in JS_BUILTINS:
        return None
    return text


__all__ = [
    "INJECTABLE_DECORATORS",
    "JS_BUILTINS",
    "JavaScriptTreeSitterAnalyzer",
    "JsTsTreeSitterAnalyzer",
    "TypeScriptTreeSitterAnalyzer",
    "module_path",
]
