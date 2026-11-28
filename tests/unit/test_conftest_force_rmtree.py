"""``force_rmtree`` must clear trees git left read-only, without following links."""

from __future__ import annotations

import stat
from collections.abc import Callable
from pathlib import Path

from tests.conftest import force_rmtree


def test_force_rmtree_removes_read_only_files(tmp_path: Path) -> None:
    tree = tmp_path / "home" / "git" / "main_repo" / ".git" / "objects" / "64"
    tree.mkdir(parents=True)
    blob = tree / ("a" * 38)
    blob.write_bytes(b"blob 5\x00hello")
    blob.chmod(stat.S_IREAD)

    force_rmtree(tmp_path / "home")

    assert not (tmp_path / "home").exists()


def test_force_rmtree_leaves_link_targets_alone(
    tmp_path: Path, dir_link: Callable[[Path, Path], None]
) -> None:
    target = tmp_path / "skills"
    target.mkdir()
    keep = target / "SKILL.md"
    keep.write_text("# keep")
    keep.chmod(stat.S_IREAD)
    tree = tmp_path / "tree"
    tree.mkdir()
    dir_link(target, tree / "skills")

    force_rmtree(tree)

    assert not tree.exists()
    assert keep.read_text() == "# keep"
    assert not keep.stat().st_mode & stat.S_IWRITE
