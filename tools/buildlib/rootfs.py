from __future__ import annotations

from pathlib import Path

from .config import BuildConfig
from .errors import BuildError
from .files import archive_zst, copy_tree_contents, extract_archive, require_empty_or_force, require_root
from .manifest import file_manifest, write_manifest


def build_rootfs(config: BuildConfig, *, force: bool = False) -> None:
    if not config.rootfs.stage3:
        raise BuildError("rootfs.stage3 is not configured")

    stage3 = Path(config.rootfs.stage3)
    if not stage3.is_absolute():
        stage3 = config.root / stage3
    if not stage3.exists():
        raise BuildError(f"Gentoo stage3 archive does not exist: {stage3}")

    modules_archive = config.kernel_artifact_dir / "modules.tar.zst"
    if not modules_archive.exists():
        raise BuildError(f"kernel modules artifact is missing, run kernel build first: {modules_archive}")

    require_root("rootfs build")

    artifact_dir = config.rootfs_artifact_dir
    work_dir = config.rootfs.work_dir
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    extract_archive(stage3, work_dir, verbose=config.verbose)
    extract_archive(modules_archive, work_dir, verbose=config.verbose)
    copy_tree_contents(config.rootfs.files_dir, work_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / "rootfs.tar.zst"
    archive_zst(work_dir, archive, verbose=config.verbose)
    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "rootfs",
            "board": config.board.name,
            "inputs": {
                "stage3": str(stage3),
                "modules": str(modules_archive.relative_to(config.root)),
                "files_dir": _optional_relative(config.rootfs.files_dir, config.root),
            },
            "outputs": file_manifest(artifact_dir, config.root),
        },
    )
    print(f"rootfs artifact: {archive}")


def _optional_relative(path: Path, root: Path) -> str | None:
    if not path.exists():
        return None
    return str(path.relative_to(root))
