"""Shared plumbing for language analyzers.

Two things live here so no analyzer re-implements them:

* :class:`LanguageAnalyzer` — the interface every analyzer implements, and the
  registry contract used by :mod:`operations.oop_analyzer`.
* Lexical utilities — comment/string masking, brace matching, offset→line
  lookup. Masking is what makes the brace-based parsers dependable: a ``{`` in
  a string literal or a ``class`` keyword inside a comment can no longer be
  mistaken for code.

The masking pass preserves byte offsets and newlines exactly, so a position
found in the masked text maps back to the original source without adjustment.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from bisect import bisect_right
from dataclasses import dataclass

from operations.schemas import (
    Confidence,
    DetectionMethod,
    FileAnalysis,
    Visibility,
)

IDENTIFIER = r"[A-Za-z_$][A-Za-z0-9_$]*"
DOTTED_IDENTIFIER = rf"{IDENTIFIER}(?:\.{IDENTIFIER})*"


# --------------------------------------------------------------------------
# Analyzer interface
# --------------------------------------------------------------------------


class LanguageAnalyzer(ABC):
    """Extracts symbols and relationships from a single source file.

    Implementations must be pure and side-effect free: given the same source
    text they always produce the same :class:`FileAnalysis`. They never touch
    the filesystem, run commands, or call an LLM — which is what makes them
    independently unit-testable and safe to run over untrusted repositories.

    To add a language, subclass this, register it in
    :mod:`operations.languages`, and add fixtures + tests. See
    ``docs/ARCHITECTURE.md`` §"Adding a language analyzer".
    """

    #: Canonical language name, matching :data:`operations.inventory.EXTENSION_LANGUAGE`.
    language: str = ""
    #: File extensions this analyzer claims.
    extensions: tuple[str, ...] = ()
    #: How findings from this analyzer were produced.
    detection_method: DetectionMethod = DetectionMethod.LEXICAL_PARSE
    #: Baseline confidence for declarations this analyzer reports.
    base_confidence: Confidence = Confidence.MEDIUM

    @abstractmethod
    def analyze(self, file_path: str, source: str) -> FileAnalysis:
        """Analyze ``source`` (already read) from repository path ``file_path``."""

    def empty(self, file_path: str, error: str | None = None) -> FileAnalysis:
        """Build an empty analysis, optionally carrying a parse error."""
        return FileAnalysis(
            file_path=file_path,
            language=self.language,
            errors=[error] if error else [],
        )


# --------------------------------------------------------------------------
# Lexical helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CommentSyntax:
    """Comment/string delimiters for a curly-brace language."""

    line_comments: tuple[str, ...] = ("//",)
    block_comment: tuple[str, str] | None = ("/*", "*/")
    quotes: tuple[str, ...] = ('"', "'")
    raw_quotes: tuple[str, ...] = ()
    """Quotes without backslash escaping (Go raw strings, JS templates)."""
    triple_quotes: tuple[str, ...] = ()
    """Multi-character string delimiters (Java text blocks)."""


C_LIKE = CommentSyntax()
GO_SYNTAX = CommentSyntax(quotes=('"', "'"), raw_quotes=("`",))
JS_SYNTAX = CommentSyntax(quotes=('"', "'"), raw_quotes=("`",))
JAVA_SYNTAX = CommentSyntax(quotes=('"', "'"), triple_quotes=('"""',))


def mask_code(source: str, syntax: CommentSyntax = C_LIKE) -> str:
    """Blank out comments and string literals, preserving offsets and newlines.

    Returns a string of exactly the same length as ``source`` in which every
    character inside a comment or string literal (delimiters included) has been
    replaced by a space. Newlines are kept so line numbers still line up.
    """
    out = list(source)
    length = len(source)
    index = 0

    def blank(start: int, stop: int) -> None:
        for position in range(start, min(stop, length)):
            if out[position] != "\n":
                out[position] = " "

    while index < length:
        char = source[index]

        line_comment = next(
            (marker for marker in syntax.line_comments if source.startswith(marker, index)),
            None,
        )
        if line_comment is not None:
            end = source.find("\n", index)
            end = length if end == -1 else end
            blank(index, end)
            index = end
            continue

        if syntax.block_comment and source.startswith(syntax.block_comment[0], index):
            opener, closer = syntax.block_comment
            end = source.find(closer, index + len(opener))
            end = length if end == -1 else end + len(closer)
            blank(index, end)
            index = end
            continue

        triple = next(
            (marker for marker in syntax.triple_quotes if source.startswith(marker, index)),
            None,
        )
        if triple is not None:
            end = source.find(triple, index + len(triple))
            end = length if end == -1 else end + len(triple)
            blank(index, end)
            index = end
            continue

        if char in syntax.raw_quotes:
            end = source.find(char, index + 1)
            end = length if end == -1 else end + 1
            blank(index, end)
            index = end
            continue

        if char in syntax.quotes:
            cursor = index + 1
            while cursor < length:
                if source[cursor] == "\\":
                    cursor += 2
                    continue
                if source[cursor] == char or source[cursor] == "\n":
                    cursor += 1
                    break
                cursor += 1
            blank(index, cursor)
            index = cursor
            continue

        index += 1

    return "".join(out)


class LineIndex:
    """Offset → 1-based line number lookups over one source string."""

    def __init__(self, source: str) -> None:
        self._starts = [0]
        for position, char in enumerate(source):
            if char == "\n":
                self._starts.append(position + 1)
        self._total = len(self._starts)

    def line_of(self, offset: int) -> int:
        """1-based line number containing ``offset``."""
        return bisect_right(self._starts, max(0, offset))

    @property
    def line_count(self) -> int:
        return self._total


def find_matching(masked: str, open_index: int, opener: str = "{", closer: str = "}") -> int:
    """Index just past the delimiter matching the one at ``open_index``.

    ``masked`` must already have comments/strings blanked (see :func:`mask_code`)
    so nested delimiters inside literals do not skew the count. Returns
    ``len(masked)`` when the block is unterminated.
    """
    depth = 0
    for position in range(open_index, len(masked)):
        char = masked[position]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return position + 1
    return len(masked)


def iter_declaration_segments(
    masked: str, start: int, end: int
) -> list[tuple[int, int, tuple[int, int] | None]]:
    """Split a scope into ``(header_start, header_end, block)`` declarations.

    A declaration header runs until either a top-level ``;`` (field, abstract
    method) or a balanced ``{...}`` block (type body, method body). Parenthesised
    runs are skipped wholesale so a ``;`` inside a ``for`` header or a generic
    ``<...>`` never splits a declaration.
    """
    segments: list[tuple[int, int, tuple[int, int] | None]] = []
    index = start
    header_start = start
    while index < end:
        char = masked[index]
        if char == "(":
            index = min(find_matching(masked, index, "(", ")"), end)
            continue
        if char == "{":
            block_end = min(find_matching(masked, index), end)
            segments.append((header_start, index, (index, block_end)))
            index = block_end
            header_start = index
            continue
        if char == "}":
            # Unbalanced closer for this scope: stop here.
            break
        if char == ";":
            segments.append((header_start, index, None))
            index += 1
            header_start = index
            continue
        index += 1
    if header_start < end and masked[header_start:end].strip():
        segments.append((header_start, end, None))
    return segments


def first_top_level_paren(header: str) -> tuple[int, int] | None:
    """Index pair of the first balanced ``(...)`` in a member header."""
    for position, char in enumerate(header):
        if char == "(":
            return position, find_matching(header, position, "(", ")") - 1
    return None


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split on ``separator`` ignoring separators nested in brackets/generics.

    Used for parameter lists and ``implements A, Map<K, V>`` clauses, where a
    naive ``str.split`` would cut a generic argument list in half.
    """
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{<":
            depth += 1
        elif char in ")]}>":
            depth = max(0, depth - 1)
        if char == separator and depth == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]


def strip_generics(name: str) -> str:
    """``List<Foo>`` → ``List``; ``*pkg.Type`` → ``pkg.Type``."""
    cleaned = name.strip().lstrip("*&")
    cut = cleaned.find("<")
    if cut != -1:
        cleaned = cleaned[:cut]
    cut = cleaned.find("[")
    if cut != -1 and not cleaned.startswith("["):
        cleaned = cleaned[:cut]
    return cleaned.strip()


def simple_name(dotted: str) -> str:
    """Last segment of a dotted/qualified name (``a.b.C`` → ``C``)."""
    return strip_generics(dotted).rsplit(".", 1)[-1]


def python_visibility(name: str) -> Visibility:
    """PEP 8 visibility convention: ``__x`` private, ``_x`` protected."""
    if name.startswith("__") and not name.endswith("__"):
        return Visibility.PRIVATE
    if name.startswith("_"):
        return Visibility.PROTECTED
    return Visibility.PUBLIC


def go_visibility(name: str) -> Visibility:
    """Go exports by capitalisation; unexported is package-scoped."""
    return Visibility.PUBLIC if name[:1].isupper() else Visibility.PACKAGE


def modifier_visibility(modifiers: str) -> Visibility:
    """Visibility from explicit Java/TypeScript modifier keywords."""
    tokens = set(re.findall(IDENTIFIER, modifiers))
    if "private" in tokens:
        return Visibility.PRIVATE
    if "protected" in tokens:
        return Visibility.PROTECTED
    if "public" in tokens:
        return Visibility.PUBLIC
    return Visibility.UNKNOWN
