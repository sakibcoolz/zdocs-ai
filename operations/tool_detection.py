"""Detect which external analysis tools are installed, and what happens if not.

The Repository Operations Agent is designed to degrade, never to lie: when an
external tool is missing it either uses a documented Python fallback or returns
a structured error naming the missing tool. This module makes that explicit, so
``make tools-check`` tells an operator exactly what they gain by installing
something — and application startup never depends on any of it.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from operations.policy import ANALYSIS_COMMANDS


class ToolLevel(str, Enum):
    """How much the platform depends on a tool."""

    #: Nothing works without it. (Currently nothing external qualifies.)
    REQUIRED = "required"
    #: Everything works without it, but noticeably slower or narrower.
    RECOMMENDED = "recommended"
    #: Purely additive; the built-in analyzers already cover the ground.
    OPTIONAL = "optional"


class ToolStatus(str, Enum):
    """Outcome of probing for one tool."""

    INSTALLED = "installed"
    MISSING_WITH_FALLBACK = "missing_with_fallback"
    MISSING_NO_FALLBACK = "missing_no_fallback"


@dataclass(frozen=True)
class ToolSpec:
    """One external tool the operations layer can use."""

    name: str
    level: ToolLevel
    purpose: str
    fallback: str | None
    """Python fallback description, or ``None`` when there is no substitute."""
    affected_operations: tuple[str, ...] = ()
    install_hint: str = ""
    probe: Callable[[], str | None] | None = field(default=None, compare=False)
    """Custom detector returning a location string, or ``None`` if absent.

    Used where "installed" means something other than a binary on ``PATH`` —
    tree-sitter is consumed through its **Python bindings**, so probing for a
    CLI would report the wrong thing.
    """


#: Every external tool the operations layer knows how to use. Each name here is
#: also on the executable allowlist in :mod:`operations.policy`.
TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="rg",
        level=ToolLevel.RECOMMENDED,
        purpose="Fast file discovery (rg --files) and candidate text search.",
        fallback="Pure-Python directory walk and line scan — same results, slower "
        "on large repositories.",
        affected_operations=("list_repository_files", "find_symbol", "find_references"),
        install_hint="dnf install ripgrep | apt install ripgrep | brew install ripgrep",
    ),
    ToolSpec(
        name="git",
        level=ToolLevel.RECOMMENDED,
        purpose="Read-only repository metadata (HEAD, branch, history, contributors) "
        "and the commit SHA used in the cache key.",
        fallback=None,
        affected_operations=("git_metadata",),
        install_hint="dnf install git | apt install git | brew install git",
    ),
    ToolSpec(
        name="tree-sitter",
        level=ToolLevel.RECOMMENDED,
        purpose="Real syntax-tree parsing for Go, Java, JavaScript and TypeScript. "
        "Raises those languages from lexical (medium confidence) to parsed "
        "(high confidence) and resolves call receivers to concrete types.",
        fallback="Built-in lexical analyzers (comment/string masked, brace "
        "matched). Same declarations and relationships, reported at medium "
        "confidence, with name-only call edges.",
        affected_operations=("analyze_oop", "find_class", "find_interface", "find_calls"),
        install_hint="pip install -r requirements-analyzers.txt",
        probe=lambda: _tree_sitter_location(),
    ),
    ToolSpec(
        name="ast-grep",
        level=ToolLevel.OPTIONAL,
        purpose="Structural pattern matching to confirm candidate matches.",
        fallback="Built-in lexical analyzers.",
        affected_operations=("find_symbol", "find_references"),
        install_hint="npm install -g @ast-grep/cli | brew install ast-grep",
    ),
    ToolSpec(
        name="ctags",
        level=ToolLevel.OPTIONAL,
        purpose="Universal Ctags symbol index for languages without an analyzer.",
        fallback="Built-in analyzers cover the supported languages; unsupported "
        "languages are reported as unanalyzed rather than guessed at.",
        affected_operations=("find_symbol",),
        install_hint="dnf install ctags | apt install universal-ctags | brew install universal-ctags",
    ),
)


@dataclass(frozen=True)
class ToolReport:
    """Probe result for one tool."""

    spec: ToolSpec
    status: ToolStatus
    path: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.spec.name,
            "level": self.spec.level.value,
            "status": self.status.value,
            "path": self.path,
            "purpose": self.spec.purpose,
            "fallback": self.spec.fallback,
            "affected_operations": list(self.spec.affected_operations),
            "install_hint": self.spec.install_hint,
        }


def _tree_sitter_location() -> str | None:
    """Describe the installed tree-sitter bindings and grammars, or ``None``.

    The analyzers use the Python bindings, never the ``tree-sitter`` CLI, so
    that is what this reports. Grammars are listed individually because
    availability is per language: a repository may get a parsed Go analysis and
    a lexical Java one on the same machine.
    """
    from operations.languages.tree_sitter_support import (
        available_languages as grammars,
        bindings_available,
    )

    if not bindings_available():
        return None
    installed = grammars()
    if not installed:
        return "python bindings installed, but no grammars"
    return f"python bindings + grammars: {', '.join(installed)}"


def detect_tools() -> list[ToolReport]:
    """Probe every known tool. Never raises, never runs the tools it finds."""
    reports: list[ToolReport] = []
    for spec in TOOL_SPECS:
        path = spec.probe() if spec.probe is not None else shutil.which(spec.name)
        if path is not None:
            status = ToolStatus.INSTALLED
        elif spec.fallback:
            status = ToolStatus.MISSING_WITH_FALLBACK
        else:
            status = ToolStatus.MISSING_NO_FALLBACK
        reports.append(ToolReport(spec=spec, status=status, path=path))
    return reports


def missing_required(reports: list[ToolReport] | None = None) -> list[str]:
    """Names of REQUIRED tools that are absent — the only startup blockers."""
    return [
        report.spec.name
        for report in (reports if reports is not None else detect_tools())
        if report.spec.level is ToolLevel.REQUIRED
        and report.status is not ToolStatus.INSTALLED
    ]


def format_report(reports: list[ToolReport] | None = None) -> str:
    """Human-readable table for ``make tools-check``."""
    reports = reports if reports is not None else detect_tools()
    icons = {
        ToolStatus.INSTALLED: "OK  ",
        ToolStatus.MISSING_WITH_FALLBACK: "WARN",
        ToolStatus.MISSING_NO_FALLBACK: "MISS",
    }
    lines = [
        "zdocs-ai — repository operations tool check",
        "",
        f"{'':4}  {'TOOL':<12} {'LEVEL':<12} {'STATUS':<22} LOCATION",
    ]
    for report in reports:
        lines.append(
            f"{icons[report.status]}  {report.spec.name:<12} "
            f"{report.spec.level.value:<12} {report.status.value:<22} "
            f"{report.path or '-'}"
        )
    lines.append("")
    lines.append("Active analyzer backend per language:")
    from operations.languages import analyzer_backends

    for language, backend in analyzer_backends().items():
        lines.append(f"    {language:<12} {backend}")
    lines.append("")
    for report in reports:
        if report.status is ToolStatus.INSTALLED:
            continue
        if report.spec.fallback:
            lines.append(f"- {report.spec.name}: not installed. Fallback in use: {report.spec.fallback}")
        else:
            lines.append(
                f"- {report.spec.name}: not installed and there is NO fallback. "
                f"These operations will return a structured error: "
                f"{', '.join(report.spec.affected_operations) or 'n/a'}"
            )
        if report.spec.install_hint:
            lines.append(f"    install: {report.spec.install_hint}")
    blockers = missing_required(reports)
    lines.append("")
    lines.append(
        "All required tools present — the application runs regardless of the "
        "warnings above."
        if not blockers
        else f"MISSING REQUIRED TOOLS: {', '.join(blockers)}"
    )
    # Sanity check: everything probed must also be allowlisted for execution.
    unlisted = [spec.name for spec in TOOL_SPECS if spec.name not in ANALYSIS_COMMANDS]
    if unlisted:  # pragma: no cover - guards against a future edit desyncing them
        lines.append(f"WARNING: probed but not allowlisted for execution: {unlisted}")
    return "\n".join(lines)


__all__ = [
    "TOOL_SPECS",
    "ToolLevel",
    "ToolReport",
    "ToolSpec",
    "ToolStatus",
    "detect_tools",
    "format_report",
    "missing_required",
]
