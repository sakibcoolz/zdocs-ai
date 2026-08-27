"""Runs the shipped analysis panel against real API payloads.

``test_operations_ui.py`` pins the API's field names; this pins the other half
of the contract — that ``static/analysis.js`` actually *renders* those payloads
without throwing. A renamed field or a bad property access would otherwise fail
only in a browser, which nothing else in the suite exercises.

The panel runs under Node with a minimal DOM shim (``tests/ui_harness.mjs``)
and a stubbed ``fetch`` fed with payloads captured from the live API. Skipped
when Node is not installed; no browser and no network are ever required.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from conftest import FIXTURES_DIR

PROJECT_ROOT = Path(__file__).resolve().parent
HARNESS = PROJECT_ROOT / "tests" / "ui_harness.mjs"
PANEL = PROJECT_ROOT / "static" / "analysis.js"

pytestmark = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not installed"
)


@pytest.fixture(scope="module")
def payloads(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Capture real API responses for every endpoint the panel calls."""
    tmp_path = tmp_path_factory.mktemp("ui")
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copytree(FIXTURES_DIR / "java_repo", stage / "demo")

    original_stage, original_docs = server.STAGE_DIR, server.GENERATED_DOCS_DIR
    server.STAGE_DIR = stage
    server.GENERATED_DOCS_DIR = tmp_path / "generated-docs"
    try:
        client = TestClient(server.app)
        captured = {
            "/api/operations/tools": client.get("/api/operations/tools").json(),
            "/inventory": client.get("/api/repos/demo/inventory").json(),
            "/oop": client.get("/api/repos/demo/oop").json(),
            "/diagrams": client.post(
                "/api/repos/demo/diagrams",
                json={"kinds": ["class", "inheritance", "dependency"], "write": True},
            ).json(),
        }
    finally:
        server.STAGE_DIR, server.GENERATED_DOCS_DIR = original_stage, original_docs

    path = tmp_path / "payloads.json"
    path.write_text(json.dumps(captured), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def report(payloads: Path) -> dict:
    """Run the panel under Node and return its render report."""
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        ["node", str(HARNESS), str(PANEL), str(payloads)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=PROJECT_ROOT,
    )
    assert result.stdout, f"harness produced no output.\nstderr:\n{result.stderr}"
    parsed = json.loads(result.stdout)
    assert not parsed["failures"], (
        f"analysis panel failed to render: {parsed['failures']}\n"
        f"stderr:\n{result.stderr}"
    )
    return parsed


def test_every_action_renders_without_error(report: dict) -> None:
    assert set(report["actions"]) == {"tools", "inventory", "oop", "diagrams"}
    for name, action in report["actions"].items():
        assert not action["failed"], f"{name}: {action['status']}"
        assert action["cards"] > 0, f"{name} rendered no cards"


def test_panel_calls_the_expected_endpoints(report: dict) -> None:
    calls = {(item["method"], item["path"]) for item in report["requests"]}
    assert ("GET", "/api/operations/tools") in calls
    assert ("GET", "/api/repos/demo/inventory") in calls
    assert ("GET", "/api/repos/demo/oop") in calls
    assert ("POST", "/api/repos/demo/diagrams") in calls


def test_inventory_render_shows_counts_and_languages(report: dict) -> None:
    rendered = report["actions"]["inventory"]
    assert rendered["tables"] >= 2
    assert "java" in rendered["text"]


def test_oop_render_shows_findings_with_confidence(report: dict) -> None:
    rendered = report["actions"]["oop"]
    assert rendered["tables"] >= 4
    text = rendered["text"]
    assert "Drawable" in text or "AbstractShape" in text


def test_diagram_render_degrades_without_the_mermaid_library(report: dict) -> None:
    # Node cannot import the CDN module, which is exactly the offline case:
    # the panel must fall back to showing the diagram source, not blow up.
    rendered = report["actions"]["diagrams"]
    assert not rendered["failed"]
    assert "classDiagram" in rendered["text"]
    assert "could not be loaded" in rendered["text"]
