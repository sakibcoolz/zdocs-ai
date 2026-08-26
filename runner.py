"""Interactive CLI runner for the zdocs-ai agent.

Usage:
    python runner.py            # interactive chat loop
    python runner.py "prompt"   # one-shot, print answer and exit

The runner uses an in-memory session/artifact service, so it requires no
external infrastructure and is ideal for local development and demos.
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv
from google.adk.artifacts import InMemoryArtifactService
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agent import build_agent

load_dotenv()

APP_NAME = "zdocs-ai"
USER_ID = "local-user"
SESSION_ID = "local-session"


def run_turn(runner: Runner, *, user_id: str, session_id: str, prompt: str) -> str:
    """Run one turn against ``runner`` and return the final response text."""
    content = types.Content(role="user", parts=[types.Part(text=prompt)])
    for event in runner.run(
        user_id=user_id,
        session_id=session_id,
        new_message=content,
    ):
        if event.is_final_response() and event.content and event.content.parts:
            texts = [part.text for part in event.content.parts if part.text]
            if texts:
                return "\n".join(texts)
    return "(no response)"


def run_once(prompt: str) -> None:
    """Run a single prompt and print the final assistant text."""
    session_service = InMemorySessionService()
    artifact_service = InMemoryArtifactService()

    runner = Runner(
        app_name=APP_NAME,
        agent=build_agent(),
        session_service=session_service,
        artifact_service=artifact_service,
        auto_create_session=True,
    )

    print(f"\nYou: {prompt}\n")
    text = run_turn(runner, user_id=USER_ID, session_id=SESSION_ID, prompt=prompt)
    print(text if text != "(no response)" else "(no final response produced)")


def repl() -> None:
    """Run an interactive read-eval-print loop."""
    session_service = InMemorySessionService()
    artifact_service = InMemoryArtifactService()
    runner = Runner(
        app_name=APP_NAME,
        agent=build_agent(),
        session_service=session_service,
        artifact_service=artifact_service,
        auto_create_session=True,
    )

    print("zdocs-ai assistant. Type 'quit' or 'exit' to leave.\n")
    while True:
        try:
            prompt = input("You> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if prompt.lower() in {"quit", "exit", "q"}:
            break
        if not prompt:
            continue

        print("zdocs> ", end="", flush=True)
        text = run_turn(runner, user_id=USER_ID, session_id=SESSION_ID, prompt=prompt)
        print(text)
        print()


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run_once(" ".join(sys.argv[1:]))
    else:
        repl()
