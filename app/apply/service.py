from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import threading
import uuid
import weakref
from contextlib import ExitStack
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from app.apply import builder as filesystem
from app.apply.manifest import PatchManifest, PatchOperation
from app.apply.store import PatchManifestStore
from app.sandbox.limits import DEFAULT_LIMITS, SandboxLimits
from app.sandbox.project_path import ProjectPath
from app.sandbox.protected_paths import ProtectedPathPolicy
from app.sandbox.runner import CancellationToken
from app.tasks.store_context import StoreContext
from app.tasks.unit_of_work import RuntimeUnitOfWork


class ApplyStatus(str, Enum):
    COMMITTED = "COMMITTED"
    CONFLICT = "CONFLICT"
    ROLLED_BACK = "ROLLED_BACK"
    RECOVERY_FAILED = "RECOVERY_FAILED"


@dataclass(frozen=True, slots=True)
class ApplyResult:
    status: ApplyStatus
    applied_paths: tuple[str, ...]
    reason: str | None
    manifest_id: str
    journal_id: int | None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ApplyStatus):
            raise ValueError("invalid apply status")
        paths = tuple(ProjectPath.parse(path).value for path in self.applied_paths)
        if paths != tuple(self.applied_paths) or len(set(paths)) != len(paths):
            raise ValueError("apply result paths must be canonical and unique")
        object.__setattr__(self, "applied_paths", paths)
        if self.reason is not None:
            object.__setattr__(self, "reason", "".join(
                char if ord(char) >= 32 else " " for char in self.reason
            )[:2000])


class _Refused(RuntimeError):
    pass


_LOCKS: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
_LOCKS_GUARD = threading.Lock()


class _ProjectLock:
    """Nonblocking process/thread exclusion without creating filesystem data."""

    def __init__(self, context: StoreContext) -> None:
        self.context = context
        key = context.project_id
        self.key = hashlib.sha256(key.encode()).hexdigest()
        with _LOCKS_GUARD:
            self.local = _LOCKS.setdefault(self.key, threading.Lock())
        self.handle = None

    def __enter__(self):
        if not self.local.acquire(blocking=False):
            raise _Refused("another project apply or recovery is active")
        try:
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                create = filesystem._kernel32.CreateMutexW
                create.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
                create.restype = wintypes.HANDLE
                handle = create(None, False, "Local\\PersistentCoderApply-" + self.key)
                if not handle:
                    raise ctypes.WinError(ctypes.get_last_error())
                wait = filesystem._kernel32.WaitForSingleObject
                wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                wait.restype = wintypes.DWORD
                if wait(handle, 0) not in {0, 0x80}:  # acquired or abandoned owner
                    filesystem._windows_close(int(handle))
                    raise _Refused("another project apply or recovery is active")
                self.handle = int(handle)
            else:
                import fcntl
                handle = os.open(self.context.canonical_source_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    # flock is descriptor-scoped: closing SQLite connections
                    # cannot silently release it (unlike POSIX record locks).
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BaseException:
                    os.close(handle)
                    raise
                self.handle = handle
            return self
        except BaseException:
            self.local.release()
            raise

    def __exit__(self, *args):
        try:
            if os.name == "nt":
                from ctypes import wintypes
                release = filesystem._kernel32.ReleaseMutex
                release.argtypes = [wintypes.HANDLE]
                release.restype = wintypes.BOOL
                release(self.handle)
                filesystem._windows_close(self.handle)
            else:
                os.close(self.handle)
        finally:
            self.local.release()


def _identity(metadata: os.stat_result) -> list[int]:
    # Directory timestamps/sizes change during our own child operations.
    return [int(metadata.st_dev), int(metadata.st_ino), stat.S_IFMT(metadata.st_mode),
            int(getattr(metadata, "st_file_attributes", 0))]


def _stamp(metadata: os.stat_result) -> tuple[int, ...]:
    return (*_identity(metadata), int(metadata.st_size), int(metadata.st_mtime_ns),
            int(metadata.st_ctime_ns) if os.name != "nt" else 0)


def _absolute(value: str | Path) -> Path:
    # Do not resolve: resolution would erase evidence of a link substitution.
    return Path(os.path.abspath(value))


def _cancel(token: CancellationToken | None) -> None:
    if token is not None and token.cancelled:
        raise _Refused("apply cancelled")


class _Tree:
    """Pinned ancestor chain and no-follow, bounded file operations.

    Windows directory handles deny delete sharing, preventing ancestor rename
    and junction replacement. POSIX operations use pinned parent descriptors.
    Leaf replacement/unlink never dereferences the destination leaf.
    """

    def __init__(self, root: Path, expected: list[int] | None = None, *, freeze_reads=False) -> None:
        self.root = root
        self.guards: dict[Path, tuple[int, list[int]]] = {}
        self.freeze_reads = freeze_reads and os.name == "nt"
        self.leases: dict[str, int] = {}
        try:
            for path in (*reversed(root.parents), root):
                self._pin(path)
            if expected is not None and self.root_identity != expected:
                raise _Refused("root identity changed")
        except BaseException:
            self.close()
            raise

    @property
    def root_identity(self) -> list[int]:
        return self.guards[self.root][1]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self.release_reads()
        for handle, _ in reversed(tuple(self.guards.values())):
            if os.name == "nt":
                filesystem._windows_close(handle)
            else:
                os.close(handle)
        self.guards.clear()

    def release_reads(self) -> None:
        for descriptor in self.leases.values():
            os.close(descriptor)
        self.leases.clear()
        self.freeze_reads = False

    def _stat(self, path: Path) -> os.stat_result:
        if os.name != "nt" and path != path.parent and path.parent in self.guards:
            return os.stat(path.name, dir_fd=self.guards[path.parent][0], follow_symlinks=False)
        return os.stat(path, follow_symlinks=False)

    def _pin(self, path: Path) -> None:
        if path in self.guards:
            self.check(path)
            return
        metadata = self._stat(path)
        if filesystem._is_link(metadata) or not stat.S_ISDIR(metadata.st_mode):
            raise _Refused("directory is a link, reparse point or non-directory")
        handle = None
        try:
            if os.name == "nt":
                handle = filesystem._windows_create_handle(path, directory=True)
                filesystem._validate_windows_handle(handle, metadata, directory=True)
            else:
                parent = self.guards.get(path.parent)
                handle = os.open(path.name if parent else path,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=parent[0] if parent else None)
                if _identity(os.fstat(handle)) != _identity(metadata):
                    raise _Refused("directory changed before opening")
            if _identity(self._stat(path)) != _identity(metadata):
                raise _Refused("directory changed before pinning")
            self.guards[path] = (handle, _identity(metadata))
        except BaseException:
            if handle is not None:
                if os.name == "nt":
                    filesystem._windows_close(handle)
                else:
                    os.close(handle)
            raise

    def check(self, leaf: Path | None = None) -> None:
        # Validate the complete relevant chain, not unrelated siblings. All
        # retained Windows handles still prevent replacement of other parents.
        leaf = leaf or self.root
        for path in (*reversed(leaf.parents), leaf):
            guard = self.guards.get(path)
            if guard is None:
                continue
            handle, identity = guard
            current = self._stat(path)
            if filesystem._is_link(current) or _identity(current) != identity:
                raise _Refused("pinned directory identity changed")
            if os.name == "nt":
                filesystem._validate_windows_handle(handle, current, directory=True)
            elif _identity(os.fstat(handle)) != identity:
                raise _Refused("pinned directory identity changed")

    def _parent(self, relative: str) -> Path | None:
        relative = ProjectPath.parse(relative).value
        self.check((self.root / relative).parent)
        parent = self.root
        for component in relative.split("/")[:-1]:
            parent = parent / component
            try:
                self._pin(parent)
            except FileNotFoundError:
                return None
        return parent

    def inspect(self, relative: str) -> os.stat_result | None:
        if self._parent(relative) is None:
            return None
        try:
            metadata = self._stat(self.root / relative)
        except FileNotFoundError:
            return None
        if filesystem._is_link(metadata):
            raise _Refused("file is a link or reparse point")
        return metadata

    def _open(self, relative: str, *, create=False, mutation=False) -> int:
        parent = self._parent(relative)
        if parent is None:
            raise _Refused("file parent is absent")
        path = self.root / relative
        if create:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            if os.name == "nt":
                return os.open(path, flags, 0o600)
            return os.open(path.name, flags | os.O_NOFOLLOW, 0o600, dir_fd=self.guards[parent][0])
        metadata = self.inspect(relative)
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            raise _Refused("expected an exact regular file")
        if relative in self.leases and not mutation:
            descriptor = os.dup(self.leases[relative])
            os.lseek(descriptor, 0, os.SEEK_SET)
        elif os.name == "nt":
            # Deny writes/deletes while hashing, including through hardlinks.
            access = filesystem._GENERIC_READ | (0x10000 if mutation else 0)
            handle = filesystem._create_file(str(path), access,
                filesystem._FILE_SHARE_READ, None, filesystem._OPEN_EXISTING,
                filesystem._FILE_FLAG_OPEN_REPARSE_POINT, None)
            if handle in {None, filesystem._INVALID_HANDLE_VALUE}:
                raise OSError("cannot lock file for reading")
            try:
                filesystem._validate_windows_handle(int(handle), metadata, directory=False)
                descriptor = filesystem.msvcrt.open_osfhandle(int(handle), os.O_RDONLY | os.O_BINARY)
            except BaseException:
                filesystem._windows_close(int(handle))
                raise
        else:
            descriptor = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=self.guards[parent][0])
        if _stamp(os.fstat(descriptor)) != _stamp(metadata):
            os.close(descriptor)
            raise _Refused("file changed before opening")
        if self.freeze_reads and not mutation and relative not in self.leases:
            self.leases[relative] = descriptor
            descriptor = os.dup(descriptor)
        return descriptor

    def audit(self, stamps: dict) -> None:
        for relative, expected in stamps.items():
            parent = self._parent(relative)
            try:
                actual = _stamp(self._stat(self.root / relative)) if parent is not None else None
            except FileNotFoundError:
                actual = None
            if expected != actual:
                raise _Refused("tree changed at the end of preflight")

    def read(self, relative: str, limit: int, *, output: int | None = None,
             token: CancellationToken | None = None) -> tuple[str, int, tuple[int, ...]]:
        descriptor = self._open(relative)
        try:
            return self._read_descriptor(descriptor, relative, limit, output=output, token=token)
        finally:
            os.close(descriptor)

    def _read_descriptor(self, descriptor: int, relative: str, limit: int, *, output=None, token=None):
        before = os.fstat(descriptor)
        if before.st_size > limit:
            raise _Refused("file exceeds bounded byte limit")
        digest, size = hashlib.sha256(), 0
        while True:
            _cancel(token)
            chunk = os.read(descriptor, min(65536, max(1, limit - size + 1)))
            if not chunk:
                break
            size += len(chunk)
            if size > limit:
                raise _Refused("file exceeds bounded byte limit")
            digest.update(chunk)
            if output is not None:
                view = memoryview(chunk)
                while view:
                    count = os.write(output, view)
                    if count <= 0:
                        raise OSError("short staging write")
                    view = view[count:]
        after = self.inspect(relative)
        self.check()
        if after is None or _stamp(before) != _stamp(after) or _stamp(before) != _stamp(os.fstat(descriptor)):
            raise _Refused("file changed while hashing")
        if size != before.st_size:
            raise _Refused("file size changed while hashing")
        return digest.hexdigest(), size, _stamp(after)

    def matches(self, relative: str, digest: str | None, size: int | None) -> bool:
        metadata = self.inspect(relative)
        if digest is None:
            return metadata is None
        if metadata is None or not stat.S_ISREG(metadata.st_mode) or metadata.st_size != size:
            return False
        actual, length, _ = self.read(relative, size)
        return (actual, length) == (digest, size)

    def sync(self, parent: Path) -> None:
        if os.name != "nt":
            os.fsync(self.guards[parent][0])

    def mkdir(self, relative: str) -> list[int]:
        parent = self._parent(relative)
        if parent is None:
            raise _Refused("directory parent is absent")
        path = self.root / relative
        if os.name == "nt":
            os.mkdir(path)
        else:
            os.mkdir(path.name, dir_fd=self.guards[parent][0])
        self.sync(parent)
        self._pin(path)
        return self.guards[path][1]

    def unlink(self, relative: str, *, digest=None, size=None) -> None:
        metadata = self.inspect(relative)
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            raise _Refused("delete requires an exact regular file")
        path = self.root / relative
        self.check()
        if digest is not None:
            descriptor = self._open(relative, mutation=True)
            try:
                actual, length, _ = self._read_descriptor(descriptor, relative, size)
                if (actual, length) != (digest, size):
                    raise _Refused("delete content changed")
                if os.name == "nt":
                    import ctypes
                    disposition = ctypes.c_ubyte(1)
                    _windows_set_information(descriptor, 4, disposition)
                else:
                    os.unlink(path.name, dir_fd=self.guards[path.parent][0])
            finally:
                os.close(descriptor)
        elif os.name == "nt":
            os.unlink(path)
        else:
            os.unlink(path.name, dir_fd=self.guards[path.parent][0])
        self.sync(path.parent)

    def rmdir(self, relative: str, identity: list[int]) -> None:
        path = self.root / relative
        metadata = self.inspect(relative)
        if metadata is None:
            return
        if _identity(metadata) != identity or not stat.S_ISDIR(metadata.st_mode):
            raise _Refused("created directory identity changed")
        # Release only this directory; its parent stays pinned throughout rmdir.
        guard = self.guards.pop(path, None)
        if guard:
            if os.name == "nt":
                filesystem._windows_close(guard[0])
            else:
                os.close(guard[0])
        if os.name == "nt":
            os.rmdir(path)
        else:
            os.rmdir(path.name, dir_fd=self.guards[path.parent][0])
        self.sync(path.parent)

    def replace_from(self, other: _Tree, name: str, relative: str, *, absent: bool,
                     digest: str, size: int) -> None:
        parent = self._parent(relative)
        if parent is None:
            raise _Refused("promotion parent is absent")
        other.check()
        metadata = other.inspect(name)
        if metadata is None or not stat.S_ISREG(metadata.st_mode):
            raise _Refused("staged content is not a regular file")
        path = self.root / relative
        descriptor = other._open(name, mutation=True)
        try:
            actual, length, _ = other._read_descriptor(descriptor, name, size)
            if (actual, length) != (digest, size):
                raise _Refused("promotion content changed")
            if os.name == "nt":
                import ctypes
                from ctypes import wintypes
                class RenameInformation(ctypes.Structure):
                    _fields_ = [("replace", wintypes.BOOL), ("root", wintypes.HANDLE),
                        ("length", wintypes.DWORD), ("name", ctypes.c_wchar * (len(str(path).encode("utf-16-le")) // 2 + 1))]
                information = RenameInformation()
                information.replace = not absent
                information.length = len(str(path).encode("utf-16-le"))
                information.name = str(path)
                # Rename the verified, locked file handle itself. Reopening a
                # path for MoveFileEx would permit staged-leaf substitution.
                _windows_set_information(descriptor, 3, information)
            elif absent:
                # link+unlink is an atomic no-clobber publication on one volume.
                os.link(name, path.name, src_dir_fd=other.guards[other.root][0],
                    dst_dir_fd=self.guards[parent][0], follow_symlinks=False)
                other.unlink(name)
            else:
                os.replace(name, path.name, src_dir_fd=other.guards[other.root][0],
                    dst_dir_fd=self.guards[parent][0])
        finally:
            os.close(descriptor)
        self.sync(parent)
        other.sync(other.root)

    def write_json(self, name: str, payload: object) -> None:
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        if len(data) > 8_000_000:
            raise _Refused("transaction metadata exceeds limit")
        descriptor = self._open(name, create=True)
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as output:
                output.write(data)
                output.flush()
                os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self.sync(self.root)

    def read_json(self, name: str):
        descriptor = self._open(name)
        try:
            with os.fdopen(descriptor, "rb", closefd=False) as source:
                content = source.read(8_000_001)
            if len(content) > 8_000_000:
                raise _Refused("transaction metadata exceeds limit")
            return json.loads(content, object_pairs_hook=_unique_keys)
        finally:
            os.close(descriptor)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _Refused("duplicate transaction metadata key")
        result[key] = value
    return result


def _windows_set_information(descriptor, information_class, information):
    import ctypes
    from ctypes import wintypes
    update = filesystem._kernel32.SetFileInformationByHandle
    update.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    update.restype = wintypes.BOOL
    handle = filesystem.msvcrt.get_osfhandle(descriptor)
    if not update(handle, information_class, ctypes.byref(information), ctypes.sizeof(information)):
        raise ctypes.WinError(ctypes.get_last_error())


class ApplyService:
    """Publish one stored manifest, with durable intent and verified rollback.

    Staging is a trusted framework-owned, project-local directory on the source
    volume, separate from both source and workspace. A pending SQLite journal
    reserves the whole project. Recovery never rebuilds a manifest or infers a
    successful commit from filesystem content.
    """

    def __init__(self, context: StoreContext, manifest_store: PatchManifestStore,
                 unit_of_work: RuntimeUnitOfWork, source_root: str | Path,
                 workspace_root: str | Path, staging_root: str | Path,
                 *, limits: SandboxLimits = DEFAULT_LIMITS) -> None:
        self.context, self.store, self.uow = context, manifest_store, unit_of_work
        self.source_root, self.workspace_root, self.staging_root = map(
            _absolute, (source_root, workspace_root, staging_root))
        self.limits = limits
        self._roots = {}
        for root in (self.source_root, self.workspace_root):
            try:
                self._roots[root] = _identity(os.stat(root, follow_symlinks=False))
            except OSError:
                self._roots[root] = None

    def _boundary(self, event: str, path: str | None = None) -> None:
        """Fault/cancellation seam; production has no callback or model input."""

    def _scope(self, *, recovery=False) -> None:
        if self.store.context != self.context or self.uow.context != self.context:
            raise _Refused("apply store scope does not match")
        canonical = self.context.canonical_source_root
        comparison = os.path.normcase(canonical) if os.name == "nt" else canonical
        if str(self.source_root) != canonical or hashlib.sha256(comparison.encode()).hexdigest() != self.context.project_id:
            raise _Refused("canonical source identity does not match")
        roots = (self.source_root, self.workspace_root, self.staging_root)
        if any(a.is_relative_to(b) or b.is_relative_to(a) for i, a in enumerate(roots) for b in roots[i + 1:]):
            raise _Refused("apply roots must be separate trees")
        required = (self.source_root,) if recovery else (self.source_root, self.workspace_root)
        if any(self._roots[root] is None for root in required):
            raise _Refused("apply root was not present at construction")

    def _session(self, session_id: str, manifest_id: str, version: int | None = None):
        session = self.uow.get_apply_session(session_id, manifest_id)
        if session is None or session["status"] != "DIRTY_VERIFIED" or session["active_run_id"] is not None:
            raise _Refused("apply session ownership or status does not match")
        if version is not None and (type(version) is not int or session["version"] != version):
            raise _Refused("apply session version does not match")
        return session

    def _manifest(self, manifest_id: str) -> PatchManifest:
        manifest = self.store.get(manifest_id)
        if manifest is None:
            raise _Refused("manifest does not belong to the project")
        if len(manifest.entries) > self.limits.max_snapshot_files:
            raise _Refused("manifest entry count exceeds limit")
        if sum(e.after_size or 0 for e in manifest.entries) > self.limits.max_patch_bytes:
            raise _Refused("manifest patch bytes exceed limit")
        if sum(e.before_size or 0 for e in manifest.entries) > self.limits.max_snapshot_bytes:
            raise _Refused("manifest backup bytes exceed limit")
        policy = ProtectedPathPolicy()
        for entry in manifest.entries:
            if len(entry.path.value) > 4096 or entry.path.value.count("/") > 255:
                raise _Refused("manifest path exceeds structural limit")
            if not entry.apply_safe or not entry.reviewable or not policy.classify(entry.path).included:
                raise _Refused("manifest entry is not safe and reviewable")
            if max(entry.before_size or 0, entry.after_size or 0) > self.limits.max_snapshot_file_bytes:
                raise _Refused("manifest file size exceeds limit")
        return manifest

    def _workspace(self, tree: _Tree, token) -> tuple[str, dict]:
        payload, stamps, total, nodes = [], {}, 0, 0
        def walk(path: Path, prefix: str):
            nonlocal total, nodes
            tree._pin(path)
            before = _stamp(tree._stat(path))
            with os.scandir(path if os.name == "nt" else tree.guards[path][0]) as items:
                names = []
                for item in items:
                    nodes += 1
                    if nodes > self.limits.max_snapshot_files * 2:
                        raise _Refused("workspace node count exceeds limit")
                    names.append(item.name)
                names.sort()
            for name in names:
                _cancel(token)
                relative = prefix + name
                if len(relative) > 4096 or relative.count("/") > 255:
                    raise _Refused("workspace path exceeds structural limit")
                project_path = ProjectPath.parse(relative)
                if project_path.value != relative:
                    raise _Refused("workspace path is not canonical")
                metadata = tree._stat(tree.root / relative)
                if stat.S_ISDIR(metadata.st_mode) and not filesystem._is_link(metadata):
                    walk(tree.root / relative, relative + "/")
                    continue
                if len(payload) >= self.limits.max_snapshot_files:
                    raise _Refused("workspace file count exceeds limit")
                kind, target = "file", ""
                if filesystem._is_link(metadata):
                    kind = "symbolic link" if stat.S_ISLNK(metadata.st_mode) else "reparse point"
                    try:
                        target = os.readlink(tree.root / relative)
                    except OSError:
                        if kind == "symbolic link":
                            raise
                elif not stat.S_ISREG(metadata.st_mode):
                    kind = "special file"
                if kind == "file":
                    digest, size, stamp = tree.read(relative,
                        min(self.limits.max_snapshot_file_bytes, self.limits.max_snapshot_bytes - total), token=token)
                else:
                    size, stamp = metadata.st_size, _stamp(metadata)
                    descriptor = f"{kind}:{stat.S_IFMT(metadata.st_mode)}:{size}:{target}"
                    digest = hashlib.sha256(descriptor.encode()).hexdigest()
                    if stamp != _stamp(tree._stat(tree.root / relative)):
                        raise _Refused("workspace descriptor changed")
                total += size
                if total > self.limits.max_snapshot_bytes:
                    raise _Refused("workspace byte count exceeds limit")
                payload.append({"path": relative, "size": size, "sha256": digest, "kind": kind})
                stamps[relative] = stamp
            if _stamp(tree._stat(path)) != before:
                raise _Refused("workspace directory changed while hashing")
        walk(tree.root, "")
        tree.check()
        payload.sort(key=lambda item: item["path"])
        keys = [ProjectPath.parse(item["path"]).comparison_key for item in payload]
        if len(keys) != len(set(keys)):
            raise _Refused("workspace paths collide")
        digest = hashlib.sha256(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).hexdigest()
        return digest, stamps

    def _conditions(self, tree: _Tree, manifest: PatchManifest, *, after=False) -> dict:
        stamps = {}
        for entry in manifest.entries:
            digest = entry.after_sha256 if after else entry.before_sha256
            size = entry.after_size if after else entry.before_size
            if not tree.matches(entry.path.value, digest, size):
                raise _Refused("manifest file precondition or postcondition does not match")
            metadata = tree.inspect(entry.path.value)
            stamps[entry.path.value] = _stamp(metadata) if metadata else None
        return stamps

    def _directories(self, source: _Tree, manifest: PatchManifest) -> dict:
        paths = {"/".join(entry.path.value.split("/")[:i]) for entry in manifest.entries
                 for i in range(1, len(entry.path.value.split("/")))}
        result = {}
        for path in sorted(paths, key=lambda value: (value.count("/"), value)):
            metadata = source.inspect(path)
            if metadata is not None and not stat.S_ISDIR(metadata.st_mode):
                raise _Refused("manifest parent is not a directory")
            result[path] = _identity(metadata) if metadata else None
        return result

    def _staging_parent(self) -> tuple[Path, list[str]]:
        path, missing = self.staging_root, []
        while True:
            try:
                os.stat(path, follow_symlinks=False)
                return path, list(reversed(missing))
            except FileNotFoundError:
                missing.append(path.name)
                path = path.parent

    def _copy(self, source: _Tree, name: str, destination: _Tree, target: str,
              digest: str, size: int, token=None) -> None:
        descriptor = destination._open(target, create=True)
        try:
            actual, length, _ = source.read(name, size, output=descriptor, token=token)
            if (actual, length) != (digest, size):
                raise _Refused("staging content does not match manifest")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        destination.sync(destination.root)
        if not destination.matches(target, digest, size):
            raise _Refused("staged content changed")

    def apply(self, manifest_id: str, *, agent_session_id: str,
              expected_session_version: int, cancellation_token: CancellationToken | None = None) -> ApplyResult:
        acquired = False
        try:
            with _ProjectLock(self.context):
                acquired = True
                return self._apply(manifest_id, agent_session_id=agent_session_id,
                    expected_session_version=expected_session_version, cancellation_token=cancellation_token)
        except Exception as error:
            # Failures escaping _apply are unreadable durable state, not a
            # proven write-free preflight conflict. Leave artifacts for recovery.
            status = ApplyStatus.RECOVERY_FAILED if acquired else ApplyStatus.CONFLICT
            return ApplyResult(status, (), self._reason(error), manifest_id, None)

    def _apply(self, manifest_id: str, *, agent_session_id: str,
               expected_session_version: int, cancellation_token: CancellationToken | None) -> ApplyResult:
        journal = None
        staging_id = backup_id = None
        try:
            with ExitStack() as stack:
                self._scope()
                manifest = self._manifest(manifest_id)
                self._session(agent_session_id, manifest_id, expected_session_version)
                if self.uow.pending_apply_journals():
                    raise _Refused("another project apply is pending")
                source = stack.enter_context(_Tree(self.source_root, self._roots[self.source_root], freeze_reads=True))
                workspace = stack.enter_context(_Tree(self.workspace_root, self._roots[self.workspace_root], freeze_reads=True))
                ancestor, missing = self._staging_parent()
                staging_ancestor = stack.enter_context(_Tree(ancestor))
                if staging_ancestor.root_identity[0] != source.root_identity[0]:
                    raise _Refused("staging must be on the source volume")
                _cancel(cancellation_token)
                initial = self._conditions(source, manifest)
                workspace_initial = self._workspace(workspace, cancellation_token)
                if workspace_initial[0] != manifest.workspace_sha256:
                    raise _Refused("workspace identity does not match manifest")
                self._conditions(workspace, manifest, after=True)
                directories = self._directories(source, manifest)
                self._boundary("during_preflight")
                self._boundary("before_reservation")
                _cancel(cancellation_token)
                if initial != self._conditions(source, manifest) or workspace_initial != self._workspace(workspace, cancellation_token):
                    raise _Refused("source or workspace changed during preflight")
                self._session(agent_session_id, manifest_id, expected_session_version)
                _cancel(cancellation_token)
                source.audit(initial)
                workspace.audit(workspace_initial[1])
                staging_id, backup_id = uuid.uuid4().hex, uuid.uuid4().hex
                journal_id, version = self.uow.prepare_apply(session_id=agent_session_id,
                    manifest_id=manifest_id, session_version=expected_session_version,
                    staging_id=staging_id, backup_id=backup_id)
                journal = {"id": journal_id, "agent_session_id": agent_session_id,
                    "manifest_id": manifest_id, "staging_id": staging_id,
                    "backup_id": backup_id, "state": "PREPARING"}
                self._boundary("after_reservation")
                prefix = ""
                for component in missing:
                    prefix = prefix + "/" + component if prefix else component
                    staging_ancestor.mkdir(prefix)
                staging = stack.enter_context(_Tree(self.staging_root))
                staging.mkdir(staging_id)
                staging.mkdir(backup_id)
                staged = stack.enter_context(_Tree(self.staging_root / staging_id))
                backup = stack.enter_context(_Tree(self.staging_root / backup_id))
                plan = {"schema": 1, "journal_id": journal_id, "manifest_id": manifest_id,
                        "source_identity": source.root_identity, "directories": directories}
                backup.write_json("plan.json", plan)
                for index, entry in enumerate(manifest.entries):
                    _cancel(cancellation_token)
                    if entry.before_sha256 is not None:
                        self._copy(source, entry.path.value, backup, f"{index}.before", entry.before_sha256, entry.before_size, cancellation_token)
                        self._boundary("after_stage_file", entry.path.value)
                    if entry.after_sha256 is not None:
                        self._copy(workspace, entry.path.value, staged, f"{index}.after", entry.after_sha256, entry.after_size, cancellation_token)
                        self._boundary("after_stage_file", entry.path.value)
                if initial != self._conditions(source, manifest) or workspace_initial != self._workspace(workspace, cancellation_token):
                    raise _Refused("source or workspace changed while staging")
                self._boundary("after_staging")
                _cancel(cancellation_token)
                for index, entry in enumerate(manifest.entries):
                    for artifact, suffix, digest, size in (
                        (backup, "before", entry.before_sha256, entry.before_size),
                        (staged, "after", entry.after_sha256, entry.after_size),
                    ):
                        if digest is not None and not artifact.matches(f"{index}.{suffix}", digest, size):
                            raise _Refused("transaction content changed after staging")
                source.release_reads()
                version = self.uow.transition_apply(journal_id, expected_state="PREPARING",
                    target_state="APPLYING", session_version=version)
                self._boundary("after_applying")
                for ordinal, (path, identity) in enumerate(directories.items()):
                    if identity is None:
                        _cancel(cancellation_token)
                        backup.write_json(f"d{ordinal}.intent", {"path": path})
                        created = source.mkdir(path)
                        backup.write_json(f"d{ordinal}.created", {"path": path, "identity": created})
                for index, entry in enumerate(manifest.entries):
                    path = entry.path.value
                    self._boundary("before_mutation", path)
                    _cancel(cancellation_token)
                    if not source.matches(path, entry.before_sha256, entry.before_size):
                        raise _Refused("source changed before mutation")
                    backup.write_json(f"{index}.intent", {"path": path})
                    if entry.operation is PatchOperation.DELETE:
                        source.unlink(path, digest=entry.before_sha256, size=entry.before_size)
                    else:
                        if not staged.matches(f"{index}.after", entry.after_sha256, entry.after_size):
                            raise _Refused("staged content changed before promotion")
                        source.replace_from(staged, f"{index}.after", path,
                            absent=entry.operation is PatchOperation.ADD, digest=entry.after_sha256, size=entry.after_size)
                    self._boundary("after_mutation", path)
                    _cancel(cancellation_token)
                self._boundary("before_commit")
                _cancel(cancellation_token)
                self._conditions(source, manifest, after=True)
                self.uow.transition_apply(journal_id, expected_state="APPLYING",
                    target_state="COMMITTED", session_version=version)
            self._cleanup(journal)
            return ApplyResult(ApplyStatus.COMMITTED, tuple(e.path.value for e in manifest.entries), None, manifest_id, journal_id)
        except Exception as error:
            reason = self._reason(error)
            if journal is None and staging_id is not None:
                # Recover an acknowledged-by-SQLite reservation even if the
                # Python caller lost its result immediately after COMMIT.
                journal = next((row for row in self.uow.pending_apply_journals()
                    if row["staging_id"] == staging_id and row["backup_id"] == backup_id
                    and row["agent_session_id"] == agent_session_id and row["manifest_id"] == manifest_id), None)
            if journal is None:
                return ApplyResult(ApplyStatus.CONFLICT, (), reason, manifest_id, None)
            # A commit may have succeeded even if its caller raised afterwards.
            try:
                current = self.uow.get_apply_journal(journal["id"])
            except Exception:
                return ApplyResult(ApplyStatus.RECOVERY_FAILED, (),
                    "durable apply state cannot be read; recovery remains required", manifest_id, journal["id"])
            if current is not None and current["state"] == "COMMITTED":
                self._cleanup(current)
                return ApplyResult(ApplyStatus.COMMITTED, tuple(e.path.value for e in manifest.entries), None, manifest_id, current["id"])
            return self._recover(current or journal, reason)

    @staticmethod
    def _reason(error: Exception) -> str:
        return str(error)[:2000] if isinstance(error, _Refused) else f"apply failed ({type(error).__name__})"

    def recover_pending(self) -> tuple[ApplyResult, ...]:
        try:
            with _ProjectLock(self.context):
                return tuple(self._recover(journal, "interrupted apply") for journal in self.uow.pending_apply_journals())
        except Exception as error:
            return tuple(ApplyResult(ApplyStatus.CONFLICT, (), self._reason(error),
                journal["manifest_id"], journal["id"]) for journal in self.uow.pending_apply_journals())

    def _plan(self, backup: _Tree, manifest: PatchManifest, journal: dict, source: _Tree) -> dict:
        plan = backup.read_json("plan.json")
        if not isinstance(plan, dict) or set(plan) != {"schema", "journal_id", "manifest_id", "source_identity", "directories"}:
            raise _Refused("transaction plan is incomplete")
        if plan["schema"] != 1 or plan["journal_id"] != journal["id"] or plan["manifest_id"] != manifest.manifest_id or plan["source_identity"] != source.root_identity:
            raise _Refused("transaction plan identity does not match")
        expected_paths = set(self._directories(source, manifest))
        if not isinstance(plan["directories"], dict) or set(plan["directories"]) != expected_paths:
            raise _Refused("transaction directory plan does not match manifest")
        for path, identity in plan["directories"].items():
            if identity is not None:
                metadata = source.inspect(path)
                if metadata is None or _identity(metadata) != identity:
                    raise _Refused("preexisting parent identity changed")
        return plan

    def _rollback(self, source: _Tree, backup: _Tree, manifest: PatchManifest, plan: dict) -> None:
        failures = []
        for index in reversed(range(len(manifest.entries))):
            entry = manifest.entries[index]
            path = entry.path.value
            try:
                intent = backup.inspect(f"{index}.intent")
                if intent is not None and backup.read_json(f"{index}.intent") != {"path": path}:
                    raise _Refused("mutation intent is invalid")
                if source.matches(path, entry.before_sha256, entry.before_size):
                    continue
                if intent is None or not source.matches(path, entry.after_sha256, entry.after_size):
                    raise _Refused("source restoration cannot be proven")
                self._boundary("before_rollback", path)
                if entry.before_sha256 is None:
                    source.unlink(path, digest=entry.after_sha256, size=entry.after_size)
                else:
                    # At most one disposable restore copy per manifest entry,
                    # even across arbitrarily many interrupted recovery calls.
                    restore = f"{index}.restore"
                    if backup.inspect(restore) is not None and not backup.matches(restore, entry.before_sha256, entry.before_size):
                        backup.unlink(restore)
                    if backup.inspect(restore) is None:
                        self._copy(backup, f"{index}.before", backup, restore, entry.before_sha256, entry.before_size)
                    source.replace_from(backup, restore, path, absent=entry.operation is PatchOperation.DELETE,
                        digest=entry.before_sha256, size=entry.before_size)
                if not source.matches(path, entry.before_sha256, entry.before_size):
                    raise _Refused("restored file verification failed")
                self._boundary("after_rollback", path)
            except Exception as error:
                failures.append(self._reason(error))
        ordered = sorted(plan["directories"].items(), key=lambda item: (item[0].count("/"), item[0]))
        for ordinal, (path, identity) in reversed(tuple(enumerate(ordered))):
            if identity is not None:
                continue
            try:
                metadata = source.inspect(path)
                if metadata is None:
                    continue
                if backup.inspect(f"d{ordinal}.created") is None:
                    raise _Refused("created directory ownership cannot be proven")
                created = backup.read_json(f"d{ordinal}.created")
                if not isinstance(created, dict) or set(created) != {"path", "identity"} or created["path"] != path:
                    raise _Refused("created directory receipt is invalid")
                source.rmdir(path, created["identity"])
            except Exception as error:
                failures.append(self._reason(error))
        self._conditions(source, manifest)
        if failures:
            raise _Refused("; ".join(failures)[:2000])

    def _recover(self, journal: dict, reason: str) -> ApplyResult:
        try:
            self._scope(recovery=True)
            manifest = self._manifest(journal["manifest_id"])
            session = self._session(journal["agent_session_id"], journal["manifest_id"])
            for key in ("staging_id", "backup_id"):
                if not isinstance(journal[key], str) or re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", journal[key]) is None:
                    raise _Refused("invalid journal artifact identifier")
            with _Tree(self.source_root, self._roots[self.source_root]) as source:
                if journal["state"] == "PREPARING":
                    # PREPARING never authorizes a source mutation. Partial or
                    # absent backups are harmless only if all before bytes exist.
                    self._conditions(source, manifest)
                    try:
                        os.stat(self.staging_root, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        with _Tree(self.staging_root) as staging:
                            if staging.inspect(journal["backup_id"]) is not None:
                                with _Tree(self.staging_root / journal["backup_id"]) as backup:
                                    if backup.inspect("plan.json") is not None:
                                        self._plan(backup, manifest, journal, source)
                else:
                    with _Tree(self.staging_root / journal["backup_id"]) as backup:
                        plan = self._plan(backup, manifest, journal, source)
                        self._rollback(source, backup, manifest, plan)
                self.uow.transition_apply(journal["id"], expected_state=journal["state"],
                    target_state="ROLLED_BACK", session_version=session["version"], error=reason)
            self._cleanup(journal)
            return ApplyResult(ApplyStatus.ROLLED_BACK, (), reason, journal["manifest_id"], journal["id"])
        except Exception as error:
            failure = self._reason(error)
            try:
                current = self.uow.get_apply_journal(journal["id"])
                if current is not None and current["state"] == "ROLLED_BACK":
                    self._cleanup(current)
                    return ApplyResult(ApplyStatus.ROLLED_BACK, (), reason, journal["manifest_id"], journal["id"])
            except Exception:
                pass
            try:
                self.uow.record_apply_recovery_failure(journal["id"], failure)
            except Exception:
                failure += "; recovery failure could not be persisted; journal remains reserved"
            return ApplyResult(ApplyStatus.RECOVERY_FAILED, (), failure, journal["manifest_id"], journal["id"])

    def _cleanup(self, journal: dict) -> None:
        # Only disposable files in these two framework-generated directories.
        # A cleanup failure cannot undo a durable terminal decision.
        try:
            with _Tree(self.staging_root) as staging:
                for key in ("staging_id", "backup_id"):
                    identifier = journal[key]
                    if re.fullmatch(r"[a-zA-Z0-9_-]{1,128}", identifier) is None:
                        continue
                    if staging.inspect(identifier) is None:
                        continue
                    with _Tree(self.staging_root / identifier) as artifact:
                        with os.scandir(artifact.root if os.name == "nt" else artifact.guards[artifact.root][0]) as items:
                            names = [item.name for item in items]
                        for name in names:
                            if re.fullmatch(r"(?:plan\.json|[0-9]+\.(?:before|after|intent|restore)|d[0-9]+\.(?:intent|created))", name):
                                artifact.unlink(name)
                        identity = artifact.root_identity
                    staging.rmdir(identifier, identity)
        except Exception:
            pass
