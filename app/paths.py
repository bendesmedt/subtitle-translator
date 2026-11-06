"""Media-root confinement helpers. All user-supplied paths are media-relative."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath


class PathError(ValueError):
    """Raised for paths outside the media root, hidden, or otherwise invalid."""


def _is_hidden(name: str) -> bool:
    return name.startswith(".")


def resolve_media_path(root: Path, rel: str, *, must_exist: bool = True) -> Path:
    """Resolve a media-relative path and verify it stays inside ``root``.

    Rejects absolute paths, NUL bytes, ``..`` traversal, hidden components and
    symlinks that point outside the root.
    """
    if not isinstance(rel, str) or "\x00" in rel or len(rel) > 4096:
        raise PathError("Invalid path")
    rel = rel.replace("\\", "/").strip()
    pure = PurePosixPath(rel)
    if pure.is_absolute():
        raise PathError("Invalid path")
    parts = [p for p in pure.parts if p not in ("", ".")]
    for part in parts:
        if part == ".." or _is_hidden(part):
            raise PathError("Invalid path")
    root_resolved = root.resolve()
    candidate = root_resolved.joinpath(*parts)
    try:
        resolved = candidate.resolve(strict=must_exist)
    except (FileNotFoundError, NotADirectoryError) as exc:
        raise PathError("Path not found") from exc
    except (OSError, RuntimeError) as exc:
        raise PathError("Invalid path") from exc
    if not resolved.is_relative_to(root_resolved):
        raise PathError("Invalid path")
    return resolved


def to_relative(root: Path, path: Path) -> str:
    """Return the media-relative POSIX path ("" for the root itself)."""
    rel = path.resolve().relative_to(root.resolve()).as_posix()
    return "" if rel == "." else rel


def entry_inside_root(root_resolved: Path, entry: os.DirEntry) -> Path | None:
    """Return the resolved path of a directory entry if it is safe to expose."""
    if _is_hidden(entry.name):
        return None
    path = Path(entry.path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if not resolved.is_relative_to(root_resolved):
        return None
    return resolved


def validate_media_root(media_base: Path, rel: str) -> Path:
    """Validate the media root chosen in the wizard: a directory inside MEDIA_BASE."""
    path = resolve_media_path(media_base, rel)
    if not path.is_dir():
        raise PathError("Media folder must be a directory")
    return path
