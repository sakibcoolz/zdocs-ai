"""The operation executor: validate, dispatch, measure, audit.

This is the single entry point for running a repository operation. It is
deliberately free of FastAPI and Google ADK imports — the API layer and the
agent tool layer both wrap *this*, so the security-relevant code has one
implementation and can be tested without a web server or an LLM.

Flow for every request:

1. The operation is checked against the active :class:`ExecutionPolicy`.
2. The cache is consulted using the deterministic identity from
   :mod:`operations.cache`.
3. A handler runs, producing matches/data. Handlers never touch
   :mod:`subprocess` directly — they go through :class:`CommandRunner`.
4. Evidence excerpts are attached, redacted.
5. Duration, status and (redacted) arguments are written to the audit log.

Any :class:`~operations.errors.OperationError` becomes a structured
``status="failed"`` result. Unexpected exceptions are caught too: the caller
gets a safe message, and the traceback goes to the log, never to the response.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Callable

from operations.cache import (
    NullCache,
    OperationCache,
    cache_key,
    content_fingerprint,
    file_fingerprint,
)
from operations.command_runner import CommandRunner
from operations.diagram_generator import (
    DEFAULT_MAX_EDGES,
    DEFAULT_MAX_NODES,
    Diagram,
    generate_class_diagrams,
    generate_component_diagram,
    generate_inheritance_diagram,
    generate_package_dependency_diagram,
    generate_sequence_diagram,
)
from operations.docs_writer import DocsWriter
from operations.errors import OperationError, PolicyViolation
from operations.git_info import commit_sha, git_metadata
from operations.inventory import (
    count_files_and_directories,
    detect_languages,
    discover_files,
)
from operations.oop_analyzer import RepositoryAnalysis, analyze_repository
from operations.policy import (
    ExecutionPolicy,
    redact,
    redact_arguments,
    resolve_repo_path,
    resolve_repo_root,
)
from operations.relationship_graph import RelationshipGraph, build_graph
from operations.sandbox import (
    Sandbox,
    SandboxLimits,
    SandboxUnavailable,
    UnavailableSandbox,
    build_sandbox,
    resolve_validation_command,
)
from operations.schemas import (
    CodeMatch,
    Confidence,
    DetectionMethod,
    OperationRequest,
    OperationResult,
    OperationType,
    RelationshipInfo,
    RelationType,
    SourceEvidence,
    SymbolInfo,
    SymbolType,
    Visibility,
)
from operations.symbol_search import (
    read_file_range,
    read_text_file,
    search_text,
    word_pattern,
)

logger = logging.getLogger("zdocs.operations")

#: How many matches get a source excerpt attached. Evidence costs file reads.
DEFAULT_EVIDENCE_LIMIT = 25

_KIND_FOR_OPERATION: dict[OperationType, tuple[SymbolType, ...]] = {
    OperationType.FIND_CLASS: (SymbolType.CLASS, SymbolType.ABSTRACT_CLASS, SymbolType.STRUCT, SymbolType.RECORD),
    OperationType.FIND_INTERFACE: (SymbolType.INTERFACE,),
    OperationType.FIND_FUNCTION: (SymbolType.FUNCTION,),
    OperationType.FIND_METHOD: (SymbolType.METHOD, SymbolType.CONSTRUCTOR),
}


class OperationExecutor:
    """Runs :class:`OperationRequest` objects against one staged repository.

    Args:
        repo_root: Directory of the staged repository. Every path an operation
            touches is resolved inside it.
        repository: Name recorded on results and used for cache/docs scoping.
        policy: Execution policy. Defaults to the read-only analysis profile.
        runner: Command runner. Defaults to one built from ``policy``; inject a
            stub in tests to exercise tool-missing and timeout paths.
        cache: Result cache. Defaults to :class:`NullCache`.
        docs_root: Root for generated documentation. Defaults to
            ``<project>/generated-docs``.
        evidence_limit: Maximum matches given a source excerpt.
        sandbox: Isolated backend for the development-validation profile. Built
            on first use; irrelevant unless that profile is enabled.

    A single instance memoizes the repository analysis, so a burst of related
    operations parses the tree once. Instances are therefore intended to be
    short-lived (one per request or per agent tool call); a long-lived instance
    would not observe edits made to the repository after its first analysis.
    """

    def __init__(
        self,
        repo_root: str | Path,
        *,
        repository: str = "",
        policy: ExecutionPolicy | None = None,
        runner: CommandRunner | None = None,
        cache: OperationCache | None = None,
        docs_root: str | Path | None = None,
        evidence_limit: int = DEFAULT_EVIDENCE_LIMIT,
        sandbox: Sandbox | None = None,
    ) -> None:
        self.root = resolve_repo_root(repo_root)
        self.repository = repository or self.root.name
        self.policy = policy or ExecutionPolicy.repository_analysis()
        self.runner = runner if runner is not None else CommandRunner(self.policy)
        self.cache: OperationCache = cache or NullCache()
        self.docs_root = Path(
            docs_root
            if docs_root is not None
            else Path(__file__).resolve().parent.parent / "generated-docs"
        )
        self.evidence_limit = evidence_limit
        self._analysis_memo: dict[tuple, RepositoryAnalysis] = {}
        self._graph_memo: dict[tuple, RelationshipGraph] = {}
        self._source_memo: dict[str, str | None] = {}
        self._commit_sha: str | None | object = _UNSET
        self._sandbox = sandbox
        self._sandbox_probed = sandbox is not None

    # -- public API --------------------------------------------------------

    def available_operations(self) -> list[str]:
        """Operation names the active policy permits."""
        return sorted(
            operation.value
            for operation in OperationType
            if self._is_permitted(operation)
        )

    def execute(self, request: OperationRequest) -> OperationResult:
        """Validate, dispatch and measure one operation. Never raises."""
        started = time.perf_counter()
        try:
            self.policy.check_operation(request.operation)
            key = self._cache_key(request)
            if key is not None:
                cached = self.cache.get(key)
                if cached is not None:
                    cached.cache_hit = True
                    self._audit(request, cached, int((time.perf_counter() - started) * 1000))
                    return cached

            handler = _HANDLERS.get(request.operation)
            if handler is None:  # pragma: no cover - enum and table kept in sync
                raise OperationError(
                    f"No handler registered for operation {request.operation.value!r}"
                )
            result = handler(self, request)
            result.operation = request.operation
            result.repository = self.repository
            self._attach_evidence(result)
            if not result.errors and result.status == "failed":
                result.errors.append("Operation failed without a specific error.")
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            if key is not None and result.status != "failed":
                self.cache.set(key, result)
            self._audit(request, result, result.duration_ms)
            return result
        except PolicyViolation as exc:
            return self._failure(request, str(exc), started, category="policy")
        except OperationError as exc:
            return self._failure(request, str(exc), started, category="operation")
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError) as exc:
            return self._failure(request, str(exc), started, category="not_found")
        except ValueError as exc:
            return self._failure(request, str(exc), started, category="invalid_argument")
        except Exception:  # noqa: BLE001 - boundary: never leak a traceback
            logger.exception(
                "Unhandled error in operation %s on repository %s",
                request.operation.value,
                self.repository,
            )
            return self._failure(
                request,
                "Internal error while executing the operation. "
                "See server logs for details.",
                started,
                category="internal",
            )

    # -- shared analysis ---------------------------------------------------

    def analysis(
        self,
        *,
        languages: set[str] | None = None,
        subdir: str | None = None,
        max_files: int | None = None,
    ) -> RepositoryAnalysis:
        """Repository analysis, memoized for the lifetime of this executor."""
        key = (tuple(sorted(languages or ())), subdir or "", max_files or 0)
        analysis = self._analysis_memo.get(key)
        if analysis is None:
            analysis = analyze_repository(
                self.root,
                self.policy,
                self.runner,
                repository=self.repository,
                languages=languages,
                subdir=subdir,
                max_files=max_files,
            )
            self._analysis_memo[key] = analysis
        return analysis

    def graph(
        self,
        *,
        languages: set[str] | None = None,
        subdir: str | None = None,
        include_calls: bool = True,
        include_field_access: bool = False,
    ) -> RelationshipGraph:
        """Relationship graph, memoized alongside the analysis."""
        key = (
            tuple(sorted(languages or ())),
            subdir or "",
            include_calls,
            include_field_access,
        )
        graph = self._graph_memo.get(key)
        if graph is None:
            graph = build_graph(
                self.analysis(languages=languages, subdir=subdir),
                include_calls=include_calls,
                include_field_access=include_field_access,
            )
            self._graph_memo[key] = graph
        return graph

    def sandbox(self) -> Sandbox:
        """Validation sandbox, discovered on first use.

        Never probed for an analysis-only executor: discovery shells out to the
        container runtime, which is pure cost for the profile that will never
        use it.
        """
        if not self._sandbox_probed:
            self._sandbox = (
                build_sandbox()
                if self.policy.validation_enabled
                else UnavailableSandbox("the development-validation profile is disabled")
            )
            self._sandbox_probed = True
        return self._sandbox  # type: ignore[return-value]

    def docs_writer(self) -> DocsWriter:
        """Writer scoped to ``generated-docs/<repository>/``."""
        return DocsWriter(self.docs_root, self.repository)

    # -- internals ---------------------------------------------------------

    def _is_permitted(self, operation: OperationType) -> bool:
        try:
            self.policy.check_operation(operation)
        except PolicyViolation:
            return False
        return True

    def _head_sha(self) -> str | None:
        if self._commit_sha is _UNSET:
            try:
                self._commit_sha = commit_sha(self.root, self.runner)
            except OperationError:
                self._commit_sha = None
        return self._commit_sha  # type: ignore[return-value]

    def _cache_key(self, request: OperationRequest) -> str | None:
        """Deterministic cache identity, or ``None`` when caching is disabled."""
        if isinstance(self.cache, NullCache):
            return None
        if request.file_path:
            try:
                path = resolve_repo_path(
                    self.root, request.file_path, follow_symlinks=self.policy.follow_symlinks
                )
                fingerprint = file_fingerprint(path)
            except (OperationError, FileNotFoundError):
                fingerprint = ""
        else:
            files, _, _ = discover_files(self.root, self.policy, self.runner)
            fingerprint = content_fingerprint(self.root, [file.path for file in files])
        return cache_key(
            repository=self.repository,
            commit_sha=self._head_sha(),
            operation=request.operation.value,
            file_path=request.file_path,
            content_hash=fingerprint,
            arguments={
                "language": request.language,
                "symbol": request.symbol,
                **request.arguments,
            },
        )

    def _source(self, relative_path: str) -> str | None:
        """Cached, policy-bounded read of a repository file."""
        if relative_path not in self._source_memo:
            try:
                path = resolve_repo_path(
                    self.root, relative_path, follow_symlinks=self.policy.follow_symlinks
                )
                self._source_memo[relative_path] = read_text_file(path, self.policy)
            except (OperationError, FileNotFoundError, OSError):
                self._source_memo[relative_path] = None
        return self._source_memo[relative_path]

    def _attach_evidence(self, result: OperationResult) -> None:
        """Add redacted source excerpts for the first N matches."""
        if result.evidence or not result.matches:
            return
        for match in result.matches[: self.evidence_limit]:
            if not match.file_path or match.line is None:
                continue
            source = self._source(match.file_path)
            if source is None:
                continue
            lines = source.splitlines()
            if not (1 <= match.line <= len(lines)):
                continue
            end = min(len(lines), match.end_line or match.line)
            end = max(end, match.line)
            snippet = "\n".join(lines[match.line - 1 : min(end, match.line + 4)])
            result.evidence.append(
                SourceEvidence(
                    file_path=match.file_path,
                    line=match.line,
                    end_line=match.end_line,
                    excerpt=redact(snippet.strip()),
                    detection_method=match.detection_method,
                )
            )

    def _failure(
        self, request: OperationRequest, message: str, started: float, *, category: str
    ) -> OperationResult:
        duration = int((time.perf_counter() - started) * 1000)
        result = OperationResult(
            status="failed",
            operation=request.operation,
            repository=self.repository,
            errors=[redact(message)],
            data={"error_category": category},
            duration_ms=duration,
        )
        self._audit(request, result, duration)
        return result

    def _audit(self, request: OperationRequest, result: OperationResult, duration_ms: int) -> None:
        """Record the operation for later review. Arguments are redacted."""
        logger.info(
            "operation=%s repository=%s status=%s matches=%d duration_ms=%d "
            "cache_hit=%s truncated=%s errors=%s arguments=%s",
            request.operation.value,
            self.repository,
            result.status,
            len(result.matches),
            duration_ms,
            result.cache_hit,
            result.truncated,
            [redact(error) for error in result.errors],
            redact_arguments(
                {
                    "language": request.language or "",
                    "symbol": request.symbol or "",
                    "file_path": request.file_path or "",
                    **request.arguments,
                }
            ),
        )


class _Unset:
    """Sentinel distinguishing "not looked up yet" from "looked up, absent"."""


_UNSET = _Unset()


# --------------------------------------------------------------------------
# Argument helpers
# --------------------------------------------------------------------------


def _int_arg(
    request: OperationRequest, name: str, default: int, *, minimum: int = 0, maximum: int = 100_000
) -> int:
    raw = request.arguments.get(name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Argument {name!r} must be an integer, got {raw!r}") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"Argument {name!r} must be between {minimum} and {maximum}")
    return value


def _bool_arg(request: OperationRequest, name: str, default: bool) -> bool:
    raw = request.arguments.get(name, default)
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    raise ValueError(f"Argument {name!r} must be a boolean, got {raw!r}")


def _str_arg(request: OperationRequest, name: str, default: str | None = None) -> str | None:
    raw = request.arguments.get(name, default)
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"Argument {name!r} must be a string, got {raw!r}")
    return raw


def _languages_arg(request: OperationRequest) -> set[str] | None:
    """Language filter from ``request.language`` or ``arguments['languages']``."""
    raw = request.arguments.get("languages")
    languages: set[str] = set()
    if request.language:
        languages.add(request.language.lower())
    if isinstance(raw, str):
        languages.update(part.strip().lower() for part in raw.split(",") if part.strip())
    elif isinstance(raw, (list, tuple)):
        languages.update(str(part).strip().lower() for part in raw if str(part).strip())
    elif raw is not None:
        raise ValueError("Argument 'languages' must be a string or list of strings")
    return languages or None


def _require_symbol(request: OperationRequest) -> str:
    symbol = (request.symbol or "").strip()
    if not symbol:
        raise ValueError(f"Operation {request.operation.value!r} requires 'symbol'")
    if len(symbol) > 200:
        raise ValueError("Symbol name is unreasonably long")
    return symbol


# --------------------------------------------------------------------------
# Conversion helpers
# --------------------------------------------------------------------------


def _symbol_match(symbol: SymbolInfo) -> CodeMatch:
    return CodeMatch(
        file_path=symbol.file_path,
        symbol=symbol.qualified_name,
        symbol_type=symbol.symbol_type,
        line=symbol.line,
        end_line=symbol.end_line,
        language=symbol.language,
        visibility=symbol.visibility,
        detection_method=symbol.detection_method,
        confidence=symbol.confidence,
        snippet=symbol.signature or None,
        metadata={
            "package": symbol.package,
            "owner": symbol.owner,
            "is_abstract": symbol.is_abstract,
            "is_static": symbol.is_static,
            **symbol.metadata,
        },
    )


def _relationship_match(relationship: RelationshipInfo, symbol_type: SymbolType) -> CodeMatch:
    return CodeMatch(
        file_path=relationship.file_path,
        symbol=relationship.source,
        symbol_type=symbol_type,
        relationship=relationship.relation,
        target_symbol=relationship.target,
        line=relationship.line,
        end_line=relationship.end_line,
        language=relationship.language,
        detection_method=relationship.detection_method,
        confidence=relationship.confidence,
        metadata=dict(relationship.metadata),
    )


def _symbol_types(analysis: RepositoryAnalysis) -> dict[str, SymbolType]:
    return {symbol.name: symbol.symbol_type for symbol in analysis.symbols}


def _matches_name(candidate: str, wanted: str, *, exact: bool) -> bool:
    simple = candidate.rsplit(".", 1)[-1]
    if exact:
        return candidate == wanted or simple == wanted
    lowered = wanted.lower()
    return lowered in candidate.lower() or lowered in simple.lower()


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


def _handle_list_files(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    limit = _int_arg(request, "limit", 1000, minimum=1, maximum=50_000)
    subdir = _str_arg(request, "subdir") or request.file_path
    files, truncated, method = discover_files(
        executor.root,
        executor.policy,
        executor.runner,
        subdir=subdir,
        languages=_languages_arg(request),
        limit=limit,
    )
    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        data={
            "files": [
                {"path": file.path, "bytes": file.size, "language": file.language}
                for file in files
            ],
            "file_count": len(files),
            "detection_method": method.value,
        },
        truncated=truncated,
        warnings=["File list truncated at the requested limit."] if truncated else [],
    )


def _handle_count(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    data = count_files_and_directories(executor.root, executor.policy, executor.runner)
    truncated = bool(data.get("truncated"))
    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        data=data,
        truncated=truncated,
    )


def _handle_languages(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    data = detect_languages(executor.root, executor.policy, executor.runner)
    truncated = bool(data.get("truncated"))
    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        data=data,
        truncated=truncated,
    )


def _handle_find_declarations(
    executor: OperationExecutor, request: OperationRequest
) -> OperationResult:
    """Shared handler for find_class/find_interface/find_function/find_method."""
    kinds = _KIND_FOR_OPERATION[request.operation]
    exact = _bool_arg(request, "exact", False)
    limit = _int_arg(request, "limit", executor.policy.max_matches, minimum=1)
    wanted = (request.symbol or "").strip()
    analysis = executor.analysis(languages=_languages_arg(request))

    matches = [
        _symbol_match(symbol)
        for symbol in analysis.symbols
        if symbol.symbol_type in kinds
        and (not wanted or _matches_name(symbol.qualified_name, wanted, exact=exact))
    ]
    truncated = len(matches) > limit
    return OperationResult(
        status="partial" if (truncated or analysis.truncated) else "success",
        operation=request.operation,
        matches=matches[:limit],
        data={
            "total_matches": len(matches),
            "languages_analyzed": analysis.languages,
            "files_analyzed": analysis.files_analyzed,
        },
        truncated=truncated or analysis.truncated,
        warnings=analysis.warnings[:10],
        errors=analysis.errors[:10],
    )


def _handle_find_symbol(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    symbol = _require_symbol(request)
    exact = _bool_arg(request, "exact", True)
    limit = _int_arg(request, "limit", 200, minimum=1)
    languages = _languages_arg(request)
    analysis = executor.analysis(languages=languages)

    matches = [
        _symbol_match(candidate)
        for candidate in analysis.symbols
        if _matches_name(candidate.qualified_name, symbol, exact=exact)
    ]

    warnings: list[str] = []
    if not matches:
        # Nothing declared under that name in an analyzed language — fall back
        # to text search so the caller still gets candidates, clearly marked as
        # unconfirmed rather than presented as declarations.
        text_matches, truncated, method = search_text(
            executor.root,
            executor.policy,
            executor.runner,
            word_pattern(symbol),
            languages=languages,
            max_matches=limit,
        )
        matches = [
            CodeMatch(
                file_path=hit.file_path,
                symbol=symbol,
                symbol_type=SymbolType.UNKNOWN,
                line=hit.line,
                detection_method=method,
                confidence=Confidence.LOW,
                snippet=hit.text,
                metadata={"kind": "text_candidate", "column": hit.column},
            )
            for hit in text_matches
        ]
        if matches:
            warnings.append(
                "No declaration was parsed for this symbol; results are "
                "text-search candidates only (low confidence)."
            )
        if truncated:
            warnings.append("Text search truncated at the match limit.")

    return OperationResult(
        status="success" if matches else "partial",
        operation=request.operation,
        matches=matches[:limit],
        data={"total_matches": len(matches), "symbol": symbol},
        warnings=warnings,
        truncated=len(matches) > limit,
    )


def _handle_find_references(
    executor: OperationExecutor, request: OperationRequest
) -> OperationResult:
    symbol = _require_symbol(request)
    limit = _int_arg(request, "limit", 300, minimum=1)
    languages = _languages_arg(request)
    analysis = executor.analysis(languages=languages)
    kinds = _symbol_types(analysis)

    declarations = {
        (candidate.file_path, candidate.line)
        for candidate in analysis.symbols
        if _matches_name(candidate.qualified_name, symbol, exact=True)
    }

    matches: list[CodeMatch] = [
        _relationship_match(relationship, kinds.get(relationship.source, SymbolType.UNKNOWN))
        for relationship in analysis.relationships
        if _matches_name(relationship.target, symbol, exact=True)
    ]

    text_matches, truncated, method = search_text(
        executor.root,
        executor.policy,
        executor.runner,
        word_pattern(symbol),
        languages=languages,
        max_matches=limit,
    )
    known = {(match.file_path, match.line) for match in matches}
    for hit in text_matches:
        if (hit.file_path, hit.line) in known:
            continue
        matches.append(
            CodeMatch(
                file_path=hit.file_path,
                symbol=symbol,
                symbol_type=SymbolType.UNKNOWN,
                line=hit.line,
                detection_method=method,
                confidence=Confidence.LOW,
                snippet=hit.text,
                metadata={
                    "kind": "declaration"
                    if (hit.file_path, hit.line) in declarations
                    else "text_candidate",
                    "column": hit.column,
                },
            )
        )

    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        matches=matches[:limit],
        data={
            "total_matches": len(matches),
            "declaration_sites": [
                {"file_path": path, "line": line} for path, line in sorted(declarations)
            ],
        },
        truncated=truncated or len(matches) > limit,
        warnings=[
            "Text-search results are candidates, not confirmed references; "
            "structural results carry their analyzer's confidence."
        ],
    )


def _relationship_handler(
    relations: tuple[RelationType, ...],
    *,
    match_target: bool,
    match_source: bool,
) -> Callable[[OperationExecutor, OperationRequest], OperationResult]:
    """Build a handler returning relationship edges of the given kinds."""

    def handler(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
        limit = _int_arg(request, "limit", 500, minimum=1)
        wanted = (request.symbol or "").strip()
        analysis = executor.analysis(languages=_languages_arg(request))
        kinds = _symbol_types(analysis)

        matches: list[CodeMatch] = []
        for relationship in analysis.relationships:
            if relationship.relation not in relations:
                continue
            if wanted:
                hit = (
                    match_target and _matches_name(relationship.target, wanted, exact=True)
                ) or (match_source and _matches_name(relationship.source, wanted, exact=True))
                if not hit:
                    continue
            matches.append(
                _relationship_match(
                    relationship, kinds.get(relationship.source, SymbolType.UNKNOWN)
                )
            )
        return OperationResult(
            status="success",
            operation=request.operation,
            matches=matches[:limit],
            data={
                "total_matches": len(matches),
                "relations": [relation.value for relation in relations],
                "symbol": wanted or None,
            },
            truncated=len(matches) > limit,
            warnings=analysis.warnings[:5],
        )

    return handler


def _handle_read_file_range(
    executor: OperationExecutor, request: OperationRequest
) -> OperationResult:
    if not request.file_path:
        raise ValueError("Operation 'read_file_range' requires 'file_path'")
    start = _int_arg(request, "start_line", 1, minimum=1, maximum=10_000_000)
    end_raw = request.arguments.get("end_line")
    end = None if end_raw in (None, "") else _int_arg(request, "end_line", start, minimum=1, maximum=10_000_000)
    max_lines = _int_arg(request, "max_lines", 400, minimum=1, maximum=5000)
    data = read_file_range(
        executor.root,
        executor.policy,
        request.file_path,
        start_line=start,
        end_line=end,
        max_lines=max_lines,
    )
    truncated = bool(data.get("truncated"))
    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        data=data,
        truncated=truncated,
        evidence=[
            SourceEvidence(
                file_path=str(data["file_path"]),
                line=int(data["start_line"]),
                end_line=int(data["end_line"]),
                excerpt=str(data["content"])[:2000],
                detection_method=DetectionMethod.FILESYSTEM,
            )
        ],
    )


def _handle_analyze_oop(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    limit = _int_arg(request, "limit", 500, minimum=1)
    languages = _languages_arg(request)
    analysis = executor.analysis(languages=languages, subdir=_str_arg(request, "subdir"))
    wanted = (request.symbol or "").strip()

    types = [
        symbol
        for symbol in analysis.types()
        if not wanted or _matches_name(symbol.name, wanted, exact=False)
    ]
    matches = [_symbol_match(symbol) for symbol in types]
    kinds = _symbol_types(analysis)
    structural = [
        relationship
        for relationship in analysis.relationships
        if relationship.relation
        in (RelationType.INHERITS, RelationType.IMPLEMENTS, RelationType.CONTAINS, RelationType.USES)
        and (not wanted or _matches_name(relationship.source, wanted, exact=True)
             or _matches_name(relationship.target, wanted, exact=True))
    ]
    matches.extend(
        _relationship_match(relationship, kinds.get(relationship.source, SymbolType.UNKNOWN))
        for relationship in structural
    )

    return OperationResult(
        status="partial" if analysis.truncated else "success",
        operation=request.operation,
        matches=matches[:limit],
        data={
            "summary": analysis.summary(),
            "languages": analysis.languages,
            "polymorphism": analysis.polymorphism(),
            "encapsulation": _encapsulation_report(analysis),
            "total_matches": len(matches),
        },
        truncated=analysis.truncated or len(matches) > limit,
        warnings=analysis.warnings[:10],
        errors=analysis.errors[:10],
    )


def _encapsulation_report(analysis: RepositoryAnalysis) -> dict[str, object]:
    """Visibility breakdown of type members, plus public-field outliers."""
    members = [symbol for symbol in analysis.symbols if symbol.owner]
    by_visibility: dict[str, int] = {}
    for member in members:
        by_visibility[member.visibility.value] = by_visibility.get(member.visibility.value, 0) + 1
    public_fields = [
        f"{member.owner}.{member.name}"
        for member in members
        if member.symbol_type is SymbolType.FIELD and member.visibility is Visibility.PUBLIC
    ]
    return {
        "member_count": len(members),
        "members_by_visibility": dict(sorted(by_visibility.items())),
        "public_field_count": len(public_fields),
        "public_field_examples": sorted(public_fields)[:25],
    }


def _handle_build_graph(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    include_calls = _bool_arg(request, "include_calls", True)
    include_field_access = _bool_arg(request, "include_field_access", False)
    max_nodes = _int_arg(request, "max_nodes", 500, minimum=1, maximum=20_000)
    max_edges = _int_arg(request, "max_edges", 1000, minimum=1, maximum=50_000)
    graph = executor.graph(
        languages=_languages_arg(request),
        subdir=_str_arg(request, "subdir"),
        include_calls=include_calls,
        include_field_access=include_field_access,
    )
    limited = graph.limited(max_nodes=max_nodes, max_edges=max_edges)
    truncated = limited is not graph
    return OperationResult(
        status="partial" if truncated else "success",
        operation=request.operation,
        data={
            "stats": graph.stats(),
            "nodes": [node.model_dump(mode="json") for node in limited.nodes],
            "edges": [edge.model_dump(mode="json") for edge in limited.edges],
            "packages": limited.packages(),
            "package_dependencies": [
                {"from": source, "to": target, "weight": weight}
                for source, target, weight in graph.package_dependency_edges()[:200]
            ],
            "omitted_nodes": limited.omitted_nodes[:200],
            "omitted_edge_count": limited.omitted_edge_count,
        },
        truncated=truncated,
        warnings=limited.warnings,
    )


def _diagram_handler(kind: str) -> Callable[[OperationExecutor, OperationRequest], OperationResult]:
    """Build a handler that generates (and optionally writes) diagrams."""

    def handler(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
        max_nodes = _int_arg(request, "max_nodes", DEFAULT_MAX_NODES, minimum=1, maximum=1000)
        max_edges = _int_arg(request, "max_edges", DEFAULT_MAX_EDGES, minimum=1, maximum=5000)
        write = _bool_arg(request, "write", False)
        split = _bool_arg(request, "split_by_package", False)
        graph = executor.graph(
            languages=_languages_arg(request),
            subdir=_str_arg(request, "subdir"),
            include_calls=kind == "sequence",
        )

        diagrams: list[Diagram]
        if kind == "class":
            diagrams = generate_class_diagrams(
                graph, max_nodes=max_nodes, max_edges=max_edges, split_by_package=split
            )
        elif kind == "inheritance":
            diagrams = [
                generate_inheritance_diagram(graph, max_nodes=max_nodes, max_edges=max_edges)
            ]
        elif kind == "dependency":
            diagrams = [
                generate_package_dependency_diagram(
                    graph,
                    max_nodes=max_nodes,
                    max_edges=max_edges,
                    include_external=_bool_arg(request, "include_external", False),
                )
            ]
        elif kind == "component":
            diagrams = [
                generate_component_diagram(graph, max_nodes=max_nodes, max_edges=max_edges)
            ]
        elif kind == "sequence":
            diagrams = [
                generate_sequence_diagram(
                    graph,
                    start_symbol=_require_symbol(request),
                    max_steps=_int_arg(request, "max_steps", 25, minimum=1, maximum=200),
                    max_depth=_int_arg(request, "max_depth", 4, minimum=1, maximum=20),
                )
            ]
        else:  # pragma: no cover - table and enum kept in sync
            raise OperationError(f"Unknown diagram kind: {kind!r}")

        written: list[str] = []
        if write:
            writer = executor.docs_writer()
            written = [str(writer.write_diagram(diagram)) for diagram in diagrams]

        warnings = [warning for diagram in diagrams for warning in diagram.warnings]
        empty = all(diagram.edge_count == 0 for diagram in diagrams)
        return OperationResult(
            status="partial" if (warnings or empty) else "success",
            operation=request.operation,
            data={
                "diagrams": [diagram.model_dump(mode="json") for diagram in diagrams],
                "written_files": written,
                "diagram_count": len(diagrams),
            },
            warnings=warnings,
            truncated=any(diagram.omitted_nodes for diagram in diagrams),
        )

    return handler


def _handle_git_metadata(executor: OperationExecutor, request: OperationRequest) -> OperationResult:
    data = git_metadata(executor.root, executor.runner)
    return OperationResult(
        status="success" if data.get("is_git_repository") else "partial",
        operation=request.operation,
        data=data,
        warnings=[str(data["reason"])] if "reason" in data else [],
        errors=[str(error) for error in data.get("errors", [])],
    )


def _handle_run_static_analysis(
    executor: OperationExecutor, request: OperationRequest
) -> OperationResult:
    """Run an approved validation tool inside an isolated sandbox.

    Reachable only when the development-validation profile is explicitly
    enabled (the policy refuses the operation otherwise) *and* a container
    sandbox is available. With no sandbox this fails loudly rather than running
    a repository's own test suite on the host — which would execute untrusted
    code with the server's privileges.

    A non-zero exit code is a *successful run with findings*, not a failure:
    linters and type checkers are supposed to exit non-zero when they have
    something to say. Only an unavailable sandbox or a timeout is a failure.
    """
    tool = _str_arg(request, "tool") or ""
    command = resolve_validation_command(tool, executor.policy.validation_tools)

    path = _str_arg(request, "path") or request.file_path
    if path:
        resolve_repo_path(
            executor.root, path, follow_symlinks=executor.policy.follow_symlinks
        )

    limits = SandboxLimits(
        memory_mb=_int_arg(request, "memory_mb", 512, minimum=64, maximum=8192),
        cpus=float(request.arguments.get("cpus", 1.0)),
        pids=_int_arg(request, "pids", 256, minimum=16, maximum=4096),
        timeout_seconds=float(request.arguments.get("timeout_seconds", 120.0)),
        max_output_bytes=_int_arg(
            request, "max_output_bytes", 1_000_000, minimum=1024, maximum=20_000_000
        ),
    )

    sandbox = executor.sandbox()
    try:
        outcome = sandbox.run(command, executor.root, limits, path=path)
    except SandboxUnavailable as exc:
        return OperationResult(
            status="failed",
            operation=request.operation,
            errors=[str(exc)],
            data={
                "error_category": "sandbox_unavailable",
                "profile": executor.policy.profile.value,
                "sandbox": sandbox.describe(),
                "tool": command.name,
            },
        )

    return OperationResult(
        status="partial" if outcome.timed_out else "success",
        operation=request.operation,
        data={
            "tool": outcome.tool,
            "passed": outcome.passed,
            "exit_code": outcome.exit_code,
            "stdout": outcome.stdout,
            "stderr": outcome.stderr,
            "timed_out": outcome.timed_out,
            "backend": outcome.backend,
            "image": outcome.image,
            "argv": outcome.argv,
            "limits": limits.model_dump(),
            "isolation": {
                "network": "none",
                "filesystem": "read-only, repository mounted read-only",
                "capabilities": "all dropped, no-new-privileges, unprivileged uid",
            },
        },
        truncated=outcome.truncated,
        warnings=(
            [f"{command.name} timed out after {limits.timeout_seconds:g}s"]
            if outcome.timed_out
            else []
        ),
    )


_HANDLERS: dict[OperationType, Callable[[OperationExecutor, OperationRequest], OperationResult]] = {
    OperationType.LIST_REPOSITORY_FILES: _handle_list_files,
    OperationType.COUNT_FILES_AND_DIRECTORIES: _handle_count,
    OperationType.DETECT_LANGUAGES: _handle_languages,
    OperationType.FIND_CLASS: _handle_find_declarations,
    OperationType.FIND_INTERFACE: _handle_find_declarations,
    OperationType.FIND_FUNCTION: _handle_find_declarations,
    OperationType.FIND_METHOD: _handle_find_declarations,
    OperationType.FIND_SYMBOL: _handle_find_symbol,
    OperationType.FIND_REFERENCES: _handle_find_references,
    OperationType.FIND_IMPLEMENTATIONS: _relationship_handler(
        (RelationType.IMPLEMENTS,), match_target=True, match_source=True
    ),
    OperationType.FIND_INHERITANCE: _relationship_handler(
        (RelationType.INHERITS,), match_target=True, match_source=True
    ),
    OperationType.FIND_IMPORTS: _relationship_handler(
        (RelationType.IMPORTS,), match_target=True, match_source=True
    ),
    OperationType.FIND_CALLS: _relationship_handler(
        (RelationType.CALLS,), match_target=True, match_source=True
    ),
    OperationType.READ_FILE_RANGE: _handle_read_file_range,
    OperationType.ANALYZE_OOP: _handle_analyze_oop,
    OperationType.BUILD_RELATIONSHIP_GRAPH: _handle_build_graph,
    OperationType.GENERATE_CLASS_DIAGRAM: _diagram_handler("class"),
    OperationType.GENERATE_INHERITANCE_DIAGRAM: _diagram_handler("inheritance"),
    OperationType.GENERATE_DEPENDENCY_DIAGRAM: _diagram_handler("dependency"),
    OperationType.GENERATE_COMPONENT_DIAGRAM: _diagram_handler("component"),
    OperationType.GENERATE_SEQUENCE_DIAGRAM: _diagram_handler("sequence"),
    OperationType.GIT_METADATA: _handle_git_metadata,
    OperationType.RUN_STATIC_ANALYSIS: _handle_run_static_analysis,
}

#: Every operation must have a handler; a missing entry is a programming error.
assert set(_HANDLERS) == set(OperationType), sorted(
    operation.value for operation in set(OperationType) - set(_HANDLERS)
)


__all__ = ["DEFAULT_EVIDENCE_LIMIT", "OperationExecutor"]
