"""Tests for the development-validation sandbox.

Running a repository's own tests and linters executes untrusted code, so this
is the highest-risk surface in the project. The tests split accordingly:

* **Hardening** (no Docker needed) — the ``docker run`` argument vector *is*
  the security boundary, and :meth:`DockerSandbox.docker_argv` is pure, so
  every flag is asserted directly. These run everywhere.
* **Policy** (no Docker needed) — only one narrowly scoped policy may launch a
  container at all, and it must refuse every known escape.
* **Isolation** (Docker required) — the properties above verified for real
  against a running daemon, then skipped when one is absent.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from operations.errors import CommandNotAllowed, PolicyViolation
from operations.executor import OperationExecutor
from operations.policy import (
    DEFAULT_VALIDATION_TOOLS,
    DOCKER_ALLOWED_SUBCOMMANDS,
    ExecutionPolicy,
)
from operations.sandbox import (
    SANDBOX_GID,
    SANDBOX_UID,
    VALIDATION_COMMANDS,
    WORKSPACE,
    DockerSandbox,
    SandboxLimits,
    SandboxUnavailable,
    UnavailableSandbox,
    ValidationCommand,
    build_sandbox,
    resolve_validation_command,
)
from operations.schemas import OperationRequest, OperationType

TEST_IMAGE = "alpine:latest"


def docker_usable() -> bool:
    """Whether a real container run is possible on this machine."""
    if shutil.which("docker") is None:
        return False
    return DockerSandbox(image=TEST_IMAGE).available()


requires_docker = pytest.mark.skipif(
    not docker_usable(), reason="docker is not available"
)


def probe(*argv: str) -> ValidationCommand:
    """A one-off command used to observe the container from inside."""
    return ValidationCommand(
        name="probe", argv=argv, description="test probe", language="n/a", accepts_path=False
    )


@pytest.fixture()
def repo(python_repo: Path) -> Path:
    return python_repo.resolve()


# --------------------------------------------------------------------------
# Hardening: the argv is the boundary
# --------------------------------------------------------------------------


@pytest.fixture()
def argv(repo: Path) -> list[str]:
    return DockerSandbox(image=TEST_IMAGE).docker_argv(
        VALIDATION_COMMANDS["pytest"], repo, SandboxLimits()
    )


def test_container_is_disposable(argv: list[str]) -> None:
    assert argv[:3] == ["docker", "run", "--rm"]


def test_container_has_no_network(argv: list[str]) -> None:
    assert "--network" in argv
    assert argv[argv.index("--network") + 1] == "none"


def test_container_filesystem_is_read_only(argv: list[str]) -> None:
    assert "--read-only" in argv


def test_container_gets_a_noexec_scratch_area(argv: list[str]) -> None:
    tmpfs = argv[argv.index("--tmpfs") + 1]
    assert tmpfs.startswith("/tmp:")
    assert "noexec" in tmpfs
    assert "nosuid" in tmpfs


def test_repository_is_mounted_read_only(argv: list[str], repo: Path) -> None:
    mount = argv[argv.index("--volume") + 1]
    assert mount == f"{repo}:{WORKSPACE}:ro"
    assert argv[argv.index("--workdir") + 1] == WORKSPACE


def test_container_drops_all_capabilities(argv: list[str]) -> None:
    assert argv[argv.index("--cap-drop") + 1] == "ALL"


def test_container_forbids_privilege_escalation(argv: list[str]) -> None:
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"


def test_container_runs_unprivileged(argv: list[str]) -> None:
    assert argv[argv.index("--user") + 1] == f"{SANDBOX_UID}:{SANDBOX_GID}"


def test_container_has_memory_and_swap_limits(repo: Path) -> None:
    built = DockerSandbox(image=TEST_IMAGE).docker_argv(
        VALIDATION_COMMANDS["ruff"], repo, SandboxLimits(memory_mb=256)
    )
    assert built[built.index("--memory") + 1] == "256m"
    # Equal swap disables swapping, so memory pressure cannot become disk thrash.
    assert built[built.index("--memory-swap") + 1] == "256m"


def test_container_has_cpu_and_pid_limits(repo: Path) -> None:
    built = DockerSandbox(image=TEST_IMAGE).docker_argv(
        VALIDATION_COMMANDS["ruff"], repo, SandboxLimits(cpus=0.5, pids=64)
    )
    assert built[built.index("--cpus") + 1] == "0.5"
    assert built[built.index("--pids-limit") + 1] == "64"


def test_argv_ends_with_the_image_then_the_command(argv: list[str]) -> None:
    image_index = argv.index(TEST_IMAGE)
    assert argv[image_index + 1 :] == list(VALIDATION_COMMANDS["pytest"].argv)


def test_container_never_fetches_an_image(argv: list[str]) -> None:
    # An analysis request must not be able to trigger a network image pull.
    assert argv[argv.index("--pull") + 1] == "never"


@requires_docker
def test_a_missing_image_fails_loudly_instead_of_pulling(repo: Path) -> None:
    sandbox = DockerSandbox(image="zdocs-ai/definitely-not-a-real-image:v0")
    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run(probe("true"), repo, SandboxLimits(timeout_seconds=30))
    assert "not present locally" in str(excinfo.value)


@requires_docker
def test_image_presence_is_detected(repo: Path) -> None:
    assert DockerSandbox(image=TEST_IMAGE).has_image() is True
    assert DockerSandbox(image="zdocs-ai/nope:v0").has_image() is False


def test_a_validated_path_is_appended(repo: Path) -> None:
    built = DockerSandbox(image=TEST_IMAGE).docker_argv(
        VALIDATION_COMMANDS["ruff"], repo, SandboxLimits(), path="shapes.py"
    )
    assert built[-1] == "shapes.py"


def test_a_path_is_ignored_by_commands_that_reject_one(repo: Path) -> None:
    built = DockerSandbox(image=TEST_IMAGE).docker_argv(
        VALIDATION_COMMANDS["go-vet"], repo, SandboxLimits(), path="pkg"
    )
    assert built[-1] == "./..."


def test_generated_argv_satisfies_the_launch_policy(argv: list[str]) -> None:
    # The generator and the validator must agree, or the sandbox cannot start.
    ExecutionPolicy.sandbox_host().check_command(argv)


def test_limits_are_bounded_by_the_schema() -> None:
    for kwargs in (
        {"memory_mb": 8},
        {"memory_mb": 99_999},
        {"cpus": 0},
        {"cpus": 99},
        {"pids": 1},
        {"timeout_seconds": 0},
        {"timeout_seconds": 10_000},
        {"max_output_bytes": 1},
    ):
        with pytest.raises(ValueError):
            SandboxLimits(**kwargs)


# --------------------------------------------------------------------------
# Policy: who may launch a container
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "policy",
    [
        ExecutionPolicy.repository_analysis(),
        ExecutionPolicy.development_validation(),
        ExecutionPolicy.development_validation(enabled=True),
    ],
    ids=["analysis", "validation-disabled", "validation-enabled"],
)
def test_analysis_policies_never_permit_docker(policy: ExecutionPolicy) -> None:
    # Only the dedicated launch policy may run a container. Analysis, including
    # the enabled validation profile, must not be able to.
    with pytest.raises(CommandNotAllowed):
        policy.check_command(["docker", "run", "alpine"])


def test_launch_policy_permits_only_docker() -> None:
    policy = ExecutionPolicy.sandbox_host()
    assert set(policy.allowed_commands) == {"docker"}
    for program in ("rg", "git", "ctags", "ast-grep", "sh", "python"):
        with pytest.raises(CommandNotAllowed):
            policy.check_command([program, "--version"])


def test_launch_policy_runs_no_operations() -> None:
    # It exists to start a container, never to serve an operation request.
    assert ExecutionPolicy.sandbox_host().allowed_operations == frozenset()


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "exec", "c", "sh"],
        ["docker", "cp", "c:/etc/passwd", "."],
        ["docker", "commit", "c"],
        ["docker", "build", "."],
        ["docker", "pull", "alpine"],
        ["docker", "save", "alpine"],
        ["docker", "load"],
        ["docker", "rm", "-f", "c"],
        ["docker", "system", "prune"],
        ["docker", "volume", "rm", "v"],
        ["docker", "network", "rm", "n"],
        ["docker", "container", "rm", "c"],
        # `image` alone must not be enough — only `image inspect` is read-only.
        ["docker", "image", "rm", "alpine"],
        ["docker", "image", "prune"],
        ["docker", "image", "push", "x"],
    ],
)
def test_launch_policy_restricts_docker_subcommands(argv: list[str]) -> None:
    with pytest.raises(CommandNotAllowed):
        ExecutionPolicy.sandbox_host().check_command(argv)


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "run", "--rm", "alpine", "true"],
        ["docker", "version", "--format", "{{.Server.Version}}"],
        ["docker", "image", "inspect", "alpine:latest", "--format", "{{.Id}}"],
    ],
)
def test_launch_policy_permits_only_launch_and_read_only_probes(argv: list[str]) -> None:
    ExecutionPolicy.sandbox_host().check_command(argv)


def test_allowed_docker_subcommands_mutate_nothing() -> None:
    assert DOCKER_ALLOWED_SUBCOMMANDS == frozenset(
        {("run",), ("version",), ("info",), ("image", "inspect")}
    )


@pytest.mark.parametrize(
    "argv",
    [
        ["docker", "run", "--privileged", "alpine"],
        ["docker", "run", "--cap-add", "SYS_ADMIN", "alpine"],
        ["docker", "run", "--pid", "host", "alpine"],
        ["docker", "run", "--ipc", "host", "alpine"],
        ["docker", "run", "--userns", "host", "alpine"],
        ["docker", "run", "--device", "/dev/sda", "alpine"],
        ["docker", "run", "--sysctl", "kernel.shmmax=1", "alpine"],
        ["docker", "run", "--mount", "type=bind,src=/,dst=/host", "alpine"],
        ["docker", "run", "--volumes-from", "other", "alpine"],
        ["docker", "run", "--security-opt", "seccomp=unconfined", "alpine"],
    ],
)
def test_launch_policy_refuses_sandbox_escapes(argv: list[str]) -> None:
    with pytest.raises(CommandNotAllowed):
        ExecutionPolicy.sandbox_host().check_command(argv)


@pytest.mark.parametrize(
    "mount",
    [
        "/var/run/docker.sock:/sock:ro",
        "/:/host:ro",
        "/etc:/etc:ro",
        "/root:/root:ro",
        "/home:/home:ro",
        "/proc:/proc:ro",
        "/dev:/dev:ro",
    ],
)
def test_launch_policy_refuses_sensitive_mounts(mount: str) -> None:
    with pytest.raises(CommandNotAllowed):
        ExecutionPolicy.sandbox_host().check_command(
            ["docker", "run", "--volume", mount, "alpine"]
        )


@pytest.mark.parametrize("mount", ["/stage/demo:/workspace", "/stage/demo:/workspace:rw"])
def test_launch_policy_requires_read_only_mounts(mount: str) -> None:
    with pytest.raises(CommandNotAllowed):
        ExecutionPolicy.sandbox_host().check_command(
            ["docker", "run", "--volume", mount, "alpine"]
        )


def test_launch_policy_accepts_a_read_only_workspace_mount() -> None:
    ExecutionPolicy.sandbox_host().check_command(
        ["docker", "run", "--volume", "/stage/demo:/workspace:ro", "alpine", "true"]
    )


# --------------------------------------------------------------------------
# Command allowlist
# --------------------------------------------------------------------------


def test_validation_commands_are_named_not_composed() -> None:
    resolved = resolve_validation_command("ruff", DEFAULT_VALIDATION_TOOLS)
    assert resolved.argv[0] == "python"
    assert "check" in resolved.argv


def test_unknown_validation_tool_is_refused() -> None:
    with pytest.raises(PolicyViolation):
        resolve_validation_command("rm -rf /", DEFAULT_VALIDATION_TOOLS)


def test_a_known_tool_outside_the_policy_is_refused() -> None:
    with pytest.raises(PolicyViolation):
        resolve_validation_command("pytest", frozenset({"ruff"}))


def test_no_validation_command_contains_shell_syntax() -> None:
    # Every argv must survive the launch policy's metacharacter check.
    for command in VALIDATION_COMMANDS.values():
        for argument in command.argv:
            assert not set(argument) & set(";|&`$><\n")


def test_every_default_tool_is_a_known_command() -> None:
    assert DEFAULT_VALIDATION_TOOLS <= set(VALIDATION_COMMANDS)


# --------------------------------------------------------------------------
# Absent sandbox
# --------------------------------------------------------------------------


def test_unavailable_sandbox_refuses_rather_than_running_on_the_host() -> None:
    sandbox = UnavailableSandbox("no container runtime")
    assert sandbox.available() is False
    with pytest.raises(SandboxUnavailable) as excinfo:
        sandbox.run(VALIDATION_COMMANDS["pytest"], Path("."), SandboxLimits())
    assert "never run on the host" in str(excinfo.value)


def test_build_sandbox_returns_something_usable_or_explains_itself() -> None:
    sandbox = build_sandbox()
    assert sandbox.describe()
    if not sandbox.available():
        assert "unavailable" in sandbox.describe()


def test_analysis_executor_never_probes_for_a_sandbox(python_repo: Path) -> None:
    executor = OperationExecutor(python_repo, repository="demo")
    assert executor.sandbox().available() is False
    assert "disabled" in executor.sandbox().describe()


# --------------------------------------------------------------------------
# Executor integration
# --------------------------------------------------------------------------


def run_validation(executor: OperationExecutor, **arguments: object):
    return executor.execute(
        OperationRequest(
            operation=OperationType.RUN_STATIC_ANALYSIS,
            repository=executor.repository,
            arguments=arguments,
        )
    )


def test_operation_is_refused_by_the_analysis_profile(python_repo: Path) -> None:
    result = run_validation(
        OperationExecutor(python_repo, repository="demo"), tool="ruff"
    )
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"


def test_operation_reports_an_unavailable_sandbox(python_repo: Path) -> None:
    executor = OperationExecutor(
        python_repo,
        repository="demo",
        policy=ExecutionPolicy.development_validation(enabled=True),
        sandbox=UnavailableSandbox("no container runtime in this environment"),
    )
    result = run_validation(executor, tool="ruff")
    assert result.status == "failed"
    assert result.data["error_category"] == "sandbox_unavailable"
    assert "never run on the host" in result.errors[0]


def test_operation_rejects_an_unapproved_tool(python_repo: Path) -> None:
    executor = OperationExecutor(
        python_repo,
        repository="demo",
        policy=ExecutionPolicy.development_validation(enabled=True),
        sandbox=UnavailableSandbox("n/a"),
    )
    result = run_validation(executor, tool="curl")
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"
    assert "Approved tools" in result.errors[0]


def test_operation_rejects_a_traversing_path(python_repo: Path) -> None:
    executor = OperationExecutor(
        python_repo,
        repository="demo",
        policy=ExecutionPolicy.development_validation(enabled=True),
        sandbox=UnavailableSandbox("n/a"),
    )
    result = run_validation(executor, tool="ruff", path="../../etc/passwd")
    assert result.status == "failed"
    assert result.data["error_category"] == "policy"


def test_validation_profile_permits_only_its_configured_tools(python_repo: Path) -> None:
    executor = OperationExecutor(
        python_repo,
        repository="demo",
        policy=ExecutionPolicy.development_validation(
            enabled=True, tools=frozenset({"ruff"})
        ),
        sandbox=UnavailableSandbox("n/a"),
    )
    assert run_validation(executor, tool="pytest").data["error_category"] == "policy"


# --------------------------------------------------------------------------
# Isolation, verified against a real daemon
# --------------------------------------------------------------------------


@requires_docker
def test_container_sees_the_repository(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(probe("ls"), repo, SandboxLimits(timeout_seconds=60))
    assert result.exit_code == 0
    assert "shapes.py" in result.stdout


@requires_docker
def test_container_cannot_write_to_the_repository(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("touch", f"{WORKSPACE}/EVIL.txt"), repo, SandboxLimits(timeout_seconds=60)
    )
    assert result.exit_code != 0
    assert "read-only" in (result.stdout + result.stderr).lower()
    assert not (repo / "EVIL.txt").exists()


@requires_docker
def test_container_cannot_write_to_its_own_root(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("touch", "/etc/EVIL"), repo, SandboxLimits(timeout_seconds=60)
    )
    assert result.exit_code != 0


@requires_docker
def test_container_can_write_to_scratch(repo: Path) -> None:
    # Tools need somewhere to write, or nothing would run at all.
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("touch", "/tmp/ok"), repo, SandboxLimits(timeout_seconds=60)
    )
    assert result.exit_code == 0


@requires_docker
def test_container_runs_as_an_unprivileged_user(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(probe("id"), repo, SandboxLimits(timeout_seconds=60))
    assert f"uid={SANDBOX_UID}" in result.stdout


@requires_docker
def test_container_has_no_network_access(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("ping", "-c1", "-W2", "1.1.1.1"), repo, SandboxLimits(timeout_seconds=60)
    )
    assert result.exit_code != 0
    assert "unreachable" in (result.stdout + result.stderr).lower()


@requires_docker
def test_container_is_killed_at_the_timeout(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("sleep", "120"), repo, SandboxLimits(timeout_seconds=3)
    )
    assert result.timed_out is True
    assert result.passed is False
    assert result.duration_ms < 30_000


@requires_docker
def test_container_output_is_truncated_at_the_limit(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("seq", "1", "3000000"),
        repo,
        SandboxLimits(timeout_seconds=120, max_output_bytes=4096),
    )
    assert result.truncated is True
    assert len(result.stdout) < 20_000
    assert "truncated" in result.stdout


@requires_docker
def test_container_memory_limit_is_enforced(repo: Path) -> None:
    result = DockerSandbox(image=TEST_IMAGE).run(
        probe("dd", "if=/dev/zero", "of=/tmp/big", "bs=1M", "count=400"),
        repo,
        SandboxLimits(timeout_seconds=90, memory_mb=64),
    )
    assert result.exit_code != 0


@requires_docker
def test_end_to_end_through_the_executor(repo: Path) -> None:
    executor = OperationExecutor(
        repo,
        repository="demo",
        policy=ExecutionPolicy.development_validation(enabled=True),
        sandbox=DockerSandbox(image=TEST_IMAGE),
    )
    # `ruff` is absent from the alpine image, so the run itself succeeds while
    # the tool does not — which is precisely the "ran, exit code non-zero"
    # case that must be reported as a completed run with findings.
    result = run_validation(executor, tool="ruff", timeout_seconds=60)
    assert result.status == "success"
    assert result.data["tool"] == "ruff"
    assert result.data["passed"] is False
    assert result.data["backend"] == "docker"
    assert result.data["isolation"]["network"] == "none"
