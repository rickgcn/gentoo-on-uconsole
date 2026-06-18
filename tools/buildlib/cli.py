from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import BuildConfig
from .errors import BuildError
from .firmware import build_firmware
from .image import build_image
from .kernel import build_kernel, check_kernel, prepare_kernel
from .rootfs import build_rootfs


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def load_config(args: argparse.Namespace) -> BuildConfig:
    root = repo_root()
    config_path = Path(args.config)
    if not config_path.is_absolute():
        config_path = root / config_path
    return BuildConfig.load(root, config_path, verbose=args.verbose)


def add_common_flags(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the board build configuration.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print external commands before running them.",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="./tools/build",
        description="Build Gentoo artifacts for ClockworkPi uConsole.",
    )
    add_common_flags(parser)

    subparsers = parser.add_subparsers(dest="command", required=True)

    graph = subparsers.add_parser("graph", help="Print the build graph.")
    graph.set_defaults(handler=handle_graph)

    kernel = subparsers.add_parser("kernel", help="Kernel commands.")
    kernel_sub = kernel.add_subparsers(dest="kernel_command", required=True)

    kernel_prepare = kernel_sub.add_parser("prepare", help="Clone or update the kernel source.")
    kernel_prepare.set_defaults(handler=handle_kernel_prepare)

    kernel_check = kernel_sub.add_parser("check", help="Validate and apply-check kernel patches.")
    kernel_check.add_argument("--source", help="Kernel source tree to check against.")
    kernel_check.add_argument(
        "--force",
        action="store_true",
        help="Replace the existing kernel apply-check worktree.",
    )
    kernel_check.set_defaults(handler=handle_kernel_check)

    kernel_build = kernel_sub.add_parser("build", help="Build the patched kernel.")
    kernel_build.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="Parallel make jobs. Defaults to the host CPU count.",
    )
    kernel_build.add_argument(
        "--force",
        action="store_true",
        help="Replace the existing kernel build worktree and artifacts.",
    )
    kernel_build.add_argument(
        "--no-prepare",
        action="store_true",
        help="Do not clone the kernel source automatically.",
    )
    kernel_build.set_defaults(handler=handle_kernel_build)

    firmware = subparsers.add_parser("firmware", help="Firmware commands.")
    firmware_sub = firmware.add_subparsers(dest="firmware_command", required=True)
    firmware_build = firmware_sub.add_parser("build", help="Assemble boot firmware artifacts.")
    firmware_build.add_argument("--force", action="store_true", help="Replace existing firmware artifacts.")
    firmware_build.set_defaults(handler=handle_firmware_build)

    rootfs = subparsers.add_parser("rootfs", help="Root filesystem commands.")
    rootfs_sub = rootfs.add_subparsers(dest="rootfs_command", required=True)
    rootfs_build = rootfs_sub.add_parser("build", help="Assemble the Gentoo root filesystem artifact.")
    rootfs_build.add_argument("--force", action="store_true", help="Replace existing rootfs artifacts.")
    rootfs_build.set_defaults(handler=handle_rootfs_build)

    image = subparsers.add_parser("image", help="Image commands.")
    image_sub = image.add_subparsers(dest="image_command", required=True)
    image_build = image_sub.add_parser("build", help="Create the final SD card image.")
    image_build.add_argument("--force", action="store_true", help="Replace an existing image artifact.")
    image_build.set_defaults(handler=handle_image_build)

    full = subparsers.add_parser("build", help="Run the full build pipeline.")
    full.add_argument(
        "-j",
        "--jobs",
        type=int,
        default=0,
        help="Parallel make jobs for the kernel build.",
    )
    full.add_argument(
        "--force",
        action="store_true",
        help="Replace existing worktrees and artifacts.",
    )
    full.add_argument(
        "--no-prepare",
        action="store_true",
        help="Do not clone the kernel source automatically.",
    )
    full.set_defaults(handler=handle_full_build)

    return parser


def handle_graph(args: argparse.Namespace) -> int:
    config = load_config(args)
    board = config.board.name
    print(f"board: {board}")
    print(f"kernel: {config.kernel.version} ({config.kernel.ref})")
    print("graph:")
    print("  kernel -> firmware")
    print("  kernel -> rootfs")
    print("  firmware + rootfs -> image")
    print("artifacts:")
    print(f"  kernel:   {config.kernel_artifact_dir}")
    print(f"  firmware: {config.firmware_artifact_dir}")
    print(f"  rootfs:   {config.rootfs_artifact_dir}")
    print(f"  image:    {config.image_artifact_dir}")
    return 0


def handle_kernel_prepare(args: argparse.Namespace) -> int:
    prepare_kernel(load_config(args))
    return 0


def handle_kernel_check(args: argparse.Namespace) -> int:
    check_kernel(load_config(args), source_override=args.source, force=args.force)
    return 0


def handle_kernel_build(args: argparse.Namespace) -> int:
    build_kernel(load_config(args), jobs=args.jobs, force=args.force, prepare=not args.no_prepare)
    return 0


def handle_firmware_build(args: argparse.Namespace) -> int:
    build_firmware(load_config(args), force=args.force)
    return 0


def handle_rootfs_build(args: argparse.Namespace) -> int:
    build_rootfs(load_config(args), force=args.force)
    return 0


def handle_image_build(args: argparse.Namespace) -> int:
    build_image(load_config(args), force=args.force)
    return 0


def handle_full_build(args: argparse.Namespace) -> int:
    config = load_config(args)
    build_kernel(config, jobs=args.jobs, force=args.force, prepare=not args.no_prepare)
    build_firmware(config, force=args.force)
    build_rootfs(config, force=args.force)
    build_image(config, force=args.force)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except BuildError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
