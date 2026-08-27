"""Exception types shared by the repository-operations layer.

Kept in their own module so every other module in :mod:`operations` can raise
and catch them without importing each other (the executor, the policy and the
analyzers all need them).

All of these are *expected* failure modes: the executor converts them into a
structured :class:`~operations.schemas.OperationResult` with
``status="failed"`` rather than letting a traceback escape to an API caller.
"""

from __future__ import annotations


class OperationError(Exception):
    """Base class for every recoverable repository-operation failure."""


class PolicyViolation(OperationError):
    """An operation, path or command was rejected by the execution policy."""


class PathEscapeError(PolicyViolation):
    """A requested path resolved outside the staged repository root."""


class OperationNotAllowed(PolicyViolation):
    """The requested operation is not enabled by the active profile."""


class CommandNotAllowed(PolicyViolation):
    """The requested executable is not on the policy allowlist."""


class ToolUnavailableError(OperationError):
    """A required external command-line tool is not installed."""


class CommandTimeout(OperationError):
    """An allowlisted command exceeded its wall-clock budget."""


class UnsupportedLanguage(OperationError):
    """No analyzer is registered for the requested language."""
