from __future__ import annotations

from pathlib import Path

from app.sandbox.dependencies import (
    DEPENDENCY_IMAGE_PREFIX,
    DependencyResolver,
)
from app.sandbox.paths import SANDBOX_IMAGE
from app.sandbox.runner import SandboxCommandRunner


def _project(root: Path, requirements: str) -> Path:
    (root / "src").mkdir(parents=True, exist_ok=True)

    (root / "requirements.txt").write_text(
        requirements,
        encoding="utf-8",
    )

    return root


class _FakeDocker:
    def __init__(
        self,
        *,
        build_code: int = 0,
        images: set[str] | None = None,
    ) -> None:
        self.build_code = build_code
        self.images = set(images or ())
        self.commands: list[list[str]] = []

    def __call__(
        self,
        arguments: list[str],
        timeout: int,
    ) -> tuple[int, str]:
        self.commands.append(list(arguments))

        if arguments[1:3] == ["image", "inspect"]:
            image = arguments[3]

            return (
                0 if image in self.images else 1,
                "",
            )

        if arguments[1] == "build":
            return self.build_code, (
                ""
                if self.build_code == 0
                else "ERROR: could not build wheels"
            )

        return 1, "unexpected"


def test_requirements_are_detected_deterministically(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\nrequests>=2.31\n",
    )

    resolver = DependencyResolver(
        cache_root=tmp_path / "deps",
        image_available=lambda image: False,
    )

    first = resolver.detect(project)
    second = resolver.detect(project)

    assert first is not None
    assert first.manifests == ("requirements.txt",)
    assert first.requirements == (
        "pytest==8.0.0",
        "requests>=2.31",
    )
    assert first.image == second.image
    assert first.image.startswith(
        DEPENDENCY_IMAGE_PREFIX
    )

    other = resolver.detect(
        _project(tmp_path / "other", "pytest==7.0.0\n")
    )

    assert other is not None
    assert other.image != first.image


def test_unsafe_requirement_lines_are_refused(tmp_path):
    project = _project(
        tmp_path / "project",
        "\n".join(
            [
                "# comment",
                "pytest==8.0.0",
                "-e .",
                "-r other.txt",
                "--index-url http://evil.example/simple",
                "http://evil.example/pkg.tar.gz",
                "/etc/passwd",
                "../outside",
                "flask @ file:///etc/passwd",
                "click",
            ]
        )
        + "\n",
    )

    spec = DependencyResolver(
        cache_root=tmp_path / "deps",
        image_available=lambda image: False,
    ).detect(project)

    assert spec is not None
    assert spec.requirements == ("pytest==8.0.0", "click")
    assert len(spec.refused) == 7


def test_setup_py_is_never_executed(tmp_path):
    project = _project(tmp_path / "project", "pytest==8.0.0\n")

    (project / "setup.py").write_text(
        "import os\nos.system('calc')\n",
        encoding="utf-8",
    )

    spec = DependencyResolver(
        cache_root=tmp_path / "deps",
        image_available=lambda image: False,
    ).detect(project)

    assert spec is not None
    assert spec.unsupported
    assert "setup.py" in spec.unsupported[0]
    assert "pytest==8.0.0" in spec.requirements


def test_build_is_blocked_by_default(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\n",
    )

    docker = _FakeDocker()

    plan = DependencyResolver(
        cache_root=tmp_path / "deps",
        docker_run=docker,
    ).plan(project)

    assert plan.status == "BLOCKED"
    assert plan.image == SANDBOX_IMAGE
    assert "opt-in" in plan.reason

    # Nothing was built: only the read-only image check ran.
    assert not [
        call
        for call in docker.commands
        if len(call) > 1 and call[1] == "build"
    ]


def test_build_uses_framework_generated_context(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\n-e .\n",
    )

    docker = _FakeDocker(build_code=0)

    resolver = DependencyResolver(
        cache_root=tmp_path / "deps",
        allow_build=True,
        docker_run=docker,
    )

    plan = resolver.plan(project)

    assert plan.status == "READY"
    assert plan.built is True
    assert plan.image.startswith(DEPENDENCY_IMAGE_PREFIX)

    build_calls = [
        call
        for call in docker.commands
        if len(call) > 1 and call[1] == "build"
    ]

    assert len(build_calls) == 1

    arguments = build_calls[0]

    # Framework-controlled argv: fixed flag order, derived tag.
    assert arguments[2] == "-t"
    assert arguments[3] == plan.image
    assert arguments[4] == "--file"

    context = Path(arguments[6])

    assert plan.spec is not None
    assert context == resolver.context_dir(plan.spec)

    dockerfile = (context / "Dockerfile").read_text(
        encoding="utf-8"
    )

    assert dockerfile.startswith(
        f"FROM {SANDBOX_IMAGE}\n"
    )
    assert "pip install" in dockerfile
    assert "USER 1000:1000" in dockerfile

    requirements = (
        context / "requirements.txt"
    ).read_text(encoding="utf-8")

    # Only the sanitized line survived.
    assert requirements == "pytest==8.0.0\n"


def test_cached_image_is_reused_without_rebuild(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\n",
    )

    plan = DependencyResolver(
        cache_root=tmp_path / "deps",
        allow_build=True,
        docker_run=_FakeDocker(build_code=0),
    ).plan(project)

    docker = _FakeDocker(images={plan.image})

    cached = DependencyResolver(
        cache_root=tmp_path / "deps",
        allow_build=True,
        docker_run=docker,
    ).plan(project)

    assert cached.status == "READY"
    assert cached.from_cache is True
    assert cached.built is False
    assert not [
        call
        for call in docker.commands
        if len(call) > 1 and call[1] == "build"
    ]


def test_failed_build_falls_back_to_base_image(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\n",
    )

    plan = DependencyResolver(
        cache_root=tmp_path / "deps",
        allow_build=True,
        docker_run=_FakeDocker(build_code=1),
    ).plan(project)

    assert plan.status == "BLOCKED"
    assert plan.image == SANDBOX_IMAGE
    assert "build failed" in plan.reason


def test_dependency_image_keeps_runtime_hardening(tmp_path):
    project = _project(
        tmp_path / "project",
        "pytest==8.0.0\n",
    )

    plan = DependencyResolver(
        cache_root=tmp_path / "deps",
        allow_build=True,
        docker_run=_FakeDocker(build_code=0),
    ).plan(project)

    runner = SandboxCommandRunner(
        sandbox_root=tmp_path / "workspace",
        image=plan.image,
    )

    arguments = runner.command_arguments(
        "python -m pytest -q"
    )

    assert "--network" in arguments
    assert arguments[arguments.index("--network") + 1] == "none"
    assert "--cap-drop" in arguments
    assert (
        arguments[arguments.index("--cap-drop") + 1]
        == "ALL"
    )
    assert "--security-opt" in arguments
    assert "--user" in arguments
    assert plan.image in arguments

    # The host project / venv are never mounted.
    joined = " ".join(arguments)

    assert ".venv" not in joined
    assert str(project) not in joined
    assert "docker.sock" not in joined
    assert "-e " not in joined


def test_poetry_constraints_preserve_versions(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "pyproject.toml").write_text(
        "[tool.poetry.dependencies]\n"
        'python = "^3.12"\n'
        'requests = "^2.31"\n'
        'click = "8.1.7"\n',
        encoding="utf-8",
    )

    spec = DependencyResolver(
        cache_root=tmp_path / "deps",
        image_available=lambda _image: False,
    ).detect(project)

    assert spec is not None
    assert "requests>=2.31,<3.0" in spec.requirements
    assert "click==8.1.7" in spec.requirements


def test_generated_dependency_dockerfile_has_valid_run_continuations(tmp_path):
    resolver = DependencyResolver(cache_root=tmp_path / "deps")
    project = _project(tmp_path / "project", "pytest==8.0.0\n")
    spec = resolver.detect(project)
    assert spec is not None

    dockerfile = resolver.dockerfile_text(spec)

    assert "RUN python -m pip install --no-cache-dir \\\n" in dockerfile
    assert "    --disable-pip-version-check \\\n" in dockerfile
    assert "    -r /tmp/requirements.txt \\\n" in dockerfile
