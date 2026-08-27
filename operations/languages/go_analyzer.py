"""Go analyzer.

Go has no ``implements`` keyword: a type satisfies an interface purely by
having the right method set. That makes text search useless for interface
implementation and makes *structural comparison* the only correct approach —
which is done in :mod:`operations.oop_analyzer` once every file's method sets
are known. This module's job is to produce those method sets accurately.

Parsing is lexical (comment/string-masked, brace-matched) rather than
tree-sitter based, so that analysis works on a stock Python install. Findings
are therefore reported as :attr:`~operations.schemas.Confidence.MEDIUM`:
declarations are read from real syntax, but this is not a full Go grammar and
exotic constructs (generic instantiation in odd positions, deeply nested
anonymous structs) may be missed rather than mis-parsed.
"""

from __future__ import annotations

import re

from operations.languages.go_common import element_type, normalize_signature
from operations.languages.base import (
    GO_SYNTAX,
    IDENTIFIER,
    LanguageAnalyzer,
    LineIndex,
    find_matching,
    go_visibility,
    mask_code,
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
)

_PACKAGE_RE = re.compile(rf"^[ \t]*package\s+({IDENTIFIER})", re.M)
_IMPORT_BLOCK_RE = re.compile(r"^[ \t]*import[ \t]*\(", re.M)
_IMPORT_SINGLE_RE = re.compile(rf'^[ \t]*import\s+(?:{IDENTIFIER}\s+|\.\s+|_\s+)?"([^"\n]+)"', re.M)
_TYPE_DECL_RE = re.compile(
    rf"^[ \t]*type[ \t]+({IDENTIFIER})[ \t]*(?:\[[^\]]*\])?[ \t]*(struct|interface)[ \t]*\{{", re.M
)
_TYPE_ALIAS_RE = re.compile(rf"^[ \t]*type[ \t]+({IDENTIFIER})[ \t]*=?[ \t]*([\w\.\[\]\*]+)[ \t]*$", re.M)
_FUNC_RE = re.compile(
    rf"^func\s*(?:\(\s*(?:({IDENTIFIER})\s+)?(\*?[\w\.]+)(?:\[[^\]]*\])?\s*\)\s*)?"
    rf"({IDENTIFIER})\s*(?:\[[^\]]*\])?\s*\(",
    re.M,
)

#: Identifiers that look like calls but are keywords, builtins or conversions.
_GO_NON_CALLS = frozenset(
    {
        "if", "for", "switch", "select", "return", "func", "go", "defer",
        "range", "case", "chan", "map", "struct", "interface", "type", "var",
        "const", "package", "import", "else",
        "make", "new", "len", "cap", "append", "copy", "delete", "panic",
        "recover", "print", "println", "close", "complex", "real", "imag",
        "string", "int", "int8", "int16", "int32", "int64", "uint", "uint8",
        "uint16", "uint32", "uint64", "float32", "float64", "bool", "byte",
        "rune", "error", "uintptr", "any",
    }
)

_CALL_RE = re.compile(rf"(?:(?:{IDENTIFIER})\s*\.\s*)?({IDENTIFIER})\s*\(")
_MAX_CALLS_PER_FILE = 400


def parse_parameter_types(parameter_list: str) -> list[str]:
    """Extract parameter *types* from a Go parameter list.

    Handles both ``(a, b int, c string)`` (grouped names) and the type-only
    form ``(int, string)``: if no part carries an explicit ``name type`` pair
    then every part is itself a type.
    """
    parts = split_top_level(parameter_list)
    if not parts:
        return []
    has_named = any(_split_named_parameter(part) is not None for part in parts)
    if not has_named:
        return [strip_generics_keep_slice(part) for part in parts]

    types: list[str] = []
    pending = 0
    for part in parts:
        named = _split_named_parameter(part)
        if named is None:
            pending += 1  # bare name sharing a later part's type
            continue
        _, type_text = named
        normalized = strip_generics_keep_slice(type_text)
        types.extend([normalized] * (pending + 1))
        pending = 0
    types.extend(["?"] * pending)
    return types


def _split_named_parameter(part: str) -> tuple[str, str] | None:
    """``"p []byte"`` → ``("p", "[]byte")``; ``"[]byte"`` → ``None``."""
    match = re.match(rf"^({IDENTIFIER})\s+(\S.*)$", part.strip())
    if match is None:
        return None
    return match.group(1), match.group(2).strip()


def strip_generics_keep_slice(type_text: str) -> str:
    """Normalize a type for comparison, keeping ``[]``/``*``/``map`` structure."""
    return re.sub(r"\s+", " ", type_text.strip())


def parse_results(text: str) -> list[str]:
    """Result types from the text following a Go parameter list."""
    tail = text.strip().rstrip("{").strip()
    if not tail:
        return []
    if tail.startswith("("):
        inner = tail[1 : tail.rfind(")")] if ")" in tail else tail[1:]
        return parse_parameter_types(inner)
    return [strip_generics_keep_slice(tail)]


class GoAnalyzer(LanguageAnalyzer):
    """Extract Go packages, structs, interfaces, methods and relationships."""

    language = "go"
    extensions = (".go",)
    detection_method = DetectionMethod.LEXICAL_PARSE
    base_confidence = Confidence.MEDIUM

    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        masked = mask_code(source, GO_SYNTAX)
        lines = LineIndex(source)
        state = _GoState(file_path=file_path, source=source, masked=masked, lines=lines)

        package_match = _PACKAGE_RE.search(masked)
        state.package = package_match.group(1) if package_match else ""

        self._parse_imports(state)
        self._parse_types(state)
        self._parse_functions(state)
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

    def _parse_imports(self, state: _GoState) -> None:
        for match in _IMPORT_SINGLE_RE.finditer(state.masked):
            state.add_import(match.group(1), state.lines.line_of(match.start()))
        for match in _IMPORT_BLOCK_RE.finditer(state.masked):
            open_index = state.masked.index("(", match.start())
            end = find_matching(state.masked, open_index, "(", ")")
            block = state.source[open_index + 1 : max(open_index + 1, end - 1)]
            offset = open_index + 1
            for raw_line in block.splitlines(keepends=True):
                path_match = re.search(r'"([^"\n]+)"', raw_line)
                if path_match:
                    state.add_import(
                        path_match.group(1),
                        state.lines.line_of(offset + path_match.start()),
                    )
                offset += len(raw_line)

    # -- type declarations -------------------------------------------------

    def _parse_types(self, state: _GoState) -> None:
        for match in _TYPE_DECL_RE.finditer(state.masked):
            name, kind = match.group(1), match.group(2)
            open_index = state.masked.index("{", match.end() - 1)
            end = find_matching(state.masked, open_index)
            body = state.masked[open_index + 1 : max(open_index + 1, end - 1)]
            body_offset = open_index + 1
            line = state.lines.line_of(match.start())
            end_line = state.lines.line_of(max(open_index, end - 1))

            state.add_symbol(
                name=name,
                symbol_type=SymbolType.STRUCT if kind == "struct" else SymbolType.INTERFACE,
                line=line,
                end_line=end_line,
                visibility=go_visibility(name),
                is_abstract=kind == "interface",
                signature=f"type {name} {kind}",
            )
            if kind == "struct":
                self._parse_struct_body(state, name, body, body_offset)
            else:
                self._parse_interface_body(state, name, body, body_offset)

        for match in _TYPE_ALIAS_RE.finditer(state.masked):
            name, target = match.group(1), match.group(2)
            if target in ("struct", "interface"):
                continue
            state.add_symbol(
                name=name,
                symbol_type=SymbolType.TYPE_ALIAS,
                line=state.lines.line_of(match.start()),
                visibility=go_visibility(name),
                signature=f"type {name} {target}",
            )

    def _parse_struct_body(
        self, state: _GoState, struct_name: str, body: str, body_offset: int
    ) -> None:
        offset = body_offset
        for raw_line in body.splitlines(keepends=True):
            line_text = raw_line.split("//", 1)[0].strip()
            line_number = state.lines.line_of(offset)
            offset += len(raw_line)
            if not line_text or line_text in ("{", "}"):
                continue
            embedded = re.fullmatch(r"(\*?[\w\.]+)(?:\[[^\]]*\])?", line_text)
            if embedded:
                # A field with a type but no name: Go struct embedding, the
                # closest thing the language has to inheritance.
                target = strip_generics(embedded.group(1))
                state.add_symbol(
                    name=target.rsplit(".", 1)[-1],
                    symbol_type=SymbolType.FIELD,
                    line=line_number,
                    owner=struct_name,
                    visibility=go_visibility(target.rsplit(".", 1)[-1]),
                    signature=line_text,
                )
                state.add_relationship(
                    source=struct_name,
                    target=target,
                    relation=RelationType.INHERITS,
                    line=line_number,
                    confidence=Confidence.MEDIUM,
                    metadata={
                        "kind": "struct_embedding",
                        "note": "Go embedding promotes the embedded type's "
                        "methods; it is not classical inheritance",
                    },
                )
                continue
            named = re.match(rf"^((?:{IDENTIFIER})(?:\s*,\s*{IDENTIFIER})*)\s+(\S.*)$", line_text)
            if not named:
                continue
            field_type = strip_generics_keep_slice(named.group(2))
            for field_name in split_top_level(named.group(1)):
                state.add_symbol(
                    name=field_name,
                    symbol_type=SymbolType.FIELD,
                    line=line_number,
                    owner=struct_name,
                    visibility=go_visibility(field_name),
                    signature=f"{field_name} {field_type}",
                )
                bare = element_type(field_type)
                if bare:
                    state.add_relationship(
                        source=struct_name,
                        target=bare,
                        relation=RelationType.CONTAINS,
                        line=line_number,
                        confidence=Confidence.MEDIUM,
                        metadata={"kind": "composition", "field": field_name},
                    )

    def _parse_interface_body(
        self, state: _GoState, interface_name: str, body: str, body_offset: int
    ) -> None:
        offset = body_offset
        for raw_line in body.splitlines(keepends=True):
            line_text = raw_line.strip()
            line_number = state.lines.line_of(offset)
            offset += len(raw_line)
            if not line_text or line_text in ("{", "}"):
                continue
            method = re.match(rf"^({IDENTIFIER})\s*\((.*)$", line_text)
            if method:
                name = method.group(1)
                remainder = method.group(2)
                params, tail = _split_parameters(remainder)
                signature = normalize_signature(
                    name, parse_parameter_types(params), parse_results(tail)
                )
                state.add_symbol(
                    name=name,
                    symbol_type=SymbolType.METHOD,
                    line=line_number,
                    owner=interface_name,
                    visibility=go_visibility(name),
                    is_abstract=True,
                    signature=signature,
                )
                continue
            embedded = re.fullmatch(r"[\w\.]+", line_text)
            if embedded:
                state.add_relationship(
                    source=interface_name,
                    target=strip_generics(line_text),
                    relation=RelationType.INHERITS,
                    line=line_number,
                    confidence=Confidence.MEDIUM,
                    metadata={"kind": "interface_embedding"},
                )

    # -- functions and methods --------------------------------------------

    def _parse_functions(self, state: _GoState) -> None:
        for match in _FUNC_RE.finditer(state.masked):
            receiver_type = match.group(2)
            name = match.group(3)
            owner = strip_generics(receiver_type).lstrip("*") if receiver_type else None
            params, tail = _split_parameters(state.masked[match.end() :])
            line = state.lines.line_of(match.start())

            body_start = state.masked.find("{", match.end() + len(params) + 1)
            end_line = line
            body = ""
            if body_start != -1:
                body_end = find_matching(state.masked, body_start)
                end_line = state.lines.line_of(max(body_start, body_end - 1))
                body = state.masked[body_start:body_end]

            parameter_types = parse_parameter_types(params)
            results = parse_results(tail.split("{", 1)[0])
            signature = normalize_signature(name, parameter_types, results)

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
                line=line,
                end_line=end_line,
                owner=owner,
                visibility=go_visibility(name),
                signature=signature,
            )

            if symbol_type is SymbolType.CONSTRUCTOR:
                constructed = strip_generics(results[0].lstrip("*")) if results else ""
                if constructed:
                    state.add_relationship(
                        source=name,
                        target=constructed,
                        relation=RelationType.CONTAINS,
                        line=line,
                        confidence=Confidence.MEDIUM,
                        metadata={"kind": "constructor_function"},
                    )
                for parameter in split_top_level(params):
                    named = _split_named_parameter(parameter)
                    if named is None:
                        continue
                    dependency = element_type(named[1])
                    if dependency is None:
                        continue
                    state.add_relationship(
                        source=constructed or name,
                        target=dependency,
                        relation=RelationType.USES,
                        line=line,
                        confidence=Confidence.MEDIUM,
                        metadata={
                            "kind": "dependency_injection",
                            "parameter": named[0],
                        },
                    )

            qualified = f"{owner}.{name}" if owner else name
            self._parse_calls(state, qualified, body, body_start)

    def _parse_calls(
        self, state: _GoState, scope: str, body: str, body_offset: int
    ) -> None:
        if not body or state.call_count >= _MAX_CALLS_PER_FILE:
            return
        for match in _CALL_RE.finditer(body):
            callee = match.group(1)
            if callee in _GO_NON_CALLS:
                continue
            state.call_count += 1
            if state.call_count > _MAX_CALLS_PER_FILE:
                return
            state.add_relationship(
                source=scope,
                target=callee,
                relation=RelationType.CALLS,
                line=state.lines.line_of(body_offset + match.start()),
                confidence=Confidence.MEDIUM,
                metadata={"kind": "call"},
            )


def _split_parameters(text: str) -> tuple[str, str]:
    """Split ``"a int) (err error) {"`` into ``("a int", " (err error) {")``.

    ``text`` starts immediately after the opening parenthesis of a parameter
    list; nesting is tracked so a function-typed parameter does not terminate
    the scan early.
    """
    depth = 1
    for position, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[:position], text[position + 1 :]
    return text, ""


class _GoState:
    """Mutable accumulator threaded through one file's parse."""

    def __init__(self, *, file_path: str, source: str, masked: str, lines: LineIndex) -> None:
        self.file_path = file_path
        self.source = source
        self.masked = masked
        self.lines = lines
        self.package = ""
        self.symbols: list[SymbolInfo] = []
        self.relationships: list[RelationshipInfo] = []
        self.imports: list[str] = []
        self.call_count = 0

    def add_import(self, path: str, line: int) -> None:
        self.imports.append(path)
        self.add_relationship(
            source=self.package or self.file_path,
            target=path,
            relation=RelationType.IMPORTS,
            line=line,
            confidence=Confidence.HIGH,
            metadata={"kind": "import"},
        )

    def add_symbol(self, **kwargs: object) -> None:
        self.symbols.append(
            SymbolInfo(
                file_path=self.file_path,
                language="go",
                package=self.package,
                detection_method=DetectionMethod.LEXICAL_PARSE,
                confidence=Confidence.MEDIUM,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    def add_relationship(self, **kwargs: object) -> None:
        self.relationships.append(
            RelationshipInfo(
                file_path=self.file_path,
                language="go",
                detection_method=DetectionMethod.LEXICAL_PARSE,
                **kwargs,  # type: ignore[arg-type]
            )
        )


__all__ = ["GoAnalyzer", "parse_parameter_types", "parse_results"]
