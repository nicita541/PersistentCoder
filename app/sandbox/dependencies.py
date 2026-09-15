from __future__ import annotations

import hashlib
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from packaging.requirements import InvalidRequirement, Requirement

from app.sandbox.paths import (
    SANDBOX_IMAGE,
    SANDBOX_ROOT,
)


# ==========================================
# FRAMEWORK-OWNED DEPENDENCY PROVISIONING
# ==========================================

# Everything is project-local and framework-controlled.
DEPENDENCY_ROOT = SANDBOX_ROOT / "deps"
DEPENDENCY_IMAGE_PREFIX = "persistentcoder-sandbox:deps-"

# Manifests the FRAMEWORK looks for (never the model).
REQUIREMENT_FILES = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
)

PYPROJECT_FILE = "pyproject.toml"
SETUP_CFG_FILE = "setup.cfg"
SETUP_PY_FILE = "setup.py"

# Limits (framework-owned).
MAX_MANIFEST_BYTES = 64_000
MAX_REQUIREMENTS = 60
MAX_REQUIREMENT_LINE = 200
MAX_BUILD_SECONDS = 1_800

# A requirement line is accepted only if it is a plain PEP 508
# requirement: name, optional extras, optional specifiers, optional
# environment marker. Everything else is refused, so the model can
# never smuggle a path, a URL, an index override or a pip option
# into the dependency image.
_REQUIREMENT_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}"
    r"(?:\[[A-Za-z0-9_,.\-]+\])?"
    r"(?:\s*(?:===|==|~=|!=|<=|>=|<|>)\s*"
    r"[A-Za-z0-9._*+!\-]+)*"
    r"(?:\s*;\s*[A-Za-z0-9_.'\"<>=!,:\s\-]+)?$"
)

DENIED_SUBSTRINGS = (
    "://",
    "file:",
    "@",
    "/",
    "\\",
    "..",
)


def _sanitize_requirement(
    raw: str,
) -> str | None:
    """
    Return the safe requirement string, or None when it must be
    refused.
    """

    line = raw.strip()

    if not line or line.startswith("#"):
        return None

    if " #" in line:
        line = line.split(" #", 1)[0].strip()

    if len(line) > MAX_REQUIREMENT_LINE:
        return None

    # pip options (-e, -r, --index-url, --extra-index-url, ...)
    if line.startswith("-"):
        return None

    lowered = line.casefold()

    if any(
        marker in lowered
        for marker in DENIED_SUBSTRINGS
    ):
        return None

    try:
        requirement = Requirement(line)
    except InvalidRequirement:
        return None

    if requirement.url is not None:
        return None

    return line


def _poetry_requirement(name: str, value: object) -> str | None:
    if isinstance(value, dict):
        value = value.get("version", "*")
    if not isinstance(value, str):
        return None
    constraint = value.strip()
    if not constraint or constraint == "*":
        return name
    if constraint.startswith("^"):
        version = constraint[1:]
        parts = version.split(".")
        try:
            major = int(parts[0])
        except ValueError:
            return None
        if major > 0:
            upper = f"{major + 1}.0"
        elif len(parts) > 1:
            upper = f"0.{int(parts[1]) + 1}"
        else:
            return None
        return f"{name}>={version},<{upper}"
    if constraint.startswith("~") and not constraint.startswith("~="):
        return f"{name}~={constraint[1:]}"
    if constraint[0].isdigit():
        return f"{name}=={constraint}"
    return f"{name}{constraint}"


@dataclass(frozen=True)
class DependencySpec:
    """
    Deterministic description of a project's dependencies.

    The image tag is derived ONLY from the sanitized manifest
    content: the model cannot choose a tag or a build argument.
    """

    manifests: tuple[str, ...]
    requirements: tuple[str, ...]
    refused: tuple[str, ...]
    unsupported: tuple[str, ...] = ()

    @property
    def digest(self) -> str:
        material = "\n".join(
            [
                "python3.12",
                *self.manifests,
                *self.requirements,
            ]
        )

        return hashlib.sha256(
            material.encode("utf-8")
        ).hexdigest()

    @property
    def image(self) -> str:
        return (
            f"{DEPENDENCY_IMAGE_PREFIX}"
            f"{self.digest[:16]}"
        )

    @property
    def has_requirements(self) -> bool:
        return bool(self.requirements)


@dataclass(frozen=True)
class DependencyPlan:
    image: str
    status: str
    reason: str
    spec: DependencySpec | None = None
    built: bool = False
    from_cache: bool = False

    @property
    def ready(self) -> bool:
        return self.status == "READY"


def _read_requirements_file(
    path: Path,
) -> list[str]:
    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    if len(text.encode("utf-8")) > MAX_MANIFEST_BYTES:
        text = text[:MAX_MANIFEST_BYTES]

    lines: list[str] = []

    for raw in text.splitlines():
        if raw.lstrip().startswith("-r"):
            continue

        lines.append(raw)

    return lines


def _read_pyproject(
    path: Path,
) -> list[str]:
    try:
        data = tomllib.loads(
            path.read_text(
                encoding="utf-8",
                errors="replace",
            )
        )

    except Exception:
        return []

    values: list[str] = []

    project = data.get("project")

    if isinstance(project, dict):
        for item in project.get("dependencies") or []:
            values.append(str(item))

    poetry = (
        data.get("tool", {})
        .get("poetry", {})
        .get("dependencies", {})
    )

    if isinstance(poetry, dict):
        for name, constraint in poetry.items():
            if str(name).casefold() != "python":
                requirement = _poetry_requirement(str(name), constraint)
                if requirement:
                    values.append(requirement)

    return values


def _read_setup_cfg(
    path: Path,
) -> list[str]:
    text = path.read_text(
        encoding="utf-8",
        errors="replace",
    )

    match = re.search(
        r"install_requires\s*=\s*\n"
        r"((?:[ \t]+\S.*\n?)+)",
        text,
    )

    if not match:
        return []

    return [
        line.strip()
        for line in match.group(1).splitlines()
        if line.strip()
    ]


class DependencyResolver:
    """
    Safe, deterministic dependency provisioning for sandboxed
    Python projects.

    Guarantees (see also runner.py hardening):

      - manifests are discovered by the FRAMEWORK;
      - only sanitized PEP 508 requirement lines survive;
      - packages are installed ONLY at Docker BUILD time, never in a
        model-generated runtime command;
      - the image tag is a content hash, so the model controls
        neither the tag nor any build argument;
      - images are cached and reused by hash;
      - the container runtime still uses --network none;
      - the host .venv / host Python are never mounted or modified.

    Building is OFF by default: the caller has to opt in explicitly
    (a framework decision), otherwise the resolver reports BLOCKED
    and the base sandbox image is used.
    """

    def __init__(
        self,
        *,
        docker_executable: str = "docker",
        base_image: str = SANDBOX_IMAGE,
        cache_root: Path | None = None,
        allow_build: bool = False,
        timeout: int = MAX_BUILD_SECONDS,
        docker_run: (
            Callable[[list[str], int], tuple[int, str]]
            | None
        ) = None,
        image_available: (
            Callable[[str], bool] | None
        ) = None,
    ) -> None:
        self.docker = docker_executable
        self.base_image = base_image
        self.cache_root = (
            Path(cache_root)
            if cache_root is not None
            else DEPENDENCY_ROOT
        )
        self.allow_build = allow_build
        self.timeout = timeout
        self._docker_run = (
            docker_run or self._subprocess_run
        )
        self._image_available_hook = (
            image_available
        )

    # ==================================
    # DETECTION
    # ==================================

    def detect(
        self,
        workspace_root: str | Path,
    ) -> DependencySpec | None:
        root = Path(workspace_root)

        manifests: list[str] = []
        requirements: list[str] = []
        refused: list[str] = []
        unsupported: list[str] = []

        raw_lines: list[tuple[str, str]] = []

        for name in REQUIREMENT_FILES:
            path = root / name

            if not path.is_file():
                continue

            manifests.append(name)

            for line in _read_requirements_file(path):
                raw_lines.append((f"{name}: {line}", line))

        pyproject = root / PYPROJECT_FILE

        if pyproject.is_file():
            manifests.append(PYPROJECT_FILE)

            for line in _read_pyproject(pyproject):
                raw_lines.append(
                    (f"{PYPROJECT_FILE}: {line}", line)
                )

        setup_cfg = root / SETUP_CFG_FILE

        if setup_cfg.is_file():
            manifests.append(SETUP_CFG_FILE)

            for line in _read_setup_cfg(setup_cfg):
                raw_lines.append(
                    (f"{SETUP_CFG_FILE}: {line}", line)
                )

        if (root / SETUP_PY_FILE).is_file():
            # Executing setup.py would run model-written code at
            # build time: refused by design.
            unsupported.append(
                f"{SETUP_PY_FILE} (not parsed: executing "
                "setup.py is unsafe)"
            )

        if not manifests and not unsupported:
            return None

        for label, raw in raw_lines:
            safe = _sanitize_requirement(raw)

            if safe is None:
                refused.append(label)
                continue

            if safe not in requirements:
                requirements.append(safe)

        return DependencySpec(
            manifests=tuple(manifests),
            requirements=tuple(
                requirements[:MAX_REQUIREMENTS]
            ),
            refused=tuple(refused),
            unsupported=tuple(unsupported),
        )

    # ==================================
    # BUILD CONTEXT (framework-owned)
    # ==================================

    def context_dir(
        self,
        spec: DependencySpec,
    ) -> Path:
        return self.cache_root / spec.digest[:16]

    def dockerfile_text(
        self,
        spec: DependencySpec,
    ) -> str:
        return (
            f"FROM {self.base_image}\n"
            "USER root\n"
            "COPY requirements.txt /tmp/requirements.txt\n"
            "RUN python -m pip install --no-cache-dir \\\n"
            "    --disable-pip-version-check \\\n"
            "    -r /tmp/requirements.txt \\\n"
            "    && rm -f /tmp/requirements.txt\n"
            "USER 1000:1000\n"
            "WORKDIR /workspace\n"
        )

    def write_context(
        self,
        spec: DependencySpec,
    ) -> Path:
        context = self.context_dir(spec)

        context.mkdir(
            parents=True,
            exist_ok=True,
        )

        (context / "requirements.txt").write_text(
            "\n".join(spec.requirements) + "\n",
            encoding="utf-8",
        )

        (context / "Dockerfile").write_text(
            self.dockerfile_text(spec),
            encoding="utf-8",
        )

        return context

    # ==================================
    # IMAGE
    # ==================================

    def _subprocess_run(
        self,
        arguments: list[str],
        timeout: int,
    ) -> tuple[int, str]:
        try:
            completed = subprocess.run(
                arguments,
                capture_output=True,
                text=True,
                timeout=timeout,
            )

        except (
            OSError,
            subprocess.SubprocessError,
        ) as error:
            return 1, str(error)

        return (
            completed.returncode,
            (completed.stdout or "")
            + (completed.stderr or ""),
        )

    def image_available(
        self,
        image: str,
    ) -> bool:
        if self._image_available_hook is not None:
            return bool(
                self._image_available_hook(image)
            )

        code, _output = self._docker_run(
            [
                self.docker,
                "image",
                "inspect",
                image,
            ],
            60,
        )

        return code == 0

    def build_image(
        self,
        spec: DependencySpec,
    ) -> tuple[bool, str]:
        context = self.write_context(spec)

        dockerfile = context / "Dockerfile"

        code, output = self._docker_run(
            [
                self.docker,
                "build",
                "-t",
                spec.image,
                "--file",
                str(dockerfile),
                str(context),
            ],
            self.timeout,
        )

        trimmed = output.strip()

        if len(trimmed) > 400:
            trimmed = trimmed[-400:]

        return code == 0, trimmed

    def plan(
        self,
        workspace_root: str | Path,
    ) -> DependencyPlan:
        spec = self.detect(workspace_root)

        if spec is None:
            return DependencyPlan(
                image=self.base_image,
                status="NONE",
                reason="no dependency manifests found",
            )

        if not spec.has_requirements:
            return DependencyPlan(
                image=self.base_image,
                status="NONE",
                reason=(
                    "no installable requirements "
                    f"({len(spec.refused)} refused, "
                    f"{len(spec.unsupported)} unsupported)"
                ),
                spec=spec,
            )

        if self.image_available(spec.image):
            return DependencyPlan(
                image=spec.image,
                status="READY",
                reason="cached dependency image",
                spec=spec,
                from_cache=True,
            )

        if not self.allow_build:
            return DependencyPlan(
                image=self.base_image,
                status="BLOCKED",
                reason=(
                    "dependency image build is disabled "
                    "(framework opt-in required); using the "
                    "base image"
                ),
                spec=spec,
            )

        built, output = self.build_image(spec)

        if not built:
            return DependencyPlan(
                image=self.base_image,
                status="BLOCKED",
                reason=(
                    "dependency image build failed: "
                    f"{output}"
                ),
                spec=spec,
            )

        return DependencyPlan(
            image=spec.image,
            status="READY",
            reason="dependency image built",
            spec=spec,
            built=True,
        )
