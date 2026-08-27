"""JavaScript and TypeScript analyzer.

One implementation serves both languages: TypeScript is a superset, and the
extra constructs it adds (``interface``, ``implements``, ``abstract``,
parameter properties, access modifiers) are simply absent from JavaScript
sources. :class:`JavaScriptAnalyzer` and :class:`TypeScriptAnalyzer` are thin
subclasses so the registry, the reported ``language`` field and the per-language
tests stay distinct.

Explicit syntax (``extends``, ``implements``) is reported with
:attr:`~operations.schemas.Confidence.HIGH`. Everything the language leaves to
convention — a field type meaning composition, a constructor parameter property
meaning injection — stays ``MEDIUM``. Dynamic patterns (``Object.assign``
mixins, prototype manipulation, HOC wrapping) are *not* detected, and this is
stated rather than approximated.
"""

from __future__ import annotations

import re

from operations.languages.base import (
    IDENTIFIER,
    JS_SYNTAX,
    LanguageAnalyzer,
    LineIndex,
    find_matching,
    first_top_level_paren,
    iter_declaration_segments,
    mask_code,
    modifier_visibility,
    simple_name,
    split_top_level,
    strip_generics,
)
from operations.schemas import (
    Confidence,
    DetectionMethod,
    FileAnalysis,
    RelationshipInfo,
    RelationType,
    SymbolInfo,
    SymbolType,
    Visibility,
)

_IMPORT_FROM_RE = re.compile(r"""\bimport\b[^;\n]*?\bfrom\s*['"]([^'"\n]+)['"]""")
_IMPORT_BARE_RE = re.compile(r"""\bimport\s*['"]([^'"\n]+)['"]""")
_EXPORT_FROM_RE = re.compile(r"""\bexport\b[^;\n]*?\bfrom\s*['"]([^'"\n]+)['"]""")
_REQUIRE_RE = re.compile(r"""\brequire\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")

_CLASS_RE = re.compile(
    rf"\b(?:export\s+)?(?:default\s+)?(?P<abstract>abstract\s+)?class\s+"
    rf"(?P<name>{IDENTIFIER})(?:\s*<[^{{]*?>)?(?P<tail>[^{{]*)\{{"
)
_INTERFACE_RE = re.compile(
    rf"\b(?:export\s+)?(?:declare\s+)?interface\s+(?P<name>{IDENTIFIER})"
    rf"(?:\s*<[^{{]*?>)?(?P<tail>[^{{]*)\{{"
)
_TYPE_ALIAS_RE = re.compile(
    rf"\b(?:export\s+)?(?:declare\s+)?type\s+(?P<name>{IDENTIFIER})\s*(?:<[^=]*?>)?\s*="
)
_FUNCTION_RE = re.compile(
    rf"\b(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*"
    rf"(?P<name>{IDENTIFIER})\s*(?:<[^(]*?>)?\s*\("
)
_ARROW_RE = re.compile(
    rf"\b(?:export\s+)?(?:const|let|var)\s+(?P<name>{IDENTIFIER})\s*"
    rf"(?::[^=\n]+)?=\s*(?:async\s+)?(?:\([^()]*\)|{IDENTIFIER})\s*(?::[^=>\n]+)?=>"
)
_DECORATOR_RE = re.compile(rf"@({IDENTIFIER})")

#: TypeScript parameter-property / member modifiers.
_TS_MODIFIERS = frozenset(
    {"public", "private", "protected", "readonly", "static", "abstract", "override", "declare"}
)

#: Decorators that mark a class as container-managed (Angular/NestJS style).
_INJECTABLE_DECORATORS = frozenset(
    {"Injectable", "Component", "Controller", "Directive", "Pipe", "Module"}
)

_BUILTIN_TYPES = frozenset(
    {
        "string", "number", "boolean", "any", "unknown", "never", "void",
        "object", "symbol", "bigint", "null", "undefined", "Array", "Promise",
        "Record", "Map", "Set", "Date", "RegExp", "Function", "Object", "String",
        "Number", "Boolean",
    }
)

_JS_NON_CALLS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "return", "function", "super",
        "typeof", "await", "new", "do", "else", "throw", "yield", "constructor",
    }
)

_CALL_RE = re.compile(rf"(?:(?:{IDENTIFIER})\s*\.\s*)?({IDENTIFIER})\s*\(")
_MAX_CALLS_PER_FILE = 400


class JsTsAnalyzer(LanguageAnalyzer):
    """Shared implementation for the JavaScript and TypeScript analyzers."""

    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")
    detection_method = DetectionMethod.LEXICAL_PARSE
    base_confidence = Confidence.MEDIUM

    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        masked = mask_code(source, JS_SYNTAX)
        state = _JsState(
            file_path=file_path,
            source=source,
            masked=masked,
            lines=LineIndex(source),
            language=self.language,
        )
        state.package = module_path(file_path)

        self._parse_imports(state)
        class_spans = self._parse_classes(state)
        self._parse_interfaces(state)
        self._parse_type_aliases(state, class_spans)
        self._parse_functions(state, class_spans)
        state.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))

        return FileAnalysis(
            file_path=file_path,
            language=self.language,
            package=state.package,
            symbols=state.symbols,
            relationships=state.relationships,
            imports=sorted(set(state.imports)),
        )

    # -- imports -----------------------------------------------------------

    def _parse_imports(self, state: _JsState) -> None:
        for pattern in (_IMPORT_FROM_RE, _IMPORT_BARE_RE, _EXPORT_FROM_RE, _REQUIRE_RE):
            # Import specifiers live inside string literals, which the mask
            # blanks — so imports are read from the original source, then
            # cross-checked against the mask to skip commented-out lines.
            for match in pattern.finditer(state.source):
                if state.masked[match.start() : match.end()].strip() == "":
                    continue
                module = match.group(1)
                state.imports.append(module)
                state.add_relationship(
                    source=state.package,
                    target=module,
                    relation=RelationType.IMPORTS,
                    line=state.lines.line_of(match.start()),
                    confidence=Confidence.HIGH,
                    metadata={"kind": "import"},
                )

    # -- classes -----------------------------------------------------------

    def _parse_classes(self, state: _JsState) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        for match in _CLASS_RE.finditer(state.masked):
            name = match.group("name")
            tail = match.group("tail") or ""
            open_index = match.end() - 1
            block_end = find_matching(state.masked, open_index)
            spans.append((open_index, block_end))

            line = state.lines.line_of(match.start())
            is_abstract = bool(match.group("abstract"))
            decorators = _decorators_before(state.masked, match.start())
            state.add_symbol(
                name=name,
                symbol_type=SymbolType.ABSTRACT_CLASS if is_abstract else SymbolType.CLASS,
                line=line,
                end_line=state.lines.line_of(block_end - 1),
                visibility=Visibility.PUBLIC,
                is_abstract=is_abstract,
                signature=f"class {name}",
                confidence=Confidence.HIGH,
                metadata={"decorators": decorators},
            )

            extends = re.search(rf"\bextends\s+([\w\.]+)", tail)
            if extends:
                state.add_relationship(
                    source=name,
                    target=simple_name(extends.group(1)),
                    relation=RelationType.INHERITS,
                    line=line,
                    confidence=Confidence.HIGH,
                    metadata={"kind": "extends"},
                )
            implements = re.search(r"\bimplements\b(?P<names>[^{]*)", tail)
            if implements:
                for entry in split_top_level(implements.group("names")):
                    target = simple_name(entry)
                    if target:
                        state.add_relationship(
                            source=name,
                            target=target,
                            relation=RelationType.IMPLEMENTS,
                            line=line,
                            confidence=Confidence.HIGH,
                            metadata={"kind": "implements"},
                        )

            self._parse_class_body(
                state, name, open_index + 1, block_end - 1, decorators
            )
        return spans

    def _parse_class_body(
        self,
        state: _JsState,
        owner: str,
        start: int,
        end: int,
        class_decorators: list[str],
    ) -> None:
        injectable = bool(set(class_decorators) & _INJECTABLE_DECORATORS)
        for header_start, header_end, block in iter_declaration_segments(
            state.masked, start, end
        ):
            header = state.masked[header_start:header_end]
            if not header.strip():
                continue
            paren = first_top_level_paren(header)
            if paren is not None and _looks_like_member_signature(header, paren):
                self._handle_method(
                    state, owner, header_start, header, paren, block, injectable
                )
            else:
                self._handle_field(state, owner, header_start, header)

    def _handle_method(
        self,
        state: _JsState,
        owner: str,
        header_start: int,
        header: str,
        paren: tuple[int, int],
        block: tuple[int, int] | None,
        injectable: bool,
    ) -> None:
        open_index, close_index = paren
        name_match = re.search(rf"(#?{IDENTIFIER})\s*\??\s*$", header[:open_index])
        if name_match is None:
            return
        name = name_match.group(1)
        prefix = header[: name_match.start()]
        modifiers = [
            token for token in re.findall(IDENTIFIER, prefix) if token in _TS_MODIFIERS
        ]
        parameters = split_top_level(header[open_index + 1 : close_index])
        line = state.lines.line_of(header_start + name_match.start())

        symbol_type = (
            SymbolType.CONSTRUCTOR if name == "constructor" else SymbolType.METHOD
        )
        state.add_symbol(
            name=name,
            symbol_type=symbol_type,
            line=line,
            end_line=state.lines.line_of(block[1] - 1) if block else line,
            owner=owner,
            visibility=_member_visibility(name, modifiers),
            is_abstract="abstract" in modifiers or block is None,
            is_static="static" in modifiers,
            signature=f"{name}({', '.join(_parameter_names(parameters))})",
            confidence=Confidence.HIGH,
            metadata={
                "modifiers": modifiers,
                "decorators": _decorators_before(state.masked, header_start),
                "is_override": "override" in modifiers,
            },
        )

        if symbol_type is SymbolType.CONSTRUCTOR:
            for parameter in parameters:
                annotated = _parameter_type(parameter)
                if not annotated or annotated in _BUILTIN_TYPES:
                    continue
                is_parameter_property = any(
                    modifier in parameter for modifier in ("private", "public", "protected", "readonly")
                )
                state.add_relationship(
                    source=owner,
                    target=annotated,
                    relation=RelationType.USES,
                    line=line,
                    confidence=Confidence.MEDIUM,
                    metadata={
                        "kind": "dependency_injection",
                        "via": "parameter_property"
                        if is_parameter_property
                        else "constructor",
                        "injectable_class": injectable,
                    },
                )

        if block is not None:
            self._parse_calls(state, f"{owner}.{name}", block)

    def _handle_field(
        self, state: _JsState, owner: str, header_start: int, header: str
    ) -> None:
        cleaned = _DECORATOR_RE.sub(" ", header).split("=", 1)[0]
        match = re.search(
            rf"(?P<name>#?{IDENTIFIER})\s*\??\s*(?::\s*(?P<type>[^;=]+))?\s*$", cleaned
        )
        if match is None:
            return
        name = match.group("name")
        if name in _TS_MODIFIERS or name in ("class", "interface", "return"):
            return
        modifiers = [
            token
            for token in re.findall(IDENTIFIER, cleaned[: match.start("name")])
            if token in _TS_MODIFIERS
        ]
        field_type = strip_generics((match.group("type") or "").strip())
        line = state.lines.line_of(header_start + match.start("name"))
        state.add_symbol(
            name=name,
            symbol_type=SymbolType.FIELD,
            line=line,
            owner=owner,
            visibility=_member_visibility(name, modifiers),
            is_static="static" in modifiers,
            signature=f"{name}: {field_type}" if field_type else name,
            confidence=Confidence.MEDIUM,
            metadata={"modifiers": modifiers, "field_type": field_type},
        )
        target = simple_name(field_type)
        if target and target not in _BUILTIN_TYPES:
            state.add_relationship(
                source=owner,
                target=target,
                relation=RelationType.CONTAINS,
                line=line,
                confidence=Confidence.MEDIUM,
                metadata={"kind": "composition", "field": name},
            )

    # -- interfaces and type aliases --------------------------------------

    def _parse_interfaces(self, state: _JsState) -> None:
        for match in _INTERFACE_RE.finditer(state.masked):
            name = match.group("name")
            tail = match.group("tail") or ""
            open_index = match.end() - 1
            block_end = find_matching(state.masked, open_index)
            line = state.lines.line_of(match.start())
            state.add_symbol(
                name=name,
                symbol_type=SymbolType.INTERFACE,
                line=line,
                end_line=state.lines.line_of(block_end - 1),
                visibility=Visibility.PUBLIC,
                is_abstract=True,
                signature=f"interface {name}",
                confidence=Confidence.HIGH,
            )
            extends = re.search(r"\bextends\b(?P<names>[^{]*)", tail)
            if extends:
                for entry in split_top_level(extends.group("names")):
                    target = simple_name(entry)
                    if target:
                        state.add_relationship(
                            source=name,
                            target=target,
                            relation=RelationType.INHERITS,
                            line=line,
                            confidence=Confidence.HIGH,
                            metadata={"kind": "interface_extends"},
                        )
            self._parse_interface_body(state, name, open_index + 1, block_end - 1)

    def _parse_interface_body(
        self, state: _JsState, owner: str, start: int, end: int
    ) -> None:
        body = state.masked[start:end]
        offset = start
        for raw_line in body.splitlines(keepends=True):
            text = raw_line.strip().rstrip(";,")
            line_number = state.lines.line_of(offset)
            offset += len(raw_line)
            if not text or text in ("{", "}"):
                continue
            member = re.match(rf"^(?:readonly\s+)?({IDENTIFIER})\s*\??\s*(\(|:)", text)
            if member is None:
                continue
            state.add_symbol(
                name=member.group(1),
                symbol_type=SymbolType.METHOD
                if member.group(2) == "("
                else SymbolType.FIELD,
                line=line_number,
                owner=owner,
                visibility=Visibility.PUBLIC,
                is_abstract=True,
                signature=text,
                confidence=Confidence.HIGH,
            )

    def _parse_type_aliases(
        self, state: _JsState, class_spans: list[tuple[int, int]]
    ) -> None:
        for match in _TYPE_ALIAS_RE.finditer(state.masked):
            if _inside(match.start(), class_spans):
                continue
            state.add_symbol(
                name=match.group("name"),
                symbol_type=SymbolType.TYPE_ALIAS,
                line=state.lines.line_of(match.start()),
                visibility=Visibility.PUBLIC,
                signature=f"type {match.group('name')}",
                confidence=Confidence.HIGH,
            )

    # -- functions ---------------------------------------------------------

    def _parse_functions(
        self, state: _JsState, class_spans: list[tuple[int, int]]
    ) -> None:
        for pattern, kind in ((_FUNCTION_RE, "function"), (_ARROW_RE, "arrow")):
            for match in pattern.finditer(state.masked):
                if _inside(match.start(), class_spans):
                    continue
                name = match.group("name")
                line = state.lines.line_of(match.start())
                body_start = state.masked.find("{", match.end() - 1)
                end_line = line
                if body_start != -1:
                    body_end = find_matching(state.masked, body_start)
                    end_line = state.lines.line_of(body_end - 1)
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.FUNCTION,
                    line=line,
                    end_line=end_line,
                    visibility=Visibility.PUBLIC,
                    signature=f"{name}(...)",
                    confidence=Confidence.HIGH,
                    metadata={"form": kind},
                )
                if body_start != -1:
                    self._parse_calls(
                        state, name, (body_start, find_matching(state.masked, body_start))
                    )

    def _parse_calls(self, state: _JsState, scope: str, block: tuple[int, int]) -> None:
        if state.call_count >= _MAX_CALLS_PER_FILE:
            return
        body = state.masked[block[0] : block[1]]
        for match in _CALL_RE.finditer(body):
            callee = match.group(1)
            if callee in _JS_NON_CALLS:
                continue
            state.call_count += 1
            if state.call_count > _MAX_CALLS_PER_FILE:
                return
            state.add_relationship(
                source=scope,
                target=callee,
                relation=RelationType.CALLS,
                line=state.lines.line_of(block[0] + match.start()),
                confidence=Confidence.MEDIUM,
                metadata={"kind": "call"},
            )


class JavaScriptAnalyzer(JsTsAnalyzer):
    """Analyzer for ``.js``/``.jsx``/``.mjs``/``.cjs`` sources."""

    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")


class TypeScriptAnalyzer(JsTsAnalyzer):
    """Analyzer for ``.ts``/``.tsx``/``.mts``/``.cts`` sources."""

    language = "typescript"
    extensions = (".ts", ".tsx", ".mts", ".cts")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def module_path(file_path: str) -> str:
    """Module identity for a JS/TS file: its path without the extension."""
    for extension in (".d.ts", ".tsx", ".ts", ".jsx", ".js", ".mjs", ".cjs", ".mts", ".cts"):
        if file_path.endswith(extension):
            return file_path[: -len(extension)]
    return file_path


def _inside(position: int, spans: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in spans)


def _decorators_before(masked: str, position: int) -> list[str]:
    """Decorator names in the ~300 characters preceding a declaration."""
    window = masked[max(0, position - 300) : position]
    window = window.rsplit("}", 1)[-1].rsplit(";", 1)[-1]
    return sorted(set(_DECORATOR_RE.findall(window)))


def _looks_like_member_signature(header: str, paren: tuple[int, int]) -> bool:
    """Whether a class-body header is a method rather than a field initializer.

    A field like ``handler = compute(1)`` also contains parentheses; a method
    signature has its name immediately before the ``(`` and no ``=`` in front.
    """
    before = header[: paren[0]]
    if "=" in before:
        return False
    return re.search(rf"(#?{IDENTIFIER})\s*\??\s*$", before) is not None


def _member_visibility(name: str, modifiers: list[str]) -> Visibility:
    """TS modifiers win; otherwise ``#name`` is private and the rest public."""
    explicit = modifier_visibility(" ".join(modifiers))
    if explicit is not Visibility.UNKNOWN:
        return explicit
    if name.startswith("#"):
        return Visibility.PRIVATE
    return Visibility.PUBLIC


def _parameter_names(parameters: list[str]) -> list[str]:
    names = []
    for parameter in parameters:
        cleaned = re.sub(r"^\s*(?:public|private|protected|readonly)\s+", "", parameter)
        names.append(cleaned.split(":")[0].split("=")[0].strip() or "?")
    return names


def _parameter_type(parameter: str) -> str:
    """TypeScript annotated type of a parameter, or ``""`` when untyped."""
    if ":" not in parameter:
        return ""
    annotation = parameter.split(":", 1)[1].split("=")[0]
    return simple_name(strip_generics(annotation.strip().split("|")[0].strip()))


class _JsState:
    """Mutable accumulator threaded through one file's parse."""

    def __init__(
        self, *, file_path: str, source: str, masked: str, lines: LineIndex, language: str
    ) -> None:
        self.file_path = file_path
        self.source = source
        self.masked = masked
        self.lines = lines
        self.language = language
        self.package = ""
        self.symbols: list[SymbolInfo] = []
        self.relationships: list[RelationshipInfo] = []
        self.imports: list[str] = []
        self.call_count = 0

    def add_symbol(self, **kwargs: object) -> None:
        self.symbols.append(
            SymbolInfo(
                file_path=self.file_path,
                language=self.language,
                package=self.package,
                detection_method=DetectionMethod.LEXICAL_PARSE,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    def add_relationship(self, **kwargs: object) -> None:
        self.relationships.append(
            RelationshipInfo(
                file_path=self.file_path,
                language=self.language,
                detection_method=DetectionMethod.LEXICAL_PARSE,
                **kwargs,  # type: ignore[arg-type]
            )
        )


__all__ = ["JsTsAnalyzer", "JavaScriptAnalyzer", "TypeScriptAnalyzer", "module_path"]
