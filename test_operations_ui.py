"""Contract tests between the analysis UI and the operations API.

``static/analysis.js`` reads specific fields out of specific endpoints. Those
reads are invisible to the Python type checker and to every other test, so a
rename in a Pydantic model would break the page silently. These tests pin the
exact JSON paths the page depends on, and check that the shipped page actually
wires up the elements it drives.

No browser is required: the page's data contract is asserted against real API
responses, and its markup against the served HTML.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import server
from conftest import FIXTURES_DIR

STATIC_DIR = Path(__file__).resolve().parent / "static"


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    stage = tmp_path / "stage"
    stage.mkdir()
    shutil.copytree(FIXTURES_DIR / "java_repo", stage / "demo")
    monkeypatch.setattr(server, "STAGE_DIR", stage)
    monkeypatch.setattr(server, "GENERATED_DOCS_DIR", tmp_path / "generated-docs")
    server._runners.clear()
    return TestClient(server.app)


def at(payload: Any, path: str) -> Any:
    """Read a dotted path, treating ``[]`` as "first element of a list"."""
    current = payload
    for part in path.split("."):
        if part == "[]":
            assert isinstance(current, list), f"expected a list at {path!r}"
            assert current, f"expected a non-empty list at {path!r}"
            current = current[0]
            continue
        assert isinstance(current, dict), f"expected an object before {part!r} in {path!r}"
        assert part in current, f"missing field {part!r} in {path!r}"
        current = current[part]
    return current


# --------------------------------------------------------------------------
# Page wiring
# --------------------------------------------------------------------------


def test_page_loads_both_scripts(client: TestClient) -> None:
    html = client.get("/").text
    assert '<script src="/app.js"></script>' in html
    assert '<script src="/analysis.js"></script>' in html


def test_scripts_are_served(client: TestClient) -> None:
    for path in ("/app.js", "/analysis.js", "/style.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.content


def test_every_element_the_panel_drives_exists_in_the_markup(client: TestClient) -> None:
    html = client.get("/").text
    script = (STATIC_DIR / "analysis.js").read_text(encoding="utf-8")
    referenced = set(re.findall(r'\bel\("([a-z0-9-]+)"\)', script))
    assert referenced, "expected analysis.js to reference elements by id"
    missing = [name for name in sorted(referenced) if f'id="{name}"' not in html]
    assert not missing, f"analysis.js drives elements absent from index.html: {missing}"


def test_chat_panel_elements_still_exist(client: TestClient) -> None:
    # The analysis tab must not have displaced the original chat UI.
    html = client.get("/").text
    for element_id in ("chat-log", "chat-form", "chat-input", "chat-send", "repo-list"):
        assert f'id="{element_id}"' in html


def test_app_js_publishes_the_repo_selection_event(client: TestClient) -> None:
    script = (STATIC_DIR / "app.js").read_text(encoding="utf-8")
    assert 'CustomEvent("repo-selected"' in script
    panel = (STATIC_DIR / "analysis.js").read_text(encoding="utf-8")
    assert 'addEventListener("repo-selected"' in panel


def test_panel_does_not_leak_globals(client: TestClient) -> None:
    # analysis.js is an IIFE so it cannot collide with app.js's module scope.
    script = (STATIC_DIR / "analysis.js").read_text(encoding="utf-8")
    assert script.lstrip().startswith('"use strict";')
    assert "(() => {" in script


# --------------------------------------------------------------------------
# Data contract: /inventory
# --------------------------------------------------------------------------


def test_inventory_shape(client: TestClient) -> None:
    body = client.get("/api/repos/demo/inventory").json()
    for path in (
        "counts.file_count",
        "counts.directory_count",
        "counts.total_bytes",
        "languages.languages",
        "languages.unclassified_files",
        "git.is_git_repository",
    ):
        at(body, path)
    entry = at(body, "languages.languages.[]")
    for field in ("language", "files", "percent_of_classified_files", "supported"):
        assert field in entry, field


def test_inventory_reports_a_reason_when_git_is_absent(client: TestClient) -> None:
    # The page shows `git.reason` whenever is_git_repository is false.
    body = client.get("/api/repos/demo/inventory").json()
    assert body["git"]["is_git_repository"] is False
    assert body["git"].get("reason")


# --------------------------------------------------------------------------
# Data contract: /oop
# --------------------------------------------------------------------------


def test_oop_shape(client: TestClient) -> None:
    body = client.get("/api/repos/demo/oop").json()
    for path in (
        "data.summary.files_analyzed",
        "data.summary.symbol_count",
        "data.summary.relationship_count",
        "data.summary.symbols_by_kind",
        "data.summary.relationships_by_type",
        "data.languages",
        "data.polymorphism",
        "data.encapsulation.members_by_visibility",
        "data.encapsulation.public_field_count",
        "warnings",
        "truncated",
    ):
        at(body, path)


def test_oop_findings_carry_provenance(client: TestClient) -> None:
    body = client.get("/api/repos/demo/oop").json()
    match = at(body, "matches.[]")
    # The page renders each of these in the findings table; dropping any of
    # them would turn an evidence row into an unsourced claim.
    for field in (
        "symbol",
        "symbol_type",
        "relationship",
        "target_symbol",
        "file_path",
        "line",
        "detection_method",
        "confidence",
    ):
        assert field in match, field
    assert match["confidence"] in ("high", "medium", "low")


# --------------------------------------------------------------------------
# Data contract: /diagrams
# --------------------------------------------------------------------------


def test_diagram_shape(client: TestClient) -> None:
    body = client.post(
        "/api/repos/demo/diagrams",
        json={"kinds": ["class", "inheritance"], "write": True},
    ).json()
    for path in ("repository", "status", "diagrams", "written_files", "warnings"):
        at(body, path)
    diagram = at(body, "diagrams.[]")
    for field in (
        "title",
        "kind",
        "filename",
        "mermaid",
        "node_count",
        "edge_count",
        "omitted_node_count",
        "omitted_edge_count",
        "warnings",
    ):
        assert field in diagram, field
    assert diagram["mermaid"].startswith("classDiagram")


def test_diagram_kinds_offered_by_the_page_are_all_accepted(client: TestClient) -> None:
    html = client.get("/").text
    kinds = re.findall(r'<input type="checkbox" value="([a-z]+)"', html)
    assert kinds, "expected the page to offer diagram kinds"
    response = client.post("/api/repos/demo/diagrams", json={"kinds": kinds, "write": False})
    assert response.status_code == 200
    assert {diagram["kind"] for diagram in response.json()["diagrams"]} <= set(kinds)


# --------------------------------------------------------------------------
# Data contract: /operations/tools
# --------------------------------------------------------------------------


def test_tools_shape(client: TestClient) -> None:
    body = client.get("/api/operations/tools").json()
    at(body, "supported_languages")
    tool = at(body, "tools.[]")
    for field in ("name", "level", "status", "path", "fallback"):
        assert field in tool, field
    assert tool["status"] in (
        "installed",
        "missing_with_fallback",
        "missing_no_fallback",
    )


# --------------------------------------------------------------------------
# Error contract
# --------------------------------------------------------------------------


def test_errors_carry_a_detail_field_for_the_page_to_show(client: TestClient) -> None:
    # The page reads `body.detail` for every failure.
    response = client.get("/api/repos/absent/inventory")
    assert response.status_code == 404
    assert response.json()["detail"]

    response = client.post(
        "/api/repos/demo/operations",
        json={"operation": "read_file_range", "file_path": "../../etc/passwd"},
    )
    assert response.status_code == 403
    assert response.json()["detail"]
