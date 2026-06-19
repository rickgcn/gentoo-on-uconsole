from __future__ import annotations

import shutil
from pathlib import Path

from .command import run
from .config import BuildConfig
from .disk import load_or_create_disk_identity
from .errors import BuildError
from .files import archive_zst, copy_tree_contents, require_empty_or_force
from .manifest import file_manifest, write_manifest


def prepare_firmware(config: BuildConfig) -> None:
    source_dir = config.firmware.source_dir
    if source_dir.exists() and not (source_dir / ".git").exists():
        raise BuildError(f"firmware source directory exists but is not a git repository: {source_dir}")

    if not source_dir.exists():
        source_dir.mkdir(parents=True, exist_ok=True)
        run(["git", "-C", str(source_dir), "init"], verbose=config.verbose)
        run(["git", "-C", str(source_dir), "remote", "add", "origin", config.firmware.repo], verbose=config.verbose)

    _configure_sparse_checkout(config)
    run(
        [
            "git",
            "-C",
            str(source_dir),
            "fetch",
            "--depth",
            "1",
            "--filter=blob:none",
            "origin",
            config.firmware.ref,
        ],
        verbose=config.verbose,
    )
    run(["git", "-C", str(source_dir), "checkout", "--force", "FETCH_HEAD"], verbose=config.verbose)
    _require_boot_files(config)
    print(f"firmware source prepared: {source_dir}")


def build_firmware(config: BuildConfig, *, force: bool = False, prepare: bool = True) -> None:
    if prepare:
        prepare_firmware(config)

    kernel_boot = config.kernel_artifact_dir / "boot"
    if not kernel_boot.exists():
        raise BuildError(f"kernel boot artifact is missing, run kernel build first: {kernel_boot}")
    _require_boot_files(config)

    identity = load_or_create_disk_identity(config)
    artifact_dir = config.firmware_artifact_dir
    work_dir = config.firmware.work_dir
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    bootfs_dir = work_dir / "bootfs"
    bootfs_dir.mkdir(parents=True, exist_ok=True)
    _copy_boot_firmware(config, bootfs_dir)
    copy_tree_contents(kernel_boot, bootfs_dir)
    _write_boot_config(config, identity.root_partuuid, bootfs_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / "bootfs.tar.zst"
    archive_zst(bootfs_dir, archive, verbose=config.verbose)
    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "firmware",
            "board": config.board.name,
            "kernel_version": config.kernel.version,
            "source": _source_manifest(config),
            "inputs": {
                "kernel_boot": str(kernel_boot.relative_to(config.root)),
                "disk_identity": str(config.disk_identity_path.relative_to(config.root)),
            },
            "boot": {
                "files": list(config.firmware.boot_files),
                "config_txt": list(config.firmware.config_txt),
                "cmdline": _cmdline(config, identity.root_partuuid),
            },
            "disk": {
                "partition_table": identity.partition_table,
                "disk_guid": identity.disk_guid,
                "boot_partition_guid": identity.boot_partition_guid,
                "root_partition_guid": identity.root_partition_guid,
                "boot_filesystem_id": identity.boot_filesystem_id,
                "root_filesystem_uuid": identity.root_filesystem_uuid,
                "boot_label": identity.boot_label,
                "root_label": identity.root_label,
            },
            "outputs": file_manifest(artifact_dir, config.root),
        },
    )
    print(f"firmware artifact: {archive}")


def _configure_sparse_checkout(config: BuildConfig) -> None:
    source_dir = config.firmware.source_dir
    run(["git", "-C", str(source_dir), "sparse-checkout", "init", "--cone"], verbose=config.verbose)
    run(["git", "-C", str(source_dir), "sparse-checkout", "set", "boot"], verbose=config.verbose)


def _require_boot_files(config: BuildConfig) -> None:
    missing = [item for item in config.firmware.boot_files if not (config.firmware.source_dir / item).exists()]
    if missing:
        joined = ", ".join(missing)
        raise BuildError(f"firmware boot files are missing, run firmware prepare first: {joined}")


def _copy_boot_firmware(config: BuildConfig, bootfs_dir: Path) -> None:
    for item in config.firmware.boot_files:
        src = config.firmware.source_dir / item
        dst = bootfs_dir / Path(item).name
        shutil.copy2(src, dst)


def _write_boot_config(config: BuildConfig, root_partuuid: str, bootfs_dir: Path) -> None:
    (bootfs_dir / "config.txt").write_text("\n".join(config.firmware.config_txt) + "\n", encoding="utf-8")
    (bootfs_dir / "cmdline.txt").write_text(_cmdline(config, root_partuuid) + "\n", encoding="utf-8")


def _cmdline(config: BuildConfig, root_partuuid: str) -> str:
    entries = [f"root=PARTUUID={root_partuuid}"]
    entries.extend(config.firmware.cmdline)
    return " ".join(entries)


def _source_manifest(config: BuildConfig) -> dict[str, str]:
    result = run(
        ["git", "-C", str(config.firmware.source_dir), "rev-parse", "HEAD"],
        capture=True,
        verbose=config.verbose,
    )
    return {
        "repo": config.firmware.repo,
        "ref": config.firmware.ref,
        "commit": (result.stdout or "").strip(),
    }
