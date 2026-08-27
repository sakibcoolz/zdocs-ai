"""FastAPI routes for the Repository Operations Agent.

Kept out of ``server.py`` so the existing staging/chat routes stay untouched,
and kept thin on purpose: every route validates input, delegates to
:class:`RepositoryOperationsService`, and translates domain failures into safe
HTTP errors. No analysis logic lives in a handler.

Handlers are declared ``def`` (not ``async def``): FastAPI runs sync handlers in
a worker thread, so a multi-second repository parse never blocks the event
loop.

Errors are translated, never leaked — a failed operation becomes a 4xx/5xx with
a redacted message, and the traceback goes to the log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from operations.docs_writer import DocsWriter
from operations.executor import OperationExecutor
from operations.policy import ExecutionPolicy
from operations.schemas import OperationRequest, OperationResult, OperationType
from operations.tool_detection import detect_tools
from tools.stage_registry import staged_repo_dir

#: Diagram kind → the operation that produces it.
DIAGRAM_OPERATIONS: dict[str, OperationType] = {
    "class": OperationType.GENERATE_CLASS_DIAGRAM,
    "inheritance": OperationType.GENERATE_INHERITANCE_DIAGRAM,
    "dependency": OperationType.GENERATE_DEPENDENCY_DIAGRAM,
    "component": OperationType.GENERATE_COMPONENT_DIAGRAM,
    "sequence": OperationType.GENERATE_SEQUENCE_DIAGRAM,
}

#: Failure category (set by the executor) → HTTP status code.
_STATUS_FOR_CATEGORY: dict[str, int] = {
    "policy": 403,
    "not_found": 404,
    "invalid_argument": 400,
    "operation": 422,
    "not_implemented": 501,
    "sandbox_unavailable": 503,
    "internal": 500,
}


# --------------------------------------------------------------------------
# Request/response models
# --------------------------------------------------------------------------


class OperationRequestBody(BaseModel):
    """POST body for ``/operations``. ``repository`` comes from the URL path."""

    operation: OperationType
    language: str | None = None
    symbol: str | None = None
    file_path: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


class InventoryResponse(BaseModel):
    """Combined file counts and language breakdown for a repository."""

    repository: str
    counts: dict[str, Any]
    languages: dict[str, Any]
    git: dict[str, Any]


class DiagramRequest(BaseModel):
    """POST body for ``/diagrams``."""

    kinds: list[Literal["class", "inheritance", "dependency", "component", "sequence"]] = Field(
        default_factory=lambda: ["class", "inheritance", "dependency"]
    )
    write: bool = True
    write_documents: bool = False
    """Also write OOP_ANALYSIS.md / CLASS_CATALOG.md / etc."""
    split_by_package: bool = False
    include_external: bool = False
    start_symbol: str | None = None
    """Required for ``sequence`` diagrams."""
    max_nodes: int = Field(default=60, ge=1, le=1000)
    max_edges: int = Field(default=150, ge=1, le=5000)
    language: str | None = None


class DiagramSummary(BaseModel):
    """One generated diagram, without its Mermaid body repeated in a list view."""

    title: str
    kind: str
    filename: str
    node_count: int = 0
    edge_count: int = 0
    omitted_node_count: int = 0
    omitted_edge_count: int = 0
    warnings: list[str] = Field(default_factory=list)
    mermaid: str = ""


class DiagramGenerationResponse(BaseModel):
    """Result of a diagram-generation request."""

    repository: str
    status: Literal["success", "partial", "failed"]
    diagrams: list[DiagramSummary] = Field(default_factory=list)
    written_files: list[str] = Field(default_factory=list)
    written_documents: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    duration_ms: int = 0


class DiagramListResponse(BaseModel):
    """Diagrams and documents already generated for a repository."""

    repository: str
    diagrams: list[dict[str, Any]] = Field(default_factory=list)
    documents: list[str] = Field(default_factory=list)
    output_directory: str


class ToolStatusResponse(BaseModel):
    """Availability of the external analysis tools."""

    tools: list[dict[str, Any]]
    missing_required: list[str]
    supported_languages: list[str]


# --------------------------------------------------------------------------
# Service layer
# --------------------------------------------------------------------------


class RepositoryOperationsService:
    """Resolves a staged repository and runs operations against it.

    Holds no FastAPI types: it raises ``FileNotFoundError``/``ValueError`` for
    an unknown or unsafe repository name and returns
    :class:`OperationResult` objects for everything else. The route layer is
    responsible for turning those into HTTP responses.
    """

    def __init__(
        self,
        stage_dir_provider: Callable[[], Path],
        docs_root_provider: Callable[[], Path],
        *,
        policy: ExecutionPolicy | None = None,
    ) -> None:
        self._stage_dir_provider = stage_dir_provider
        self._docs_root_provider = docs_root_provider
        self._policy = policy or ExecutionPolicy.repository_analysis()

    def executor(self, reponame: str) -> OperationExecutor:
        """Build an executor for a staged repository.

        Reuses ``tools.stage_registry.staged_repo_dir``, so the repository name
        gets exactly the same traversal validation as the existing chat route.
        """
        repo_dir = staged_repo_dir(reponame, stage_dir=self._stage_dir_provider())
        return OperationExecutor(
            repo_dir,
            repository=reponame,
            policy=self._policy,
            docs_root=self._docs_root_provider(),
        )

    def docs_writer(self, reponame: str) -> DocsWriter:
        """Docs writer for a staged repository (validates the name first)."""
        staged_repo_dir(reponame, stage_dir=self._stage_dir_provider())
        return DocsWriter(self._docs_root_provider(), reponame)

    def run(
        self,
        reponame: str,
        operation: OperationType,
        *,
        language: str | None = None,
        symbol: str | None = None,
        file_path: str | None = None,
        arguments: dict[str, Any] | None = None,
        executor: OperationExecutor | None = None,
    ) -> OperationResult:
        """Execute one operation against a staged repository."""
        runner = executor or self.executor(reponame)
        return runner.execute(
            OperationRequest(
                operation=operation,
                repository=reponame,
                language=language,
                symbol=symbol,
                file_path=file_path,
                arguments=arguments or {},
            )
        )


# --------------------------------------------------------------------------
# Error translation
# --------------------------------------------------------------------------


def _resolve_or_404(service: RepositoryOperationsService, reponame: str) -> OperationExecutor:
    try:
        return service.executor(reponame)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=404, detail=f"Repo not staged: {reponame!r}") from exc


def _raise_for_failure(result: OperationResult) -> OperationResult:
    """Turn a failed operation into an HTTP error with a safe message."""
    if result.status != "failed":
        return result
    category = str(result.data.get("error_category", "operation"))
    detail = result.errors[0] if result.errors else "Operation failed."
    raise HTTPException(status_code=_STATUS_FOR_CATEGORY.get(category, 422), detail=detail)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------


def create_operations_router(
    stage_dir_provider: Callable[[], Path],
    docs_root_provider: Callable[[], Path],
    *,
    policy: ExecutionPolicy | None = None,
) -> APIRouter:
    """Build the operations router.

    The directories are supplied as callables rather than values so the router
    always reads the *current* configuration — which keeps it working when a
    test monkeypatches ``server.STAGE_DIR``.
    """
    service = RepositoryOperationsService(
        stage_dir_provider, docs_root_provider, policy=policy
    )
    router = APIRouter(prefix="/api", tags=["repository-operations"])

    @router.get("/operations", response_model=dict)
    def list_operations() -> dict:
        """Operations the active policy permits, and their profile."""
        from operations.sandbox import VALIDATION_COMMANDS

        policy = service._policy
        return {
            "profile": policy.profile.value,
            "validation_enabled": policy.validation_enabled,
            "validation_tools": sorted(policy.validation_tools),
            "approved_validation_tools": sorted(VALIDATION_COMMANDS),
            "operations": sorted(
                operation.value
                for operation in OperationType
                if operation in policy.allowed_operations
                and not (
                    operation is OperationType.RUN_STATIC_ANALYSIS
                    and not policy.validation_enabled
                )
            ),
        }

    @router.get("/operations/tools", response_model=ToolStatusResponse)
    def tool_status() -> ToolStatusResponse:
        """Which external analysis tools are installed, and their fallbacks."""
        from operations.inventory import SUPPORTED_LANGUAGES
        from operations.tool_detection import missing_required

        reports = detect_tools()
        return ToolStatusResponse(
            tools=[report.as_dict() for report in reports],
            missing_required=missing_required(reports),
            supported_languages=sorted(SUPPORTED_LANGUAGES),
        )

    @router.post("/repos/{reponame}/operations", response_model=OperationResult)
    def run_operation(reponame: str, body: OperationRequestBody) -> OperationResult:
        """Run one structured operation against a staged repository."""
        executor = _resolve_or_404(service, reponame)
        result = service.run(
            reponame,
            body.operation,
            language=body.language,
            symbol=body.symbol,
            file_path=body.file_path,
            arguments=body.arguments,
            executor=executor,
        )
        return _raise_for_failure(result)

    @router.get("/repos/{reponame}/inventory", response_model=InventoryResponse)
    def inventory(reponame: str) -> InventoryResponse:
        """File/directory counts, language breakdown and git metadata."""
        executor = _resolve_or_404(service, reponame)
        counts = _raise_for_failure(
            service.run(reponame, OperationType.COUNT_FILES_AND_DIRECTORIES, executor=executor)
        )
        languages = _raise_for_failure(
            service.run(reponame, OperationType.DETECT_LANGUAGES, executor=executor)
        )
        git = service.run(reponame, OperationType.GIT_METADATA, executor=executor)
        return InventoryResponse(
            repository=reponame,
            counts=counts.data,
            languages=languages.data,
            git=git.data if git.status != "failed" else {"is_git_repository": False},
        )

    @router.get("/repos/{reponame}/oop", response_model=OperationResult)
    def oop_analysis(
        reponame: str, language: str | None = None, symbol: str | None = None
    ) -> OperationResult:
        """OOP structure: types, relationships, polymorphism, encapsulation."""
        executor = _resolve_or_404(service, reponame)
        return _raise_for_failure(
            service.run(
                reponame,
                OperationType.ANALYZE_OOP,
                language=language,
                symbol=symbol,
                executor=executor,
            )
        )

    @router.get("/repos/{reponame}/relationships", response_model=OperationResult)
    def relationships(
        reponame: str,
        language: str | None = None,
        include_calls: bool = False,
        max_nodes: int = 500,
        max_edges: int = 1000,
    ) -> OperationResult:
        """The typed relationship graph (nodes, edges, package dependencies)."""
        executor = _resolve_or_404(service, reponame)
        return _raise_for_failure(
            service.run(
                reponame,
                OperationType.BUILD_RELATIONSHIP_GRAPH,
                language=language,
                arguments={
                    "include_calls": include_calls,
                    "max_nodes": max_nodes,
                    "max_edges": max_edges,
                },
                executor=executor,
            )
        )

    @router.post("/repos/{reponame}/diagrams", response_model=DiagramGenerationResponse)
    def generate_diagrams(reponame: str, body: DiagramRequest) -> DiagramGenerationResponse:
        """Generate Mermaid diagrams (and optionally the Markdown documents)."""
        executor = _resolve_or_404(service, reponame)
        if "sequence" in body.kinds and not body.start_symbol:
            raise HTTPException(
                status_code=400,
                detail="A 'sequence' diagram requires 'start_symbol'.",
            )

        diagrams: list[DiagramSummary] = []
        written: list[str] = []
        warnings: list[str] = []
        duration = 0
        status: Literal["success", "partial", "failed"] = "success"

        for kind in body.kinds:
            result = _raise_for_failure(
                service.run(
                    reponame,
                    DIAGRAM_OPERATIONS[kind],
                    language=body.language,
                    symbol=body.start_symbol,
                    arguments={
                        "write": body.write,
                        "split_by_package": body.split_by_package,
                        "include_external": body.include_external,
                        "max_nodes": body.max_nodes,
                        "max_edges": body.max_edges,
                    },
                    executor=executor,
                )
            )
            duration += result.duration_ms
            warnings.extend(result.warnings)
            written.extend(str(path) for path in result.data.get("written_files", []))
            if result.status == "partial":
                status = "partial"
            for payload in result.data.get("diagrams", []):
                diagrams.append(
                    DiagramSummary(
                        title=payload["title"],
                        kind=payload["kind"],
                        filename=payload["filename"],
                        node_count=payload["node_count"],
                        edge_count=payload["edge_count"],
                        omitted_node_count=len(payload["omitted_nodes"]),
                        omitted_edge_count=payload["omitted_edge_count"],
                        warnings=payload["warnings"],
                        mermaid=payload["mermaid"],
                    )
                )

        documents: list[str] = []
        if body.write_documents:
            from operations.diagram_generator import Diagram

            analysis = executor.analysis(
                languages={body.language.lower()} if body.language else None
            )
            graph = executor.graph(
                languages={body.language.lower()} if body.language else None
            )
            rendered = [
                Diagram(
                    title=summary.title,
                    kind=summary.kind,
                    mermaid=summary.mermaid,
                    filename=summary.filename,
                    node_count=summary.node_count,
                    edge_count=summary.edge_count,
                    warnings=summary.warnings,
                )
                for summary in diagrams
            ]
            # write_bundle re-writes the diagrams alongside the reports so the
            # bundle is self-contained on disk; only the Markdown documents are
            # reported here, since the diagrams are already in written_files.
            documents = [
                str(path)
                for path in service.docs_writer(reponame).write_bundle(
                    analysis, graph, rendered
                )
                if path.suffix == ".md"
            ]

        return DiagramGenerationResponse(
            repository=reponame,
            status=status,
            diagrams=diagrams,
            written_files=written,
            written_documents=documents,
            warnings=warnings,
            duration_ms=duration,
        )

    @router.get("/repos/{reponame}/diagrams", response_model=DiagramListResponse)
    def list_diagrams(reponame: str) -> DiagramListResponse:
        """List diagrams and documents already generated for a repository."""
        try:
            writer = service.docs_writer(reponame)
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(
                status_code=404, detail=f"Repo not staged: {reponame!r}"
            ) from exc
        return DiagramListResponse(
            repository=reponame,
            diagrams=writer.list_diagrams(),
            documents=writer.list_documents(),
            output_directory=str(writer.repo_dir),
        )

    return router


__all__ = [
    "DIAGRAM_OPERATIONS",
    "DiagramGenerationResponse",
    "DiagramListResponse",
    "DiagramRequest",
    "DiagramSummary",
    "InventoryResponse",
    "OperationRequestBody",
    "RepositoryOperationsService",
    "ToolStatusResponse",
    "create_operations_router",
]
