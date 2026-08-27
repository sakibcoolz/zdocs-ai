"""Repository operations: deterministic, sandboxed analysis of staged repos.

This package is the engine behind the Repository Operations Agent (agent 9 of
the ZDocs AI platform). It answers structured questions about a staged
repository — what files exist, which classes and interfaces are declared, what
inherits from what, which types implement which interfaces — and renders the
results as Mermaid diagrams.

Layering (nothing below depends on anything above it):

``schemas``            Pydantic contracts shared by every layer.
``errors``             Recoverable failure types.
``policy``             What may run, on which paths; secret redaction.
``command_runner``     Sandboxed subprocess execution of allowlisted tools.
``inventory``          File discovery, counting, language detection.
``symbol_search``      Text search and bounded file reads.
``git_info``           Read-only git metadata.
``languages/``         Per-language analyzers behind one interface.
``oop_analyzer``       Repository-wide analysis and cross-file derivations.
``relationship_graph`` Resolved node/edge graph.
``diagram_generator``  Mermaid rendering with sanitizing and size limits.
``docs_writer``        ``generated-docs/`` output.
``cache``              Deterministic result cache.
``executor``           Validate → dispatch → measure → audit.

The package has no FastAPI and no Google ADK imports: ``api_operations.py`` and
``tools/repository_operations.py`` adapt it to those, so the security-critical
code stays independently testable.
"""

from __future__ import annotations

from operations.errors import (
    CommandNotAllowed,
    CommandTimeout,
    OperationError,
    OperationNotAllowed,
    PathEscapeError,
    PolicyViolation,
    ToolUnavailableError,
    UnsupportedLanguage,
)
from operations.executor import OperationExecutor
from operations.policy import ExecutionPolicy, ExecutionProfile, redact
from operations.schemas import (
    CodeMatch,
    Confidence,
    DetectionMethod,
    OperationRequest,
    OperationResult,
    OperationType,
    RelationType,
    SourceEvidence,
    SymbolType,
    Visibility,
)

__all__ = [
    "CodeMatch",
    "CommandNotAllowed",
    "CommandTimeout",
    "Confidence",
    "DetectionMethod",
    "ExecutionPolicy",
    "ExecutionProfile",
    "OperationError",
    "OperationExecutor",
    "OperationNotAllowed",
    "OperationRequest",
    "OperationResult",
    "OperationType",
    "PathEscapeError",
    "PolicyViolation",
    "RelationType",
    "SourceEvidence",
    "SymbolType",
    "ToolUnavailableError",
    "UnsupportedLanguage",
    "Visibility",
]
