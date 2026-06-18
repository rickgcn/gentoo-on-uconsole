from __future__ import annotations

from pathlib import Path

from .command import run
from .config import BuildConfig
from .errors import BuildError
from .files import require_empty_or_force, require_root, safe_rmtree
from .manifest import file_manifest, write_manifest


def build_image(config: BuildConfig, *, force: bool = False) -> None:
    bootfs_archive = config.firmware_artifact_dir / "bootfs.tar.zst"
    rootfs_archive = config.rootfs_artifact_dir / "rootfs.tar.zst"
    if not bootfs_archive.exists():
        raise BuildError(f"firmware artifact is missing, run firmware build first: {bootfs_archive}")
    if not rootfs_archive.exists():
        raise BuildError(f"rootfs artifact is missing, run rootfs build first: {rootfs_archive}")

    require_root("image build")

    artifact_dir = config.image_artifact_dir
    work_dir = config.image.work_dir
    image_path = artifact_dir / config.image.name
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(image_path, force=force, allowed_root=config.paths.artifacts_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    root_mount = work_dir / "mnt-root"
    boot_mount = work_dir / "mnt-boot"
    root_mount.mkdir(parents=True, exist_ok=True)
    boot_mount.mkdir(parents=True, exist_ok=True)

    loop_device = ""
    try:
        _create_partitioned_image(config, image_path)
        loop_device = _attach_loop(config, image_path)
        boot_part = _loop_partition(loop_device, 1)
        root_part = _loop_partition(loop_device, 2)

        run(["mkfs.vfat", "-F", "32", boot_part], verbose=config.verbose)
        run(["mkfs.ext4", "-F", root_part], verbose=config.verbose)
        run(["mount", root_part, str(root_mount)], verbose=config.verbose)
        run(["mount", boot_part, str(boot_mount)], verbose=config.verbose)
        run(["tar", "--zstd", "-xpf", str(rootfs_archive), "-C", str(root_mount)], verbose=config.verbose)
        run(["tar", "--zstd", "-xpf", str(bootfs_archive), "-C", str(boot_mount)], verbose=config.verbose)
        run(["sync"], verbose=config.verbose)
    finally:
        _best_effort_unmount(config, boot_mount)
        _best_effort_unmount(config, root_mount)
        if loop_device:
            _best_effort_detach(config, loop_device)
        safe_rmtree(work_dir, config.paths.build_dir)

    compressed_path = None
    if config.image.compress:
        run(["xz", "-T0", "-f", "-k", str(image_path)], verbose=config.verbose)
        compressed_path = image_path.with_suffix(image_path.suffix + ".xz")

    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "image",
            "board": config.board.name,
            "kernel_version": config.kernel.version,
            "inputs": {
                "bootfs": str(bootfs_archive.relative_to(config.root)),
                "rootfs": str(rootfs_archive.relative_to(config.root)),
            },
            "image": {
                "size_mib": config.image.size_mib,
                "boot_size_mib": config.image.boot_size_mib,
            },
            "outputs": file_manifest(artifact_dir, config.root),
        },
    )
    print(f"image artifact: {image_path}")
    if compressed_path:
        print(f"compressed image: {compressed_path}")


def _create_partitioned_image(config: BuildConfig, image_path: Path) -> None:
    run(["truncate", "-s", f"{config.image.size_mib}M", str(image_path)], verbose=config.verbose)
    layout = (
        "label: dos\n"
        "unit: MiB\n"
        "\n"
        f", {config.image.boot_size_mib}, c, *\n"
        ", , L\n"
    )
    run(["sfdisk", str(image_path)], input_text=layout, verbose=config.verbose)


def _attach_loop(config: BuildConfig, image_path: Path) -> str:
    result = run(
        ["losetup", "--find", "--partscan", "--show", str(image_path)],
        capture=True,
        verbose=config.verbose,
    )
    loop_device = (result.stdout or "").strip()
    if not loop_device:
        raise BuildError("losetup did not return a loop device")
    run(["udevadm", "settle"], verbose=config.verbose)
    return loop_device


def _loop_partition(loop_device: str, number: int) -> str:
    path = f"{loop_device}p{number}"
    if Path(path).exists():
        return path
    alt = f"{loop_device}{number}"
    if Path(alt).exists():
        return alt
    raise BuildError(f"loop partition does not exist: {path}")


def _best_effort_unmount(config: BuildConfig, path: Path) -> None:
    try:
        run(["umount", str(path)], verbose=config.verbose)
    except BuildError:
        pass


def _best_effort_detach(config: BuildConfig, loop_device: str) -> None:
    try:
        run(["losetup", "-d", loop_device], verbose=config.verbose)
    except BuildError:
        pass
