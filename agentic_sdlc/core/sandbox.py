"""Sandbox: every write lands under workspace/<run_id>/. Reads are allowed there and in explicit read-only roots."""

from __future__ import annotations

import shutil
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from .errors import SandboxViolation

MAX_WRITE_BYTES = 512 * 1024
BLOCKED_NAMES = {".env", "id_rsa", "id_ed25519", ".git"}


class Sandbox:
    def __init__(self, root: Path, read_roots: Sequence[Path] = ()) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.read_roots = [r.resolve() for r in read_roots]

    def resolve(self, relative: str | Path, *, for_write: bool = True) -> Path:
        rel = PurePosixPath(str(relative).replace("\\", "/"))
        if rel.is_absolute() or ".." in rel.parts or (rel.parts and rel.parts[0].endswith(":")):
            if for_write:
                raise SandboxViolation(f"path must be relative and stay inside the workspace: {relative}")
            return self._resolve_read_root(Path(str(relative)))
        if any(part in BLOCKED_NAMES for part in rel.parts):
            raise SandboxViolation(f"blocked path component in {relative}")
        target = (self.root / Path(*rel.parts)).resolve() if rel.parts else self.root
        if target != self.root and self.root not in target.parents:
            raise SandboxViolation(f"path escapes workspace via symlink: {relative}")
        return target

    def _resolve_read_root(self, path: Path) -> Path:
        target = path.resolve()
        for root in self.read_roots:
            if target == root or root in target.parents:
                return target
        raise SandboxViolation(f"read outside workspace and allowed read roots: {path}")

    def write_text(self, relative: str | Path, content: str) -> Path:
        if len(content.encode("utf-8")) > MAX_WRITE_BYTES:
            raise SandboxViolation(f"refusing to write more than {MAX_WRITE_BYTES} bytes to {relative}")
        target = self.resolve(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def read_text(self, relative: str | Path) -> str:
        return self.resolve(relative, for_write=False).read_text(encoding="utf-8")

    def copy_tree_in(self, source: Path, relative_dest: str) -> Path:
        """Copy a read-only root (e.g. the brownfield target repo) into the sandbox. The original is never modified."""
        src = self._resolve_read_root(source)
        dest = self.resolve(relative_dest)
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.db", "*.db-wal", "*.db-shm", "*.db-journal",
                                                                  ".pytest_cache", ".git"))
        return dest

    def reset_dir(self, relative: str) -> Path:
        target = self.resolve(relative)
        if target == self.root:
            raise SandboxViolation("refusing to wipe the workspace root")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        return target
