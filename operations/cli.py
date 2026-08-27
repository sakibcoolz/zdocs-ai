"""Command-line entry point for the repository operations layer.

Backs the ``make tools-check`` / ``make analyze-sample`` / ``make
generate-diagrams`` targets and is handy for running an analysis without
starting the web app::

    python -m operations.cli tools
    python -m operations.cli operations
    python -m operations.cli analyze tests/fixtures/python_repo
    python -m operations.cli diagrams tests/fixtures --out generated-docs --docs
    python -m operations.cli validate stage/myrepo --tool ruff

Exit codes: ``0`` success, ``1`` the operation failed (or a validation tool
reported findings), ``2`` bad usage or a missing REQUIRED tool.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from operations.diagram_generator import Diagram
from operations.executor import OperationExecutor
from operations.policy import ExecutionPolicy
from operations.schemas import OperationRequest, OperationType
from operations.tool_detection import format_report, missing_required


def _executor(target: str, docs_root: str | None) -> OperationExecutor:
    """Build an executor for a directory path or a staged repository name."""
    path = Path(target)
    if not path.is_dir():
        from tools.stage_registry import staged_repo_dir

        path = staged_repo_dir(target)
    return OperationExecutor(
        path,
        repository=path.name,
        policy=ExecutionPolicy.repository_analysis(),
        docs_root=docs_root,
    )


def _cmd_tools(args: argparse.Namespace) -> int:
    if args.json:
        from operations.tool_detection import detect_tools

        print(json.dumps([report.as_dict() for report in detect_tools()], indent=2))
    else:
        print(format_report())
    return 2 if missing_required() else 0


def _cmd_operations(args: argparse.Namespace) -> int:
    executor_policy = ExecutionPolicy.repository_analysis()
    operations = sorted(
        operation.value
        for operation in OperationType
        if operation in executor_policy.allowed_operations
        and operation is not OperationType.RUN_STATIC_ANALYSIS
    )
    if args.json:
        print(json.dumps({"profile": executor_policy.profile.value, "operations": operations}, indent=2))
    else:
        print(f"profile: {executor_policy.profile.value}")
        for operation in operations:
            print(f"  {operation}")
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    executor = _executor(args.target, args.out)
    result = executor.execute(
        OperationRequest(
            operation=OperationType.ANALYZE_OOP,
            repository=executor.repository,
            language=args.language,
        )
    )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
        return 0 if result.status != "failed" else 1

    if result.status == "failed":
        print("FAILED:", "; ".join(result.errors), file=sys.stderr)
        return 1

    summary = result.data.get("summary", {})
    print(f"repository: {executor.repository}  ({executor.root})")
    print(f"status:     {result.status} in {result.duration_ms} ms")
    print(f"languages:  {', '.join(result.data.get('languages') or []) or 'none'}")
    print(f"files:      {summary.get('files_analyzed')} analyzed, {summary.get('files_skipped')} skipped")
    print(f"symbols:    {summary.get('symbol_count')}   relationships: {summary.get('relationship_count')}")
    print("\nsymbols by kind:")
    for kind, count in (summary.get("symbols_by_kind") or {}).items():
        print(f"  {kind:<16} {count}")
    print("\nrelationships by type:")
    for relation, count in (summary.get("relationships_by_type") or {}).items():
        print(f"  {relation:<16} {count}")
    polymorphism = result.data.get("polymorphism") or {}
    if polymorphism:
        print("\npolymorphic abstractions:")
        for abstraction, implementers in polymorphism.items():
            print(f"  {abstraction} <- {', '.join(implementers)}")
    for warning in result.warnings[:5]:
        print(f"\nwarning: {warning}")
    return 0


def _cmd_diagrams(args: argparse.Namespace) -> int:
    executor = _executor(args.target, args.out)
    kinds = {
        "class": OperationType.GENERATE_CLASS_DIAGRAM,
        "inheritance": OperationType.GENERATE_INHERITANCE_DIAGRAM,
        "dependency": OperationType.GENERATE_DEPENDENCY_DIAGRAM,
        "component": OperationType.GENERATE_COMPONENT_DIAGRAM,
    }
    produced: list[Diagram] = []
    failed = False
    for name, operation in kinds.items():
        result = executor.execute(
            OperationRequest(
                operation=operation,
                repository=executor.repository,
                language=args.language,
                arguments={"write": True, "split_by_package": args.split},
            )
        )
        if result.status == "failed":
            failed = True
            print(f"{name}: FAILED — {'; '.join(result.errors)}", file=sys.stderr)
            continue
        for payload in result.data.get("diagrams", []):
            diagram = Diagram.model_validate(payload)
            produced.append(diagram)
            print(
                f"{name:<12} {diagram.filename:<40} "
                f"{diagram.node_count} nodes, {diagram.edge_count} edges"
                + (f", {len(diagram.omitted_nodes)} omitted" if diagram.omitted_nodes else "")
            )

    writer = executor.docs_writer()
    if args.docs:
        paths = writer.write_bundle(executor.analysis(), executor.graph(), produced)
        print("\nwrote:")
        for path in paths:
            print(f"  {path}")
    else:
        print(f"\ndiagrams written to: {writer.diagrams_dir}")
    return 1 if failed else 0


def _cmd_validate(args: argparse.Namespace) -> int:
    """Run one approved validation tool inside an isolated container."""
    from operations.sandbox import VALIDATION_COMMANDS, build_sandbox

    root = _executor(args.target, None).root
    sandbox = build_sandbox(image=args.image)
    executor = OperationExecutor(
        root,
        repository=root.name,
        policy=ExecutionPolicy.development_validation(enabled=True),
        sandbox=sandbox,
    )
    if not sandbox.available():
        print(
            f"error: {sandbox.describe()}\n"
            f"The development-validation profile runs project tooling in a "
            f"disposable container and never on the host.",
            file=sys.stderr,
        )
        return 2

    result = executor.execute(
        OperationRequest(
            operation=OperationType.RUN_STATIC_ANALYSIS,
            repository=executor.repository,
            arguments={
                "tool": args.tool,
                "path": args.path,
                "timeout_seconds": args.timeout,
                "memory_mb": args.memory,
                "cpus": args.cpus,
            },
        )
    )
    if args.json:
        print(json.dumps(result.model_dump(mode="json"), indent=2, default=str))
        return 0 if result.status != "failed" else 1

    if result.status == "failed":
        print("FAILED:", "; ".join(result.errors), file=sys.stderr)
        print(f"approved tools: {', '.join(sorted(VALIDATION_COMMANDS))}", file=sys.stderr)
        return 1

    data = result.data
    isolation = data.get("isolation", {})
    print(f"tool:      {data['tool']}  ({' '.join(data['argv'])})")
    print(f"sandbox:   {data['backend']} image={data['image']}")
    print(f"isolation: network={isolation.get('network')}; {isolation.get('filesystem')}")
    print(f"limits:    {data['limits']}")
    print(f"exit code: {data['exit_code']}  passed={data['passed']}")
    if data.get("stdout"):
        print("\n--- stdout ---")
        print(data["stdout"])
    if data.get("stderr"):
        print("\n--- stderr ---")
        print(data["stderr"])
    return 0 if data["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="python -m operations.cli",
        description="Repository operations: tool check, OOP analysis, Mermaid diagrams.",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Log every operation to stderr."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    tools = subparsers.add_parser("tools", help="Report which analysis tools are installed.")
    tools.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    tools.set_defaults(handler=_cmd_tools)

    operations = subparsers.add_parser("operations", help="List permitted operations.")
    operations.add_argument("--json", action="store_true", help="Emit JSON.")
    operations.set_defaults(handler=_cmd_operations)

    analyze = subparsers.add_parser("analyze", help="Run OOP analysis on a repository.")
    analyze.add_argument("target", help="Directory path, or the name of a staged repo.")
    analyze.add_argument("--language", default=None, help="Restrict to one language.")
    analyze.add_argument("--out", default=None, help="generated-docs root.")
    analyze.add_argument("--json", action="store_true", help="Emit the full result as JSON.")
    analyze.set_defaults(handler=_cmd_analyze)

    diagrams = subparsers.add_parser("diagrams", help="Generate Mermaid diagrams.")
    diagrams.add_argument("target", help="Directory path, or the name of a staged repo.")
    diagrams.add_argument("--language", default=None, help="Restrict to one language.")
    diagrams.add_argument("--out", default=None, help="generated-docs root.")
    diagrams.add_argument(
        "--split", action="store_true", help="One class diagram per package."
    )
    diagrams.add_argument(
        "--docs", action="store_true", help="Also write the Markdown report bundle."
    )
    diagrams.set_defaults(handler=_cmd_diagrams)

    validate = subparsers.add_parser(
        "validate",
        help="Run an approved validation tool in an isolated container (needs Docker).",
    )
    validate.add_argument("target", help="Directory path, or the name of a staged repo.")
    validate.add_argument("--tool", default="ruff", help="Approved tool name.")
    validate.add_argument("--path", default=None, help="Repository-relative path to check.")
    validate.add_argument("--image", default=None, help="Container image to run in.")
    validate.add_argument("--timeout", type=float, default=120.0, help="Seconds.")
    validate.add_argument("--memory", type=int, default=512, help="Memory limit in MB.")
    validate.add_argument("--cpus", type=float, default=1.0, help="CPU limit.")
    validate.add_argument("--json", action="store_true", help="Emit the full result as JSON.")
    validate.set_defaults(handler=_cmd_validate)

    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the CLI. Returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
    )
    try:
        return int(args.handler(args))
    except (FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
