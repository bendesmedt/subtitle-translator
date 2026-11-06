import os
from pathlib import Path

import pytest

from app.paths import PathError, resolve_media_path, to_relative, validate_media_root


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "root"
    (root / "Show" / "S01").mkdir(parents=True)
    (root / "Show" / "S01" / "ep1.en.srt").write_text("x")
    (root / ".hidden").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.srt").write_text("secret")
    os.symlink(outside, root / "escape")
    os.symlink(outside / "secret.srt", root / "escape.srt")
    os.symlink(root / "Show", root / "inside-link")
    return root


def test_valid_paths(tree: Path):
    assert resolve_media_path(tree, "") == tree.resolve()
    assert resolve_media_path(tree, "Show/S01/ep1.en.srt").name == "ep1.en.srt"
    assert resolve_media_path(tree, "./Show//S01") == (tree / "Show" / "S01").resolve()
    assert to_relative(tree, tree / "Show" / "S01") == "Show/S01"
    assert to_relative(tree, tree) == ""


@pytest.mark.parametrize(
    "rel",
    [
        "../outside/secret.srt",
        "Show/../../outside",
        "/etc/passwd",
        "..",
        "Show\\..\\..\\outside",
        ".hidden",
        "Show/.git",
        "escape",
        "escape/secret.srt",
        "escape.srt",
        "bad\x00name",
    ],
)
def test_rejected_paths(tree: Path, rel: str):
    with pytest.raises(PathError):
        resolve_media_path(tree, rel)


def test_symlink_inside_root_allowed(tree: Path):
    assert resolve_media_path(tree, "inside-link/S01") == (tree / "Show" / "S01").resolve()


def test_missing_path(tree: Path):
    with pytest.raises(PathError, match="not found"):
        resolve_media_path(tree, "nope")


def test_validate_media_root_requires_directory(tree: Path):
    assert validate_media_root(tree, "Show") == (tree / "Show").resolve()
    with pytest.raises(PathError):
        validate_media_root(tree, "Show/S01/ep1.en.srt")
