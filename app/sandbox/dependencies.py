from __future__ import annotations

import configparser
import hashlib
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


class DependencyManifestError(ValueError):
    pass

# A requirement line is accepted only if it is a plain PEP 508
# requirement: name, optional extras, optional specifiers, optional
# environment marker. Everything else is refused, so the model can
# never smuggle a path, a URL, an index override or a pip option
# into the dependency image.
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
        unsupported_keys = set(value) - {"version"}
        if unsupported_keys:
            raise DependencyManifestError(
                f"unsupported Poetry dependency fields for {name}: "
                + ", ".join(sorted(unsupported_keys))
            )
        value = value.get("version", "*")
    if not isinstance(value, str):
        raise DependencyManifestError(f"invalid Poetry dependency constraint: {name}")
    constraint = value.strip()
    if not constraint or constraint == "*":
        return name
    if constraint.startswith("^"):
        version = constraint[1:]
        parts = version.split(".")
        try:
            major = int(parts[0])
        except ValueError:
            raise DependencyManifestError(f"invalid Poetry version constraint: {name}")
        if major > 0:
            upper = f"{major + 1}.0"
        elif len(parts) > 1:
            try:
                upper = f"0.{int(parts[1]) + 1}"
            except ValueError as error:
                raise DependencyManifestError(
                    f"invalid Poetry version constraint: {name}"
                ) from error
        else:
            raise DependencyManifestError(f"invalid Poetry version constraint: {name}")
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
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise DependencyManifestError(f"manifest size exceeds limit: {path.name}")
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as error:
        raise DependencyManifestError(f"manifest is not UTF-8: {path.name}") from error

    lines: list[str] = []

    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        lines.append(raw)

    return lines


def _read_pyproject(
    path: Path,
) -> list[str]:
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise DependencyManifestError(f"manifest size exceeds limit: {path.name}")
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise DependencyManifestError(f"malformed dependency manifest: {path.name}") from error

    values: list[str] = []

    project = data.get("project")

    if project is not None and not isinstance(project, dict):
        raise DependencyManifestError("project must be a TOML table")

    if isinstance(project, dict):
        dynamic = project.get("dynamic") or []
        if not isinstance(dynamic, list) or not all(
            isinstance(item, str) for item in dynamic
        ):
            raise DependencyManifestError("project.dynamic must be a string list")
        dynamic_dependencies = {
            item.casefold()
            for item in dynamic
            if item.casefold() in {"dependencies", "optional-dependencies"}
        }
        if dynamic_dependencies:
            fields = ", ".join(sorted(dynamic_dependencies))
            raise DependencyManifestError(
                f"dynamic project dependencies are unsupported: {fields}"
            )
        declared = project.get("dependencies") or []
        if not isinstance(declared, list) or not all(isinstance(item, str) for item in declared):
            raise DependencyManifestError("project.dependencies must be a string list")
        values.extend(declared)
        optional = project.get("optional-dependencies") or {}
        if not isinstance(optional, dict):
            raise DependencyManifestError("project.optional-dependencies must be a table")
        for group, items in optional.items():
            if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
                raise DependencyManifestError(
                    f"project.optional-dependencies.{group} must be a string list"
                )
            values.extend(items)

    build_system = data.get("build-system")
    if build_system is not None:
        if not isinstance(build_system, dict):
            raise DependencyManifestError("build-system must be a TOML table")
        build_requires = build_system.get("requires") or []
        if not isinstance(build_requires, list) or not all(
            isinstance(item, str) for item in build_requires
        ):
            raise DependencyManifestError(
                "build-system.requires must be a string list"
            )
        values.extend(build_requires)

    dependency_groups = data.get("dependency-groups", {})
    if not isinstance(dependency_groups, dict):
        raise DependencyManifestError("dependency-groups must be a TOML table")
    for group, items in dependency_groups.items():
        if not isinstance(items, list) or not all(
            isinstance(item, str) for item in items
        ):
            raise DependencyManifestError(
                f"dependency-groups.{group} contains unsupported declarations"
            )
        values.extend(items)

    tool = data.get("tool", {})
    if not isinstance(tool, dict):
        raise DependencyManifestError("tool must be a TOML table")
    poetry_table = tool.get("poetry", {})
    if not isinstance(poetry_table, dict):
        raise DependencyManifestError("tool.poetry must be a TOML table")
    poetry = poetry_table.get("dependencies", {})
    if not isinstance(poetry, dict):
        raise DependencyManifestError("tool.poetry.dependencies must be a TOML table")

    for name, constraint in poetry.items():
        if str(name).casefold() != "python":
            requirement = _poetry_requirement(str(name), constraint)
            if requirement:
                values.append(requirement)

    legacy_dev = poetry_table.get("dev-dependencies", {})
    if not isinstance(legacy_dev, dict):
        raise DependencyManifestError(
            "tool.poetry.dev-dependencies must be a TOML table"
        )
    for name, constraint in legacy_dev.items():
        requirement = _poetry_requirement(str(name), constraint)
        if requirement:
            values.append(requirement)

    poetry_groups = poetry_table.get("group", {})
    if not isinstance(poetry_groups, dict):
        raise DependencyManifestError("tool.poetry.group must be a TOML table")
    for group, group_table in poetry_groups.items():
        if not isinstance(group_table, dict):
            raise DependencyManifestError(
                f"tool.poetry.group.{group} must be a TOML table"
            )
        group_dependencies = group_table.get("dependencies", {})
        if not isinstance(group_dependencies, dict):
            raise DependencyManifestError(
                f"tool.poetry.group.{group}.dependencies must be a TOML table"
            )
        for name, constraint in group_dependencies.items():
            requirement = _poetry_requirement(str(name), constraint)
            if requirement:
                values.append(requirement)

    return values


def _read_setup_cfg(
    path: Path,
) -> list[str]:
    raw_bytes = path.read_bytes()
    if len(raw_bytes) > MAX_MANIFEST_BYTES:
        raise DependencyManifestError(f"manifest size exceeds limit: {path.name}")
    try:
        text = raw_bytes.decode("utf-8")
        parser = configparser.ConfigParser(interpolation=None, strict=True)
        parser.read_string(text)
    except (UnicodeDecodeError, configparser.Error) as error:
        raise DependencyManifestError(f"malformed dependency manifest: {path.name}") from error
    if not parser.has_option("options", "install_requires"):
        values: list[str] = []
    else:
        values = [
            line.strip()
            for line in parser.get("options", "install_requires").splitlines()
            if line.strip()
        ]
    if parser.has_section("options.extras_require"):
        for _group, declared in parser.items("options.extras_require"):
            values.extend(
                line.strip()
                for line in declared.splitlines()
                if line.strip()
            )
    return values


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

        accepted_declarations = 0
        for label, raw in raw_lines:
            safe = _sanitize_requirement(raw)

            if safe is None:
                refused.append(label)
                continue

            accepted_declarations += 1

            if safe not in requirements:
                requirements.append(safe)

        if accepted_declarations > MAX_REQUIREMENTS:
            refused.append(
                "dependency count exceeds limit "
                f"({accepted_declarations} > {MAX_REQUIREMENTS})"
            )

        return DependencySpec(
            manifests=tuple(manifests),
            requirements=tuple(requirements),
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
        try:
            spec = self.detect(workspace_root)
        except (DependencyManifestError, OSError) as error:
            return DependencyPlan(
                image=self.base_image,
                status="BLOCKED",
                reason=str(error),
            )

        if spec is None:
            return DependencyPlan(
                image=self.base_image,
                status="NONE",
                reason="no dependency manifests found",
            )

        if spec.refused or spec.unsupported:
            details = "; ".join((*spec.refused, *spec.unsupported))[:800]
            return DependencyPlan(
                image=self.base_image,
                status="BLOCKED",
                reason=(
                    "dependency manifest refused: "
                    f"{len(spec.refused)} refused, "
                    f"{len(spec.unsupported)} unsupported: {details}"
                ),
                spec=spec,
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
