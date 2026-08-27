"""Google ADK tools exposing approved repository operations to agents.

Agents call *structured* tools — ``find_class``, ``find_implementations``,
``generate_class_diagram`` — and never compose shell text. Each tool below is a
thin adapter: it builds an :class:`~operations.schemas.OperationRequest`, runs
it through :class:`~operations.executor.OperationExecutor` (where the policy,
sandbox and audit log live) and returns a compact JSON-serialisable dict.

Results deliberately carry *evidence* — file paths, line numbers, detection
method, confidence — and not conclusions. Interpreting that evidence is the job
of the Business Logic and Architecture agents.

The tool surface mirrors :mod:`tools.file_reader`: a factory bound to one
directory, so the model only ever supplies a symbol name or a relative path and
never controls the repository root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from google.adk.tools import FunctionTool

from operations.cache import content_fingerprint
from operations.executor import OperationExecutor
from operations.inventory import discover_files
from operations.policy import ExecutionPolicy
from operations.schemas import OperationRequest, OperationType

#: Matches/evidence returned to the model. The full result is still available
#: through the HTTP API; the tool response is trimmed to protect the context
#: window, exactly as ``read_file_with_limit`` does for file contents.
MAX_TOOL_MATCHES = 40
MAX_TOOL_EVIDENCE = 10
MAX_TOOL_LIST_ITEMS = 50


class ExecutorProvider:
    """Supplies an :class:`OperationExecutor`, reusing it while the repo is unchanged.

    An executor memoizes its repository analysis, which makes a burst of agent
    tool calls fast — but a stale executor would keep answering from a parse of
    files that have since changed. The provider keys the cached executor on a
    content fingerprint (path, size, mtime of every discovered file), so a
    staged repository that is re-staged or edited transparently gets a fresh
    analysis while an unchanged one is never re-parsed.
    """

    def __init__(
        self,
        repo_dir: str | Path,
        *,
        policy: ExecutionPolicy | None = None,
        repository: str = "",
        docs_root: str | Path | None = None,
    ) -> None:
        self.repo_dir = Path(repo_dir)
        self.policy = policy or ExecutionPolicy.repository_analysis()
        self.repository = repository or self.repo_dir.name
        self.docs_root = docs_root
        self._executor: OperationExecutor | None = None
        self._fingerprint: str | None = None

    def get(self) -> OperationExecutor:
        """Return a current executor, rebuilding it if the repository changed."""
        candidate = self._executor or self._build()
        files, _, _ = discover_files(candidate.root, candidate.policy, candidate.runner)
        fingerprint = content_fingerprint(candidate.root, [file.path for file in files])
        if self._executor is None or fingerprint != self._fingerprint:
            self._executor = self._build()
            self._fingerprint = fingerprint
        return self._executor

    def _build(self) -> OperationExecutor:
        return OperationExecutor(
            self.repo_dir,
            repository=self.repository,
            policy=self.policy,
            docs_root=self.docs_root,
        )


def _trim(value: Any) -> Any:
    """Shrink oversized lists inside an operation payload for the model."""
    if isinstance(value, list):
        if len(value) > MAX_TOOL_LIST_ITEMS:
            return [_trim(item) for item in value[:MAX_TOOL_LIST_ITEMS]] + [
                {"_omitted_items": len(value) - MAX_TOOL_LIST_ITEMS}
            ]
        return [_trim(item) for item in value]
    if isinstance(value, dict):
        return {key: _trim(item) for key, item in value.items()}
    return value


def run_operation(
    provider: ExecutorProvider,
    operation: OperationType,
    **fields: Any,
) -> dict[str, Any]:
    """Execute one operation and return a compact, model-friendly dict."""
    executor = provider.get()
    arguments = dict(fields.pop("arguments", {}) or {})
    request = OperationRequest(
        operation=operation,
        repository=executor.repository,
        language=fields.pop("language", None) or None,
        symbol=fields.pop("symbol", None) or None,
        file_path=fields.pop("file_path", None) or None,
        arguments=arguments,
    )
    result = executor.execute(request)
    payload = result.model_dump(mode="json")
    return {
        "status": payload["status"],
        "operation": payload["operation"],
        "repository": payload["repository"],
        "match_count": len(result.matches),
        "matches": _trim(payload["matches"][:MAX_TOOL_MATCHES]),
        "evidence": _trim(payload["evidence"][:MAX_TOOL_EVIDENCE]),
        "data": _trim(payload["data"]),
        "warnings": payload["warnings"][:10],
        "errors": payload["errors"][:10],
        "truncated": payload["truncated"],
        "duration_ms": payload["duration_ms"],
    }


def RepositoryOperationsTool(  # noqa: N802 - factory, matches FileReaderTool
    repo_dir: str | Path,
    *,
    policy: ExecutionPolicy | None = None,
    repository: str = "",
    docs_root: str | Path | None = None,
) -> list[FunctionTool]:
    """Build the ADK tool set bound to one staged repository directory.

    Args:
        repo_dir: The staged repository the tools may inspect. Every path is
            resolved inside it; the model never supplies a root.
        policy: Execution policy. Defaults to the read-only analysis profile.
        repository: Name used in results and in ``generated-docs/<name>/``.
        docs_root: Where generated diagrams are written.

    Returns:
        ``FunctionTool`` instances ready to pass to ``LlmAgent(tools=...)``.
    """
    provider = ExecutorProvider(
        repo_dir, policy=policy, repository=repository, docs_root=docs_root
    )

    def list_repository_files(subdir: str = "", language: str = "", limit: int = 500) -> dict:
        """List files in the repository with size and detected language.

        Args:
            subdir: Optional repository-relative directory to restrict the listing to.
            language: Optional language filter, e.g. "python", "go", "java".
            limit: Maximum number of files to return.
        """
        return run_operation(
            provider,
            OperationType.LIST_REPOSITORY_FILES,
            language=language,
            arguments={"subdir": subdir, "limit": limit},
        )

    def count_files_and_directories() -> dict:
        """Count files, directories and bytes in the repository."""
        return run_operation(provider, OperationType.COUNT_FILES_AND_DIRECTORIES)

    def detect_languages() -> dict:
        """Report which programming languages the repository uses, and which are analyzable."""
        return run_operation(provider, OperationType.DETECT_LANGUAGES)

    def find_class(name: str = "", language: str = "", exact: bool = False) -> dict:
        """Find declared classes, structs, records and abstract classes.

        Args:
            name: Class name to look for. Empty returns every class found.
            language: Optional language filter.
            exact: Require an exact name match instead of a substring match.
        """
        return run_operation(
            provider,
            OperationType.FIND_CLASS,
            symbol=name,
            language=language,
            arguments={"exact": exact},
        )

    def find_interface(name: str = "", language: str = "", exact: bool = False) -> dict:
        """Find declared interfaces (and Python Protocols).

        Args:
            name: Interface name to look for. Empty returns every interface found.
            language: Optional language filter.
            exact: Require an exact name match instead of a substring match.
        """
        return run_operation(
            provider,
            OperationType.FIND_INTERFACE,
            symbol=name,
            language=language,
            arguments={"exact": exact},
        )

    def find_function(name: str = "", language: str = "", exact: bool = False) -> dict:
        """Find top-level functions.

        Args:
            name: Function name to look for. Empty returns every function found.
            language: Optional language filter.
            exact: Require an exact name match instead of a substring match.
        """
        return run_operation(
            provider,
            OperationType.FIND_FUNCTION,
            symbol=name,
            language=language,
            arguments={"exact": exact},
        )

    def find_method(name: str = "", language: str = "", exact: bool = False) -> dict:
        """Find methods and constructors declared on a type.

        Args:
            name: Method name to look for. Empty returns every method found.
            language: Optional language filter.
            exact: Require an exact name match instead of a substring match.
        """
        return run_operation(
            provider,
            OperationType.FIND_METHOD,
            symbol=name,
            language=language,
            arguments={"exact": exact},
        )

    def find_symbol(symbol: str, language: str = "") -> dict:
        """Find any declaration matching a symbol name.

        Falls back to text search when nothing is declared under that name; such
        results are returned with low confidence and marked as candidates.

        Args:
            symbol: Symbol name to look for.
            language: Optional language filter.
        """
        return run_operation(
            provider, OperationType.FIND_SYMBOL, symbol=symbol, language=language
        )

    def find_references(symbol: str, language: str = "") -> dict:
        """Find places that reference a symbol, with file and line evidence.

        Args:
            symbol: Symbol name to look for.
            language: Optional language filter.
        """
        return run_operation(
            provider, OperationType.FIND_REFERENCES, symbol=symbol, language=language
        )

    def find_implementations(symbol: str = "") -> dict:
        """Find types implementing an interface or abstract base.

        Args:
            symbol: Interface or implementing type name. Empty returns all
                implementation relationships in the repository.
        """
        return run_operation(provider, OperationType.FIND_IMPLEMENTATIONS, symbol=symbol)

    def find_inheritance(symbol: str = "") -> dict:
        """Find inheritance relationships involving a type.

        Args:
            symbol: Type name. Empty returns all inheritance relationships.
        """
        return run_operation(provider, OperationType.FIND_INHERITANCE, symbol=symbol)

    def find_imports(symbol: str = "") -> dict:
        """Find import/dependency relationships.

        Args:
            symbol: Module or package name. Empty returns all imports.
        """
        return run_operation(provider, OperationType.FIND_IMPORTS, symbol=symbol)

    def find_calls(symbol: str = "") -> dict:
        """Find function and method call relationships.

        Args:
            symbol: Caller or callee name. Empty returns all recorded calls.
        """
        return run_operation(provider, OperationType.FIND_CALLS, symbol=symbol)

    def read_file_range(file_path: str, start_line: int = 1, end_line: int = 0) -> dict:
        """Read a bounded range of lines from a repository file.

        Args:
            file_path: Repository-relative path. Absolute paths and ".." are rejected.
            start_line: First line to read, 1-based.
            end_line: Last line to read. 0 means "to the end of the file".
        """
        return run_operation(
            provider,
            OperationType.READ_FILE_RANGE,
            file_path=file_path,
            arguments={"start_line": start_line, "end_line": end_line or None},
        )

    def analyze_oop(language: str = "", symbol: str = "") -> dict:
        """Analyze OOP structure: types, inheritance, implementations, encapsulation.

        Args:
            language: Optional language filter.
            symbol: Optional type name to focus the analysis on.
        """
        return run_operation(
            provider, OperationType.ANALYZE_OOP, language=language, symbol=symbol
        )

    def build_relationship_graph(include_calls: bool = False, language: str = "") -> dict:
        """Build the typed relationship graph of the repository.

        Args:
            include_calls: Include CALLS edges (large; needed for call graphs).
            language: Optional language filter.
        """
        return run_operation(
            provider,
            OperationType.BUILD_RELATIONSHIP_GRAPH,
            language=language,
            arguments={"include_calls": include_calls},
        )

    def generate_class_diagram(write: bool = True, split_by_package: bool = False) -> dict:
        """Generate a Mermaid class diagram of the repository.

        Args:
            write: Also write the diagram to generated-docs/<repo>/diagrams/.
            split_by_package: Produce one diagram per package instead of one large one.
        """
        return run_operation(
            provider,
            OperationType.GENERATE_CLASS_DIAGRAM,
            arguments={"write": write, "split_by_package": split_by_package},
        )

    def generate_inheritance_diagram(write: bool = True) -> dict:
        """Generate a Mermaid inheritance/implementation diagram.

        Args:
            write: Also write the diagram to generated-docs/<repo>/diagrams/.
        """
        return run_operation(
            provider,
            OperationType.GENERATE_INHERITANCE_DIAGRAM,
            arguments={"write": write},
        )

    def generate_dependency_diagram(write: bool = True, include_external: bool = False) -> dict:
        """Generate a Mermaid package-dependency diagram.

        Args:
            write: Also write the diagram to generated-docs/<repo>/diagrams/.
            include_external: Include third-party/standard-library modules.
        """
        return run_operation(
            provider,
            OperationType.GENERATE_DEPENDENCY_DIAGRAM,
            arguments={"write": write, "include_external": include_external},
        )

    def git_metadata() -> dict:
        """Read git metadata: HEAD, branch, remotes, recent commits, contributors."""
        return run_operation(provider, OperationType.GIT_METADATA)

    return [
        FunctionTool(function)
        for function in (
            list_repository_files,
            count_files_and_directories,
            detect_languages,
            find_class,
            find_interface,
            find_function,
            find_method,
            find_symbol,
            find_references,
            find_implementations,
            find_inheritance,
            find_imports,
            find_calls,
            read_file_range,
            analyze_oop,
            build_relationship_graph,
            generate_class_diagram,
            generate_inheritance_diagram,
            generate_dependency_diagram,
            git_metadata,
        )
    ]


__all__ = [
    "MAX_TOOL_EVIDENCE",
    "MAX_TOOL_LIST_ITEMS",
    "MAX_TOOL_MATCHES",
    "ExecutorProvider",
    "RepositoryOperationsTool",
    "run_operation",
]
