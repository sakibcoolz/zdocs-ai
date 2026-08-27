"""Text-level search over a staged repository, plus bounded file reads.

Ripgrep is used when available (``rg --json`` for unambiguous parsing, since a
path may itself contain ``:``); otherwise an equivalent pure-Python line scan
runs instead. Both honour the same ignore rules, file-size caps and match
limits, and both redact secrets from returned lines.

Text search is *candidate discovery only*. Nothing here claims to have
understood the code — matches are emitted with
:attr:`~operations.schemas.Confidence.LOW` and
:attr:`~operations.schemas.DetectionMethod.TEXT_SEARCH`/``RIPGREP``. The
analyzers in :mod:`operations.languages` are what upgrade a candidate to a
confirmed symbol or relationship.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

from operations.command_runner import CommandRunner
from operations.inventory import discover_files
from operations.policy import ExecutionPolicy, redact, resolve_repo_path
from operations.schemas import DetectionMethod

#: Read this many bytes to decide whether a file is binary.
_BINARY_SNIFF_BYTES = 8192


@dataclass(frozen=True)
class TextMatch:
    """One matching line found by text search."""

    file_path: str
    line: int
    column: int
    text: str


def word_pattern(symbol: str) -> str:
    """Word-boundary regex for an identifier, safely escaped."""
    return rf"\b{re.escape(symbol)}\b"


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\x00" in handle.read(_BINARY_SNIFF_BYTES)
    except OSError:  # pragma: no cover - unreadable file
        return True


def read_text_file(path: Path, policy: ExecutionPolicy) -> str | None:
    """Read a source file as text, or ``None`` if binary/oversized/unreadable."""
    try:
        if path.stat().st_size > policy.max_file_bytes:
            return None
    except OSError:  # pragma: no cover - race with deletion
        return None
    if _is_binary(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover - unreadable file
        return None


def _search_ripgrep(
    root: Path,
    policy: ExecutionPolicy,
    runner: CommandRunner,
    pattern: str,
    *,
    globs: list[str] | None,
    case_sensitive: bool,
    max_matches: int,
) -> tuple[list[TextMatch], bool] | None:
    """``rg --json`` search; ``None`` when ripgrep cannot serve the request."""
    if not runner.is_available("rg"):
        return None
    argv = [
        "rg", "--json", "--hidden", "--no-ignore", "--no-messages",
        "--max-count", str(max_matches),
    ]
    if not case_sensitive:
        argv.append("--ignore-case")
    if not policy.follow_symlinks:
        argv.append("--no-follow")
    from operations.inventory import IGNORED_DIRS

    for ignored in sorted(IGNORED_DIRS):
        argv += ["--glob", f"!{ignored}/"]
    for glob in globs or []:
        argv += ["--glob", glob]
    argv += ["--regexp", pattern]

    result = runner.run(argv, cwd=root)
    if result.timed_out or result.exit_code not in (0, 1):
        return None

    matches: list[TextMatch] = []
    for line in result.stdout.splitlines():
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:  # pragma: no cover - truncated tail
            continue
        if event.get("type") != "match":
            continue
        data = event["data"]
        path_text = (data.get("path") or {}).get("text")
        if not path_text:
            continue
        submatches = data.get("submatches") or [{}]
        matches.append(
            TextMatch(
                file_path=path_text.lstrip("./"),
                line=int(data.get("line_number") or 0),
                column=int((submatches[0].get("start") or 0)) + 1,
                text=redact((data.get("lines") or {}).get("text", "").rstrip("\n")),
            )
        )
        if len(matches) >= max_matches:
            return matches, True
    return matches, result.truncated


def _search_python(
    root: Path,
    policy: ExecutionPolicy,
    runner: CommandRunner | None,
    pattern: str,
    *,
    languages: set[str] | None,
    case_sensitive: bool,
    max_matches: int,
) -> tuple[list[TextMatch], bool]:
    """Pure-Python fallback scan used when ripgrep is not installed."""
    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(pattern, flags)
    files, truncated, _ = discover_files(root, policy, runner, languages=languages)
    matches: list[TextMatch] = []
    for repo_file in files:
        content = read_text_file(root / repo_file.path, policy)
        if content is None:
            continue
        for number, line in enumerate(content.splitlines(), start=1):
            found = compiled.search(line)
            if found is None:
                continue
            matches.append(
                TextMatch(
                    file_path=repo_file.path,
                    line=number,
                    column=found.start() + 1,
                    text=redact(line.rstrip()),
                )
            )
            if len(matches) >= max_matches:
                return matches, True
    return matches, truncated


def search_text(
    root: Path,
    policy: ExecutionPolicy,
    runner: CommandRunner | None,
    pattern: str,
    *,
    globs: list[str] | None = None,
    languages: set[str] | None = None,
    case_sensitive: bool = True,
    max_matches: int | None = None,
) -> tuple[list[TextMatch], bool, DetectionMethod]:
    """Search repository text for ``pattern``.

    Args:
        pattern: Regular expression (ripgrep/Rust and Python syntax overlap for
            the identifier patterns this layer generates).
        globs: ripgrep-style include/exclude globs (fast path only).
        languages: Restrict to these detected languages (fallback path; also
            converted to globs for ripgrep when no explicit globs are given).
        case_sensitive: Case-sensitive matching.
        max_matches: Cap, defaulting to the policy's ``max_matches``.

    Returns:
        ``(matches, truncated, detection_method)``.
    """
    limit = max_matches or policy.max_matches
    effective_globs = list(globs or [])
    if languages and not effective_globs:
        effective_globs = _globs_for_languages(languages)

    if runner is not None:
        fast = _search_ripgrep(
            root,
            policy,
            runner,
            pattern,
            globs=effective_globs,
            case_sensitive=case_sensitive,
            max_matches=limit,
        )
        if fast is not None:
            matches, truncated = fast
            return matches, truncated, DetectionMethod.RIPGREP

    matches, truncated = _search_python(
        root,
        policy,
        runner,
        pattern,
        languages=languages,
        case_sensitive=case_sensitive,
        max_matches=limit,
    )
    return matches, truncated, DetectionMethod.TEXT_SEARCH


def _globs_for_languages(languages: set[str]) -> list[str]:
    """ripgrep include-globs covering every extension of ``languages``."""
    from operations.inventory import EXTENSION_LANGUAGE

    return [
        f"*{extension}"
        for extension, language in sorted(EXTENSION_LANGUAGE.items())
        if language in languages
    ]


def read_file_range(
    root: Path,
    policy: ExecutionPolicy,
    file_path: str,
    *,
    start_line: int = 1,
    end_line: int | None = None,
    max_lines: int = 400,
) -> dict[str, object]:
    """Read a bounded, redacted line range from a repository file.

    Raises the usual policy errors (:class:`~operations.errors.PathEscapeError`,
    ``FileNotFoundError``) for anything outside the repository.
    """
    path = resolve_repo_path(root, file_path, follow_symlinks=policy.follow_symlinks)
    if not path.is_file():
        raise FileNotFoundError(f"Not a file: {file_path!r}")
    content = read_text_file(path, policy)
    if content is None:
        raise ValueError(f"File is binary or exceeds the size limit: {file_path!r}")

    lines = content.splitlines()
    start = max(1, int(start_line))
    stop = len(lines) if end_line is None else min(len(lines), int(end_line))
    stop = max(stop, start - 1)
    selected = lines[start - 1 : stop]
    truncated = len(selected) > max_lines
    if truncated:
        selected = selected[:max_lines]
        stop = start + max_lines - 1
    return {
        "file_path": file_path,
        "start_line": start,
        "end_line": start + len(selected) - 1 if selected else start - 1,
        "total_lines": len(lines),
        "truncated": truncated,
        "content": redact("\n".join(selected)),
    }


def excerpt(content: str, line: int, *, context: int = 0) -> str:
    """Redacted single line (optionally with context) from in-memory content."""
    lines = content.splitlines()
    if not (1 <= line <= len(lines)):
        return ""
    start = max(0, line - 1 - context)
    stop = min(len(lines), line + context)
    return redact("\n".join(lines[start:stop]).strip())
