"""Offline tests for the Repository Operations HTTP API.

No network, no LLM: the staged repository is a fixture copied into a temporary
stage directory, and ``server.STAGE_DIR`` / ``server.GENERATED_DOCS_DIR`` are
monkeypatched — the same seam the existing ``test_server.py`` uses.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import server
from conftest import FIXTURES_DIR


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    stage = tmp_path / "stage"
    stage.mkdir()
    for name in ("python_repo", "go_repo", "java_repo"):
        shutil.copytree(FIXTURES_DIR / name, stage / name)
    monkeypatch.setattr(server, "STAGE_DIR", stage)
    monkeypatch.setattr(server, "GENERATED_DOCS_DIR", tmp_path / "generated-docs")
    server._runners.clear()
    return TestClient(server.app)


# --------------------------------------------------------------------------
# Existing routes still work
# --------------------------------------------------------------------------


def test_existing_repo_listing_is_unaffected(client: TestClient) -> None:
    assert client.get("/api/repos").json() == {
        "repos": ["go_repo", "java_repo", "python_repo"]
    }


def test_existing_chat_route_still_gates_on_staging(client: TestClient) -> None:
    assert client.post("/api/repos/absent/chat", json={"message": "hi"}).status_code == 404


# --------------------------------------------------------------------------
# Discovery routes
# --------------------------------------------------------------------------


def test_lists_permitted_operations(client: TestClient) -> None:
    body = client.get("/api/operations").json()
    assert body["profile"] == "repository_analysis"
    assert "analyze_oop" in body["operations"]
    assert "run_static_analysis" not in body["operations"]


def test_reports_tool_availability(client: TestClient) -> None:
    body = client.get("/api/operations/tools").json()
    names = {tool["name"] for tool in body["tools"]}
    assert names == {"rg", "git", "tree-sitter", "ast-grep", "ctags"}
    assert body["missing_required"] == []
    assert body["supported_languages"] == [
        "go",
        "java",
        "javascript",
        "python",
        "typescript",
    ]


# --------------------------------------------------------------------------
# Inventory
# --------------------------------------------------------------------------


def test_inventory(client: TestClient) -> None:
    response = client.get("/api/repos/python_repo/inventory")
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == "python_repo"
    assert body["counts"]["file_count"] == 2
    assert body["languages"]["primary_language"] == "python"
    assert body["git"]["is_git_repository"] is False


def test_inventory_unknown_repo_is_404(client: TestClient) -> None:
    assert client.get("/api/repos/absent/inventory").status_code == 404


def test_inventory_rejects_traversing_repo_names(client: TestClient) -> None:
    assert client.get("/api/repos/..%2F..%2Fetc/inventory").status_code == 404


# --------------------------------------------------------------------------
# Operations
# --------------------------------------------------------------------------


def test_run_operation_returns_a_typed_result(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={"operation": "find_class", "symbol": "Circle"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "success"
    assert body["operation"] == "find_class"
    assert body["repository"] == "python_repo"
    assert body["matches"][0]["symbol"] == "Circle"
    assert body["matches"][0]["confidence"] == "high"
    assert body["matches"][0]["detection_method"] == "python_ast"
    assert body["evidence"]
    assert "duration_ms" in body


def test_run_operation_rejects_an_unknown_operation(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations", json={"operation": "rm -rf /"}
    )
    assert response.status_code == 422


def test_run_operation_refuses_static_analysis(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations", json={"operation": "run_static_analysis"}
    )
    assert response.status_code == 403


def test_run_operation_rejects_path_traversal(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={"operation": "read_file_range", "file_path": "../../../etc/passwd"},
    )
    assert response.status_code == 403
    assert "traversal" in response.json()["detail"].lower()


def test_run_operation_rejects_absolute_paths(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={"operation": "read_file_range", "file_path": "/etc/passwd"},
    )
    assert response.status_code == 403


def test_run_operation_invalid_argument_is_400(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={"operation": "find_class", "arguments": {"limit": "lots"}},
    )
    assert response.status_code == 400


def test_run_operation_unknown_repo_is_404(client: TestClient) -> None:
    response = client.post(
        "/api/repos/absent/operations", json={"operation": "detect_languages"}
    )
    assert response.status_code == 404


def test_errors_never_expose_a_stack_trace(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={"operation": "read_file_range", "file_path": "../secrets"},
    )
    detail = response.json()["detail"]
    assert "Traceback" not in detail
    assert "File \"" not in detail


def test_read_file_range_through_the_api(client: TestClient) -> None:
    response = client.post(
        "/api/repos/python_repo/operations",
        json={
            "operation": "read_file_range",
            "file_path": "shapes.py",
            "arguments": {"start_line": 1, "end_line": 1},
        },
    )
    assert response.status_code == 200
    assert response.json()["data"]["total_lines"] > 1


# --------------------------------------------------------------------------
# OOP and relationships
# --------------------------------------------------------------------------


def test_oop_endpoint(client: TestClient) -> None:
    body = client.get("/api/repos/java_repo/oop").json()
    assert body["status"] == "success"
    assert body["data"]["summary"]["files_analyzed"] == 2
    assert "Drawable" in body["data"]["polymorphism"]


def test_oop_endpoint_accepts_a_language_filter(client: TestClient) -> None:
    body = client.get("/api/repos/java_repo/oop?language=python").json()
    assert body["data"]["summary"]["files_analyzed"] == 0


def test_relationships_endpoint(client: TestClient) -> None:
    body = client.get("/api/repos/go_repo/relationships").json()
    assert body["data"]["stats"]["node_count"] > 0
    edges = {
        (edge["source_name"], edge["target_name"], edge["relation"])
        for edge in body["data"]["edges"]
    }
    assert ("MemoryStore", "Store", "IMPLEMENTS") in edges


def test_relationships_endpoint_can_include_calls(client: TestClient) -> None:
    without = client.get("/api/repos/go_repo/relationships").json()
    with_calls = client.get("/api/repos/go_repo/relationships?include_calls=true").json()
    assert with_calls["data"]["stats"]["edge_count"] > without["data"]["stats"]["edge_count"]


# --------------------------------------------------------------------------
# Diagrams
# --------------------------------------------------------------------------


def test_generate_diagrams(client: TestClient, tmp_path: Path) -> None:
    response = client.post(
        "/api/repos/java_repo/diagrams",
        json={"kinds": ["class", "inheritance"], "write": True},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["repository"] == "java_repo"
    assert {diagram["kind"] for diagram in body["diagrams"]} == {"class", "inheritance"}
    assert all(
        diagram["mermaid"].startswith("classDiagram") for diagram in body["diagrams"]
    )
    assert len(body["written_files"]) == 2
    for path in body["written_files"]:
        assert Path(path).exists()
        assert (tmp_path / "generated-docs") in Path(path).parents


def test_generate_diagrams_without_writing(client: TestClient) -> None:
    body = client.post(
        "/api/repos/java_repo/diagrams", json={"kinds": ["class"], "write": False}
    ).json()
    assert body["written_files"] == []
    assert body["diagrams"][0]["mermaid"]


def test_generate_diagrams_can_write_the_document_bundle(client: TestClient) -> None:
    body = client.post(
        "/api/repos/java_repo/diagrams",
        json={"kinds": ["class"], "write": True, "write_documents": True},
    ).json()
    written = {Path(path).name for path in body["written_documents"]}
    assert {
        "OOP_ANALYSIS.md",
        "CLASS_CATALOG.md",
        "INTERFACE_IMPLEMENTATIONS.md",
        "FUNCTION_CALL_GRAPH.md",
    } <= written


def test_returned_diagram_kind_matches_the_requested_kind(client: TestClient) -> None:
    # A caller must be able to correlate a returned diagram with its request.
    for kind in ("class", "inheritance", "dependency", "component"):
        body = client.post(
            "/api/repos/java_repo/diagrams", json={"kinds": [kind], "write": False}
        ).json()
        assert {diagram["kind"] for diagram in body["diagrams"]} == {kind}


def test_sequence_diagram_requires_a_start_symbol(client: TestClient) -> None:
    response = client.post(
        "/api/repos/java_repo/diagrams", json={"kinds": ["sequence"]}
    )
    assert response.status_code == 400
    assert "start_symbol" in response.json()["detail"]


def test_diagram_request_validates_the_kind(client: TestClient) -> None:
    response = client.post("/api/repos/java_repo/diagrams", json={"kinds": ["hairball"]})
    assert response.status_code == 422


def test_diagram_request_validates_limits(client: TestClient) -> None:
    response = client.post(
        "/api/repos/java_repo/diagrams", json={"kinds": ["class"], "max_nodes": 0}
    )
    assert response.status_code == 422


def test_list_diagrams_is_empty_before_generation(client: TestClient) -> None:
    body = client.get("/api/repos/python_repo/diagrams").json()
    assert body["diagrams"] == []
    assert body["documents"] == []
    assert body["repository"] == "python_repo"


def test_list_diagrams_after_generation(client: TestClient) -> None:
    client.post(
        "/api/repos/python_repo/diagrams",
        json={"kinds": ["class", "dependency"], "write": True},
    )
    body = client.get("/api/repos/python_repo/diagrams").json()
    filenames = {entry["filename"] for entry in body["diagrams"]}
    assert filenames == {"class-diagram.mmd", "package-dependency.mmd"}
    assert all(entry["bytes"] > 0 for entry in body["diagrams"])


def test_list_diagrams_unknown_repo_is_404(client: TestClient) -> None:
    assert client.get("/api/repos/absent/diagrams").status_code == 404


def test_generated_docs_stay_out_of_the_stage_directory(client: TestClient) -> None:
    client.post("/api/repos/python_repo/diagrams", json={"kinds": ["class"], "write": True})
    staged = server.STAGE_DIR / "python_repo"
    assert sorted(path.name for path in staged.iterdir()) == ["registry.py", "shapes.py"]


def test_openapi_document_includes_the_new_routes(client: TestClient) -> None:
    paths = client.get("/openapi.json").json()["paths"]
    assert "/api/repos/{reponame}/operations" in paths
    assert "/api/repos/{reponame}/inventory" in paths
    assert "/api/repos/{reponame}/oop" in paths
    assert "/api/repos/{reponame}/relationships" in paths
    assert "/api/repos/{reponame}/diagrams" in paths
    # Pre-existing routes must still be documented.
    assert "/api/repos/from-url" in paths
    assert "/api/repos/{reponame}/chat" in paths
