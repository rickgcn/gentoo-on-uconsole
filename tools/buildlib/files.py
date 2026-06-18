from __future__ import annotations

import os
import shutil
from pathlib import Path

from .command import run
from .errors import BuildError


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def ensure_within(path: Path, allowed_root: Path) -> None:
    resolved = path.resolve(strict=False)
    allowed = allowed_root.resolve(strict=False)
    if resolved != allowed and allowed not in resolved.parents:
        raise BuildError(f"refusing to modify path outside {allowed}: {resolved}")


def safe_rmtree(path: Path, allowed_root: Path) -> None:
    ensure_within(path, allowed_root)
    if path.exists():
        shutil.rmtree(path)


def safe_unlink(path: Path, allowed_root: Path) -> None:
    ensure_within(path, allowed_root)
    if path.exists():
        path.unlink()


def require_empty_or_force(path: Path, *, force: bool, allowed_root: Path) -> None:
    if path.exists():
        if not force:
            raise BuildError(f"path already exists, pass --force to replace it: {path}")
        if path.is_dir():
            safe_rmtree(path, allowed_root)
        else:
            safe_unlink(path, allowed_root)


def copy_tree_contents(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            ensure_parent(target)
            shutil.copy2(item, target)


def archive_zst(src: Path, dst: Path, *, verbose: bool = False) -> None:
    ensure_parent(dst)
    if not src.exists():
        raise BuildError(f"cannot archive missing directory: {src}")
    run(["tar", "--zstd", "-cf", str(dst), "-C", str(src), "."], verbose=verbose)


def extract_archive(src: Path, dst: Path, *, verbose: bool = False) -> None:
    if not src.exists():
        raise BuildError(f"archive does not exist: {src}")
    dst.mkdir(parents=True, exist_ok=True)
    args = ["tar"]
    suffixes = "".join(src.suffixes)
    if suffixes.endswith(".tar.zst"):
        args.append("--zstd")
    elif suffixes.endswith(".tar.xz"):
        args.append("-J")
    elif suffixes.endswith(".tar.gz") or suffixes.endswith(".tgz"):
        args.append("-z")
    elif suffixes.endswith(".tar.bz2"):
        args.append("-j")
    args.extend(["-xpf", str(src), "-C", str(dst)])
    run(args, verbose=verbose)


def require_root(step: str) -> None:
    if os.geteuid() != 0:
        raise BuildError(f"{step} requires root privileges")

