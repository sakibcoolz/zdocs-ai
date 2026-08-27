"""Go domain knowledge shared by both Go analyzer backends.

The lexical analyzer (:mod:`operations.languages.go_analyzer`) and the
tree-sitter analyzer (:mod:`operations.languages.go_tree_sitter`) must agree on
what a Go type *means* — otherwise the two backends would disagree about
composition targets and method signatures, and switching backends would change
results. Anything language-specific but backend-independent belongs here.
"""

from __future__ import annotations

import re

from operations.languages.base import strip_generics

#: Predeclared Go types. Not modelled as composition targets: a `string` field
#: says nothing about the design, and edges to it swamp diagrams.
GO_PRIMITIVES: frozenset[str] = frozenset(
    {
        "string", "bool", "byte", "rune", "error", "any", "uintptr",
        "int", "int8", "int16", "int32", "int64",
        "uint", "uint8", "uint16", "uint32", "uint64",
        "float32", "float64", "complex64", "complex128",
    }
)

#: Type constructors, not types. ``map[string]string`` must not yield ``map``.
_TYPE_CONSTRUCTORS: frozenset[str] = frozenset(
    {"map", "chan", "func", "struct", "interface"}
)

_ARRAY_PREFIX = re.compile(r"^\[[^\]]*\]")
_PLAIN_NAME = re.compile(r"^[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)?$")


def element_type(raw: str) -> str | None:
    """Innermost named type of a Go type expression, or ``None`` if there is none.

    Peels pointers, slices, arrays, variadics, channels and map keys so that a
    field's *meaningful* type is what ends up in the graph:

    ``*User`` → ``User``; ``[]*User`` → ``User``; ``map[string]*User`` →
    ``User``; ``map[string]string`` → ``None`` (both sides primitive);
    ``func(int) error`` → ``None`` (a function type has no single element);
    ``string`` → ``None`` (predeclared).

    Returning ``None`` is the point: the caller records no relationship rather
    than an edge to something that is not a type in the repository.
    """
    text = " ".join((raw or "").split())
    while text:
        if text.startswith("..."):
            text = text[3:].strip()
        elif text.startswith("*") or text.startswith("&"):
            text = text[1:].strip()
        elif text.startswith("<-chan "):
            text = text[7:].strip()
        elif text.startswith("chan "):
            text = text[5:].strip()
        elif text.startswith("map["):
            closing = _matching_bracket(text, 3)
            if closing is None:
                return None
            text = text[closing + 1 :].strip()
        elif _ARRAY_PREFIX.match(text):
            text = _ARRAY_PREFIX.sub("", text, count=1).strip()
        else:
            break

    text = strip_generics(text)
    if not text or not _PLAIN_NAME.match(text):
        return None
    if text in GO_PRIMITIVES or text in _TYPE_CONSTRUCTORS:
        return None
    return text


def _matching_bracket(text: str, open_index: int) -> int | None:
    """Index of the ``]`` matching the ``[`` at ``open_index``."""
    depth = 0
    for position in range(open_index, len(text)):
        if text[position] == "[":
            depth += 1
        elif text[position] == "]":
            depth -= 1
            if depth == 0:
                return position
    return None


def receiver_type(raw: str) -> str | None:
    """Type of a *call receiver*, or ``None`` when it has no single named type.

    Distinct from :func:`element_type` on purpose. ``element_type("[]User")`` is
    ``User`` — right for "this field composes a User". But a receiver declared
    ``[]User`` is a *slice*, so ``users.Len()`` is a call on the slice, not on
    ``User``; attributing it to ``User`` would invent a method that does not
    exist. Only pointers are transparent here.
    """
    text = " ".join((raw or "").split())
    while text.startswith("*") or text.startswith("&"):
        text = text[1:].strip()
    if not text or text.startswith(("[", "map[", "chan ", "<-chan ", "func", "...")):
        return None
    text = strip_generics(text)
    if not _PLAIN_NAME.match(text) or text in GO_PRIMITIVES or text in _TYPE_CONSTRUCTORS:
        return None
    return text.rsplit(".", 1)[-1]


def simple_type_name(raw: str) -> str | None:
    """:func:`element_type` reduced to its last dotted segment (``pkg.T`` → ``T``)."""
    resolved = element_type(raw)
    return resolved.rsplit(".", 1)[-1] if resolved else None


def normalize_signature(name: str, parameters: list[str], results: list[str]) -> str:
    """Canonical ``name(param,param) (result)`` form used for method matching.

    Both backends emit this exact shape, so a method set parsed lexically and
    one parsed by tree-sitter compare equal — which is what makes the Go
    interface-satisfaction check in :mod:`operations.oop_analyzer` backend
    independent.
    """
    return f"{name}({','.join(parameters)}) ({','.join(results)})"


__all__ = [
    "GO_PRIMITIVES",
    "element_type",
    "receiver_type",
    "normalize_signature",
    "simple_type_name",
]
