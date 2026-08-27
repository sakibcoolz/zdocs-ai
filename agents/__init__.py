"""Agent definitions for the ZDocs AI platform.

The platform's design calls for nine logical agents (Repository Planner, File
Analysis, Business Logic, Architecture, API Analysis, Data Analysis,
Documentation, Repository Q&A and Repository Operations). Only the agents that
are actually implemented are registered here — an entry in :data:`AGENT_BUILDERS`
means the agent exists and works, not that it is planned.

Implemented today:

* ``zdocs_assistant`` — the original documentation assistant (``agent.py``).
* ``repository_operations_agent`` — agent 9, this feature.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from google.adk.agents import LlmAgent

from agents.repository_operations_agent import (
    AGENT_NAME as REPOSITORY_OPERATIONS_AGENT,
    build_repository_operations_agent,
)


def _build_zdocs_assistant(repo_dir: str | Path, **kwargs: object) -> LlmAgent:
    """Adapter so the original assistant shares the registry's signature."""
    from agent import build_agent

    return build_agent(stage_dir=repo_dir, model=kwargs.get("model"))  # type: ignore[arg-type]


#: Agent name → builder taking a staged repository directory.
AGENT_BUILDERS: dict[str, Callable[..., LlmAgent]] = {
    "zdocs_assistant": _build_zdocs_assistant,
    REPOSITORY_OPERATIONS_AGENT: build_repository_operations_agent,
}


def available_agents() -> list[str]:
    """Names of the agents that are implemented and registered."""
    return sorted(AGENT_BUILDERS)


def build_agent_by_name(name: str, repo_dir: str | Path, **kwargs: object) -> LlmAgent:
    """Build a registered agent by name for one staged repository."""
    try:
        builder = AGENT_BUILDERS[name]
    except KeyError as exc:
        raise ValueError(
            f"Unknown agent {name!r}. Registered agents: {', '.join(available_agents())}"
        ) from exc
    return builder(repo_dir, **kwargs)


__all__ = [
    "AGENT_BUILDERS",
    "REPOSITORY_OPERATIONS_AGENT",
    "available_agents",
    "build_agent_by_name",
    "build_repository_operations_agent",
]
