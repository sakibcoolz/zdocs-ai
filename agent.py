"""zdocs-ai agent built on the Google Agent Development Kit (ADK).

Defines a single ``LlmAgent`` equipped with the file-reader tool. The agent is
a document-assistant: it can list files staged in the ``stage/`` directory and
read their contents to answer questions, summarize, or extract information.
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from google.adk.agents import LlmAgent

from tools.file_reader import FileReaderTool

load_dotenv()

# The model is resolved from the environment so the same code runs locally and
# in CI. Defaults to a Gemini model; override with ADK_MODEL.
MODEL = os.getenv("ADK_MODEL", "gemini-2.5-flash")

SYSTEM_INSTRUCTION = """\
You are zdocs, a documentation assistant. You help users understand the files \
staged in the `stage/` directory.

Rules:
- Use `list_files` to see what is available before reading.
- Use `read_file` to fetch a specific file's contents when answering.
- Never claim to know a file's contents without actually reading it.
- Quote the relevant text from files when it supports your answer.
- If a requested file is not in the stage directory, say so and list what IS \
available instead of guessing.
"""


def build_agent(stage_dir: str | Path | None = None, model: str | None = None) -> LlmAgent:
    """Construct the zdocs agent.

    Args:
        stage_dir: Root directory the file-reader tool is restricted to.
            Defaults to the ``stage/`` directory next to this file.
        model: LLM model string. Defaults to ``ADK_MODEL`` env var / Gemini.

    Returns:
        A configured ``LlmAgent`` ready to run via a ``Runner``.
    """
    if stage_dir is None:
        # Resolve relative to this file so it works regardless of CWD.
        stage_dir = Path(__file__).resolve().parent / "stage"

    return LlmAgent(
        name="zdocs_assistant",
        model=model or MODEL,
        description="Documentation assistant with access to staged files.",
        instruction=SYSTEM_INSTRUCTION,
        tools=FileReaderTool(stage_dir=stage_dir),
    )
