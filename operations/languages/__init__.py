"""Language analyzer registry.

Every analyzer implements :class:`~operations.languages.base.LanguageAnalyzer`
and is registered here by language name and by file extension. Nothing else in
the codebase hard-codes a language: :mod:`operations.oop_analyzer` asks this
registry which analyzer (if any) can handle a file.

**Two backends per language.** Go, Java, JavaScript and TypeScript each have a
tree-sitter analyzer and a lexical one. The registry prefers tree-sitter when
its bindings *and* that language's grammar are installed, and silently falls
back to the lexical analyzer otherwise — so the project has no hard dependency
on tree-sitter, and installing it upgrades results without a code change. Both
backends emit the same :class:`~operations.schemas.FileAnalysis` shape; the
tree-sitter one reports higher confidence and resolves call receivers to types.

Python has one backend only: the standard library's ``ast`` module is a
first-party parser, so a grammar would add a dependency for nothing.

Use :func:`analyzer_backends` to see which backend is active per language.

**Adding a language**

1. Add ``operations/languages/<name>_analyzer.py`` with a ``LanguageAnalyzer``
   subclass that sets ``language``, ``extensions``, ``detection_method`` and
   ``base_confidence``.
2. Register the class in :data:`LEXICAL_ANALYZERS` (and, if you add a grammar,
   :data:`TREE_SITTER_ANALYZERS`).
3. Add its extensions to :data:`operations.inventory.EXTENSION_LANGUAGE` and,
   once it is dependable, to ``SUPPORTED_LANGUAGES`` there.
4. Add a fixture repository under ``tests/fixtures/`` and tests covering
   class/interface/function discovery plus at least one relationship kind —
   including a negative case for something that must *not* be detected.

Nothing else needs to change — the executor, graph, diagrams and API pick the
new language up automatically.
"""

from __future__ import annotations

from operations.errors import UnsupportedLanguage
from operations.languages.base import LanguageAnalyzer
from operations.languages.go_analyzer import GoAnalyzer
from operations.languages.go_tree_sitter import GoTreeSitterAnalyzer
from operations.languages.java_analyzer import JavaAnalyzer
from operations.languages.java_tree_sitter import JavaTreeSitterAnalyzer
from operations.languages.js_ts_analyzer import JavaScriptAnalyzer, TypeScriptAnalyzer
from operations.languages.js_ts_tree_sitter import (
    JavaScriptTreeSitterAnalyzer,
    TypeScriptTreeSitterAnalyzer,
)
from operations.languages.python_analyzer import PythonAnalyzer
from operations.languages.tree_sitter_support import is_available

#: Always-available analyzers. These define the supported language set.
LEXICAL_ANALYZERS: dict[str, type[LanguageAnalyzer]] = {
    "python": PythonAnalyzer,
    "go": GoAnalyzer,
    "java": JavaAnalyzer,
    "javascript": JavaScriptAnalyzer,
    "typescript": TypeScriptAnalyzer,
}

#: Preferred analyzers, used when the matching tree-sitter grammar is installed.
TREE_SITTER_ANALYZERS: dict[str, type[LanguageAnalyzer]] = {
    "go": GoTreeSitterAnalyzer,
    "java": JavaTreeSitterAnalyzer,
    "javascript": JavaScriptTreeSitterAnalyzer,
    "typescript": TypeScriptTreeSitterAnalyzer,
}


def _grammar_key(language: str) -> str:
    """Grammar name backing a language (TypeScript needs TSX for ``.tsx``)."""
    return language


def _select() -> dict[str, LanguageAnalyzer]:
    """Instantiate the best available analyzer per language.

    Analyzer instances are stateless, so one shared instance per language is
    safe and avoids re-instantiating on every file.
    """
    selected: dict[str, LanguageAnalyzer] = {}
    for language, lexical in LEXICAL_ANALYZERS.items():
        preferred = TREE_SITTER_ANALYZERS.get(language)
        if preferred is not None and is_available(_grammar_key(language)):
            selected[language] = preferred()  # type: ignore[abstract]
        else:
            selected[language] = lexical()  # type: ignore[abstract]
    return selected


_BY_LANGUAGE: dict[str, LanguageAnalyzer] = _select()
_BY_EXTENSION: dict[str, LanguageAnalyzer] = {
    extension: analyzer
    for analyzer in _BY_LANGUAGE.values()
    for extension in analyzer.extensions
}


def available_languages() -> list[str]:
    """Languages with a registered analyzer, sorted."""
    return sorted(_BY_LANGUAGE)


def analyzer_backends() -> dict[str, str]:
    """Language → active backend (``"tree_sitter"``, ``"python_ast"``, ``"lexical"``).

    Reported by ``make tools-check`` and ``GET /api/operations/tools`` so it is
    always visible which parser produced a given result.
    """
    return {
        language: analyzer.detection_method.value
        for language, analyzer in sorted(_BY_LANGUAGE.items())
    }


def get_analyzer(language: str) -> LanguageAnalyzer:
    """Active analyzer for ``language``; raises :class:`UnsupportedLanguage` if none."""
    try:
        return _BY_LANGUAGE[language.lower()]
    except KeyError as exc:
        raise UnsupportedLanguage(
            f"No analyzer registered for language {language!r}. "
            f"Available: {', '.join(available_languages())}"
        ) from exc


def get_lexical_analyzer(language: str) -> LanguageAnalyzer:
    """The always-available lexical analyzer for ``language``.

    Used by the parity tests, which assert both backends agree, and available to
    a caller that needs results reproducible on a machine without tree-sitter.
    """
    try:
        return LEXICAL_ANALYZERS[language.lower()]()  # type: ignore[abstract]
    except KeyError as exc:
        raise UnsupportedLanguage(f"No lexical analyzer for language {language!r}") from exc


def analyzer_for_path(file_path: str) -> LanguageAnalyzer | None:
    """Analyzer matching a file's extension, or ``None`` if unsupported."""
    lowered = file_path.lower()
    for extension in sorted(_BY_EXTENSION, key=len, reverse=True):
        if lowered.endswith(extension):
            return _BY_EXTENSION[extension]
    return None


__all__ = [
    "LEXICAL_ANALYZERS",
    "TREE_SITTER_ANALYZERS",
    "GoAnalyzer",
    "GoTreeSitterAnalyzer",
    "JavaAnalyzer",
    "JavaScriptAnalyzer",
    "JavaTreeSitterAnalyzer",
    "LanguageAnalyzer",
    "PythonAnalyzer",
    "TypeScriptAnalyzer",
    "analyzer_backends",
    "analyzer_for_path",
    "available_languages",
    "get_analyzer",
    "get_lexical_analyzer",
]
