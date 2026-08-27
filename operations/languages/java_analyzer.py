"""Java analyzer.

Java states its OOP relationships explicitly — ``extends``, ``implements``,
``abstract``, ``@Override`` — so those are read directly off the declaration
and reported with :attr:`~operations.schemas.Confidence.HIGH`. Anything the
language leaves implicit (a field type modelling composition, a constructor
parameter modelling injection) stays ``MEDIUM``.

The parse is lexical: comments and string/text-block literals are masked, then
the class body is walked segment by segment (a segment ends at a top-level
``;`` or at a balanced ``{...}`` block), which handles nested types, generic
signatures and annotations without a full Java grammar.
"""

from __future__ import annotations

import re

from operations.languages.base import (
    IDENTIFIER,
    JAVA_SYNTAX,
    LanguageAnalyzer,
    LineIndex,
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

_PACKAGE_RE = re.compile(rf"^[ \t]*package\s+([\w\.]+)\s*;", re.M)
_IMPORT_RE = re.compile(r"^[ \t]*import\s+(?:static\s+)?([\w\.\*]+)\s*;", re.M)

_MODIFIERS = (
    "public", "protected", "private", "static", "final", "abstract",
    "synchronized", "native", "transient", "volatile", "strictfp",
    "default", "sealed", "non-sealed",
)
_MODIFIER_ALTERNATION = "|".join(re.escape(modifier) for modifier in _MODIFIERS)

_TYPE_DECL_RE = re.compile(
    rf"(?P<modifiers>(?:(?:{_MODIFIER_ALTERNATION})\s+)*)"
    rf"(?P<kind>class|interface|enum|record|@interface)\s+(?P<name>{IDENTIFIER})"
)
_ANNOTATION_RE = re.compile(rf"@({IDENTIFIER})")

#: Annotations that mark a member as injected by a DI container.
_INJECTION_ANNOTATIONS = frozenset({"Autowired", "Inject", "Resource", "Value"})

#: Types that carry no design information as composition targets.
_JAVA_PRIMITIVES = frozenset(
    {
        "byte", "short", "int", "long", "float", "double", "boolean", "char",
        "void", "var", "String", "Object", "Integer", "Long", "Double",
        "Boolean", "Character", "Byte", "Short", "Float", "Number",
    }
)

_JAVA_NON_CALLS = frozenset(
    {
        "if", "for", "while", "switch", "catch", "return", "new", "super",
        "this", "synchronized", "try", "do", "else", "assert", "throw",
    }
)

_CALL_RE = re.compile(rf"(?:(?:{IDENTIFIER})\s*\.\s*)?({IDENTIFIER})\s*\(")
_MAX_CALLS_PER_FILE = 400


class JavaAnalyzer(LanguageAnalyzer):
    """Extract Java types, members and relationships."""

    language = "java"
    extensions = (".java",)
    detection_method = DetectionMethod.LEXICAL_PARSE
    base_confidence = Confidence.HIGH

    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        masked = mask_code(source, JAVA_SYNTAX)
        state = _JavaState(
            file_path=file_path, source=source, masked=masked, lines=LineIndex(source)
        )
        package_match = _PACKAGE_RE.search(masked)
        state.package = package_match.group(1) if package_match else ""

        for match in _IMPORT_RE.finditer(masked):
            state.imports.append(match.group(1))
            state.add_relationship(
                source=state.package or file_path,
                target=match.group(1),
                relation=RelationType.IMPORTS,
                line=state.lines.line_of(match.start()),
                confidence=Confidence.HIGH,
                metadata={"kind": "import"},
            )

        self._parse_scope(state, start=0, end=len(masked), owner=None, owner_is_interface=False)
        state.symbols.sort(key=lambda symbol: (symbol.line, symbol.name))

        return FileAnalysis(
            file_path=file_path,
            language=self.language,
            package=state.package,
            symbols=state.symbols,
            relationships=state.relationships,
            imports=sorted(set(state.imports)),
        )

    # -- scope walking -----------------------------------------------------

    def _parse_scope(
        self,
        state: _JavaState,
        *,
        start: int,
        end: int,
        owner: str | None,
        owner_is_interface: bool,
    ) -> None:
        """Parse declarations directly inside ``[start, end)`` of the masked text."""
        for header_start, header_end, block in iter_declaration_segments(state.masked, start, end):
            header = state.masked[header_start:header_end]
            if not header.strip():
                continue
            type_match = _TYPE_DECL_RE.search(header)
            if type_match is not None:
                self._handle_type(state, header_start, header, type_match, block)
            elif owner is not None:
                self._handle_member(
                    state, header_start, header, block, owner, owner_is_interface
                )

    def _handle_type(
        self,
        state: _JavaState,
        header_start: int,
        header: str,
        match: re.Match[str],
        block: tuple[int, int] | None,
    ) -> None:
        name = match.group("name")
        kind = match.group("kind")
        modifiers = match.group("modifiers") or ""
        annotations = sorted(set(_ANNOTATION_RE.findall(header[: match.start()])))
        line = state.lines.line_of(header_start + match.start())
        end_line = state.lines.line_of(block[1] - 1) if block else line

        symbol_type = {
            "class": SymbolType.CLASS,
            "interface": SymbolType.INTERFACE,
            "@interface": SymbolType.INTERFACE,
            "enum": SymbolType.ENUM,
            "record": SymbolType.RECORD,
        }[kind]
        is_abstract = "abstract" in modifiers or kind in ("interface", "@interface")
        if symbol_type is SymbolType.CLASS and is_abstract:
            symbol_type = SymbolType.ABSTRACT_CLASS

        state.add_symbol(
            name=name,
            symbol_type=symbol_type,
            line=line,
            end_line=end_line,
            visibility=modifier_visibility(modifiers) or Visibility.PACKAGE,
            is_abstract=is_abstract,
            is_static="static" in modifiers,
            signature=f"{kind} {name}".strip(),
            confidence=Confidence.HIGH,
            metadata={"annotations": annotations, "modifiers": modifiers.split()},
        )

        tail = header[match.end() :]
        for target in _clause_targets(tail, "extends"):
            # `interface X extends Y` is interface inheritance, not implementation.
            state.add_relationship(
                source=name,
                target=target,
                relation=RelationType.INHERITS,
                line=line,
                confidence=Confidence.HIGH,
                metadata={"kind": "extends"},
            )
        for target in _clause_targets(tail, "implements"):
            state.add_relationship(
                source=name,
                target=target,
                relation=RelationType.IMPLEMENTS,
                line=line,
                confidence=Confidence.HIGH,
                metadata={"kind": "implements"},
            )

        if block is not None:
            self._parse_scope(
                state,
                start=block[0] + 1,
                end=block[1] - 1,
                owner=name,
                owner_is_interface=symbol_type is SymbolType.INTERFACE,
            )

    def _handle_member(
        self,
        state: _JavaState,
        header_start: int,
        header: str,
        block: tuple[int, int] | None,
        owner: str,
        owner_is_interface: bool,
    ) -> None:
        annotations = sorted(set(_ANNOTATION_RE.findall(header)))
        paren = first_top_level_paren(header)
        if paren is not None:
            self._handle_method(
                state,
                header_start,
                header,
                paren,
                block,
                owner,
                annotations,
                owner_is_interface,
            )
            return
        self._handle_field(state, header_start, header, owner, annotations)

    def _handle_method(
        self,
        state: _JavaState,
        header_start: int,
        header: str,
        paren: tuple[int, int],
        block: tuple[int, int] | None,
        owner: str,
        annotations: list[str],
        owner_is_interface: bool,
    ) -> None:
        open_index, close_index = paren
        name_match = re.search(rf"({IDENTIFIER})\s*$", header[:open_index])
        if name_match is None:
            return
        name = name_match.group(1)
        prefix = header[: name_match.start()]
        modifiers = " ".join(
            token for token in re.findall(IDENTIFIER, prefix) if token in _MODIFIERS
        )
        return_type = _return_type(prefix)
        parameters = split_top_level(header[open_index + 1 : close_index])
        line = state.lines.line_of(header_start + name_match.start())
        end_line = state.lines.line_of(block[1] - 1) if block else line

        is_constructor = name == owner and not return_type
        symbol_type = SymbolType.CONSTRUCTOR if is_constructor else SymbolType.METHOD
        visibility = modifier_visibility(modifiers)
        if visibility is Visibility.UNKNOWN:
            # Interface members are implicitly public; class members default to
            # package-private.
            visibility = Visibility.PUBLIC if owner_is_interface else Visibility.PACKAGE
        is_abstract = "abstract" in modifiers or block is None

        state.add_symbol(
            name=name,
            symbol_type=symbol_type,
            line=line,
            end_line=end_line,
            owner=owner,
            visibility=visibility,
            is_abstract=is_abstract,
            is_static="static" in modifiers,
            signature=f"{name}({','.join(_parameter_types(parameters))})",
            confidence=Confidence.HIGH,
            metadata={
                "annotations": annotations,
                "return_type": return_type,
                "is_override": "Override" in annotations,
            },
        )

        injected = bool(set(annotations) & _INJECTION_ANNOTATIONS)
        if is_constructor or injected:
            for parameter in parameters:
                dependency = _parameter_type(parameter)
                if dependency and dependency not in _JAVA_PRIMITIVES:
                    state.add_relationship(
                        source=owner,
                        target=dependency,
                        relation=RelationType.USES,
                        line=line,
                        confidence=Confidence.MEDIUM,
                        metadata={
                            "kind": "dependency_injection",
                            "via": "annotation" if injected else "constructor",
                            "annotations": annotations,
                        },
                    )

        if block is not None:
            self._parse_calls(state, f"{owner}.{name}", block)

    def _handle_field(
        self, state: _JavaState, header_start: int, header: str, owner: str, annotations: list[str]
    ) -> None:
        cleaned = _ANNOTATION_RE.sub(" ", header).strip()
        cleaned = cleaned.split("=", 1)[0].strip()
        match = re.search(
            rf"(?P<type>[\w\.\<\>\[\],\s\?]+?)\s+(?P<name>{IDENTIFIER})\s*$", cleaned
        )
        if match is None:
            return
        field_type = strip_generics(match.group("type").split()[-1])
        name = match.group("name")
        if name in _MODIFIERS or field_type in _MODIFIERS:
            return
        modifiers = " ".join(
            token for token in re.findall(IDENTIFIER, cleaned) if token in _MODIFIERS
        )
        line = state.lines.line_of(header_start + match.start("name"))
        visibility = modifier_visibility(modifiers)
        state.add_symbol(
            name=name,
            symbol_type=SymbolType.FIELD,
            line=line,
            owner=owner,
            visibility=Visibility.PACKAGE if visibility is Visibility.UNKNOWN else visibility,
            is_static="static" in modifiers,
            signature=f"{field_type} {name}",
            confidence=Confidence.HIGH,
            metadata={"annotations": annotations, "field_type": field_type},
        )
        target = simple_name(field_type)
        if target and target not in _JAVA_PRIMITIVES:
            injected = bool(set(annotations) & _INJECTION_ANNOTATIONS)
            state.add_relationship(
                source=owner,
                target=target,
                relation=RelationType.USES if injected else RelationType.CONTAINS,
                line=line,
                confidence=Confidence.MEDIUM,
                metadata={
                    "kind": "dependency_injection" if injected else "composition",
                    "field": name,
                },
            )

    def _parse_calls(self, state: _JavaState, scope: str, block: tuple[int, int]) -> None:
        if state.call_count >= _MAX_CALLS_PER_FILE:
            return
        body = state.masked[block[0] : block[1]]
        for match in _CALL_RE.finditer(body):
            callee = match.group(1)
            if callee in _JAVA_NON_CALLS:
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


# --------------------------------------------------------------------------
# Declaration-header helpers
# --------------------------------------------------------------------------


def _clause_targets(tail: str, keyword: str) -> list[str]:
    """Type names listed after ``extends``/``implements`` in a declaration."""
    match = re.search(rf"\b{keyword}\b(?P<names>[^{{]*)", tail)
    if match is None:
        return []
    names = match.group("names")
    for terminator in ("extends", "implements", "permits"):
        if terminator != keyword:
            names = re.split(rf"\b{terminator}\b", names)[0]
    return [
        simple_name(entry)
        for entry in split_top_level(names)
        if entry and simple_name(entry)
    ]


def _return_type(prefix: str) -> str:
    """Return type from a method header prefix (``""`` for constructors)."""
    tokens = [token for token in re.findall(r"[\w\.\<\>\[\]]+", prefix)]
    tokens = [token for token in tokens if token not in _MODIFIERS]
    return tokens[-1] if tokens else ""


def _parameter_type(parameter: str) -> str:
    """Declared type of one parameter (``final Foo bar`` → ``Foo``)."""
    tokens = [token for token in parameter.replace("...", " ").split() if token]
    tokens = [token for token in tokens if token not in ("final",) and not token.startswith("@")]
    if len(tokens) < 2:
        return ""
    return simple_name(strip_generics(tokens[-2]))


def _parameter_types(parameters: list[str]) -> list[str]:
    return [_parameter_type(parameter) or "?" for parameter in parameters]


class _JavaState:
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

    def add_symbol(self, **kwargs: object) -> None:
        self.symbols.append(
            SymbolInfo(
                file_path=self.file_path,
                language="java",
                package=self.package,
                detection_method=DetectionMethod.LEXICAL_PARSE,
                **kwargs,  # type: ignore[arg-type]
            )
        )

    def add_relationship(self, **kwargs: object) -> None:
        self.relationships.append(
            RelationshipInfo(
                file_path=self.file_path,
                language="java",
                detection_method=DetectionMethod.LEXICAL_PARSE,
                **kwargs,  # type: ignore[arg-type]
            )
        )


__all__ = ["JavaAnalyzer"]
