from __future__ import annotations

from pathlib import Path

from .config import BuildConfig
from .errors import BuildError
from .files import archive_zst, copy_tree_contents, require_empty_or_force
from .manifest import file_manifest, write_manifest


def build_firmware(config: BuildConfig, *, force: bool = False) -> None:
    kernel_boot = config.kernel_artifact_dir / "boot"
    if not kernel_boot.exists():
        raise BuildError(f"kernel boot artifact is missing, run kernel build first: {kernel_boot}")

    artifact_dir = config.firmware_artifact_dir
    work_dir = config.firmware.work_dir
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    bootfs_dir = work_dir / "bootfs"
    bootfs_dir.mkdir(parents=True, exist_ok=True)
    copy_tree_contents(kernel_boot, bootfs_dir)
    copy_tree_contents(config.firmware.files_dir, bootfs_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / "bootfs.tar.zst"
    archive_zst(bootfs_dir, archive, verbose=config.verbose)
    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "firmware",
            "board": config.board.name,
            "kernel_version": config.kernel.version,
            "inputs": {
                "kernel_boot": str(kernel_boot.relative_to(config.root)),
                "files_dir": _optional_relative(config.firmware.files_dir, config.root),
            },
            "outputs": file_manifest(artifact_dir, config.root),
        },
    )
    print(f"firmware artifact: {archive}")


def _optional_relative(path: Path, root: Path) -> str | None:
    if not path.exists():
        return None
    return str(path.relative_to(root))
