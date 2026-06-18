from __future__ import annotations

import os
import shutil
from pathlib import Path

from .command import run
from .config import BuildConfig
from .errors import BuildError
from .files import archive_zst, require_empty_or_force, safe_rmtree
from .manifest import file_manifest, patch_manifest, write_manifest
from .patches import discover_patches, print_patch_queue


def prepare_kernel(config: BuildConfig) -> None:
    source_dir = config.kernel.source_dir
    if source_dir.exists():
        if not (source_dir / ".git").exists():
            raise BuildError(f"kernel source directory exists but is not a git repository: {source_dir}")
        run(["git", "-C", str(source_dir), "fetch", "--depth", "1", "origin", config.kernel.ref], verbose=config.verbose)
        run(["git", "-C", str(source_dir), "checkout", "FETCH_HEAD"], verbose=config.verbose)
        print(f"kernel source updated: {source_dir}")
        return

    source_dir.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "git",
            "clone",
            "--depth",
            "1",
            "--branch",
            config.kernel.ref,
            config.kernel.repo,
            str(source_dir),
        ],
        verbose=config.verbose,
    )
    print(f"kernel source prepared: {source_dir}")


def check_kernel(config: BuildConfig, *, source_override: str | None = None, force: bool = False) -> None:
    patches = discover_patches(config.kernel.patches_dir)
    print_patch_queue(patches)

    source_dir = Path(source_override) if source_override else config.kernel.source_dir
    if not source_dir.is_absolute():
        source_dir = config.root / source_dir
    if not source_dir.exists():
        raise BuildError(f"kernel source tree does not exist: {source_dir}")

    work_dir = config.kernel.check_dir
    _prepare_worktree_path(config, source_dir, work_dir, force)
    _create_worktree(config, source_dir, work_dir)
    try:
        _apply_patches(config, work_dir, patches)
        manifest_path = config.kernel_artifact_dir / "patches.manifest.json"
        write_manifest(
            manifest_path,
            {
                "kind": "kernel-patch-check",
                "board": config.board.name,
                "kernel_version": config.kernel.version,
                "source": _source_manifest(config, source_dir),
                "patches": patch_manifest(patches, config.root),
            },
        )
    except BuildError:
        print(f"kernel apply-check worktree left for inspection: {work_dir}")
        raise
    _remove_worktree(config, source_dir, work_dir)
    print("kernel apply-check passed")


def build_kernel(config: BuildConfig, *, jobs: int = 0, force: bool = False, prepare: bool = True) -> None:
    if prepare:
        prepare_kernel(config)

    source_dir = config.kernel.source_dir
    if not source_dir.exists():
        raise BuildError(f"kernel source tree does not exist: {source_dir}")

    patches = discover_patches(config.kernel.patches_dir)
    artifact_dir = config.kernel_artifact_dir
    work_dir = config.kernel.work_dir
    _prepare_worktree_path(config, source_dir, work_dir, force)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    _create_worktree(config, source_dir, work_dir)
    _apply_patches(config, work_dir, patches)
    _run_kernel_make(config, work_dir, jobs)
    _collect_kernel_artifacts(config, work_dir, artifact_dir)
    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "kernel",
            "board": config.board.name,
            "kernel_version": config.kernel.version,
            "source": _source_manifest(config, source_dir),
            "config": {
                "defconfig": config.kernel.defconfig,
                "arch": config.kernel.arch,
                "cross_compile": config.kernel.cross_compile,
            },
            "patches": patch_manifest(patches, config.root),
            "outputs": file_manifest(artifact_dir, config.root),
        },
    )
    print(f"kernel artifact: {artifact_dir}")


def _create_worktree(config: BuildConfig, source_dir: Path, work_dir: Path) -> None:
    work_dir.parent.mkdir(parents=True, exist_ok=True)
    run(
        ["git", "-C", str(source_dir), "worktree", "add", "--detach", str(work_dir), "HEAD"],
        verbose=config.verbose,
    )


def _prepare_worktree_path(config: BuildConfig, source_dir: Path, work_dir: Path, force: bool) -> None:
    if not work_dir.exists():
        return
    if not force:
        raise BuildError(f"path already exists, pass --force to replace it: {work_dir}")

    _remove_worktree(config, source_dir, work_dir)
    run(["git", "-C", str(source_dir), "worktree", "prune"], verbose=config.verbose)


def _remove_worktree(config: BuildConfig, source_dir: Path, work_dir: Path) -> None:
    try:
        run(
            ["git", "-C", str(source_dir), "worktree", "remove", "--force", str(work_dir)],
            verbose=config.verbose,
        )
    except BuildError:
        safe_rmtree(work_dir, config.paths.build_dir)


def _apply_patches(config: BuildConfig, work_dir: Path, patches: list[Path]) -> None:
    for index, patch in enumerate(patches, start=1):
        label = patch.relative_to(config.root)
        print(f"applying kernel patch {index}/{len(patches)}: {label}", flush=True)
        run(
            ["git", "-C", str(work_dir), "apply", "--check", "--whitespace=error", str(patch)],
            verbose=config.verbose,
        )
        run(["git", "-C", str(work_dir), "apply", str(patch)], verbose=config.verbose)


def _run_kernel_make(config: BuildConfig, work_dir: Path, jobs: int) -> None:
    env = {
        "ARCH": config.kernel.arch,
    }
    if config.kernel.cross_compile:
        env["CROSS_COMPILE"] = config.kernel.cross_compile

    if jobs <= 0:
        jobs = os.cpu_count() or 1

    run(["make", config.kernel.defconfig], cwd=work_dir, env=env, verbose=config.verbose)
    run(["make", f"-j{jobs}"], cwd=work_dir, env=env, verbose=config.verbose)
    modules_dir = config.kernel_artifact_dir / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    run(
        ["make", f"INSTALL_MOD_PATH={modules_dir}", "modules_install"],
        cwd=work_dir,
        env=env,
        verbose=config.verbose,
    )
    archive_zst(modules_dir, config.kernel_artifact_dir / "modules.tar.zst", verbose=config.verbose)


def _collect_kernel_artifacts(config: BuildConfig, work_dir: Path, artifact_dir: Path) -> None:
    boot_dir = artifact_dir / "boot"
    overlays_dir = boot_dir / "overlays"
    overlays_dir.mkdir(parents=True, exist_ok=True)

    image_src = work_dir / config.kernel.image_path
    if not image_src.exists():
        raise BuildError(f"kernel image was not produced: {image_src}")
    shutil.copy2(image_src, boot_dir / config.kernel.kernel_image_name)

    _copy_glob(work_dir, config.kernel.dtb_glob, boot_dir)
    _copy_glob(work_dir, config.kernel.dtbo_glob, overlays_dir)

    readme_src = work_dir / config.kernel.overlays_readme
    if readme_src.exists():
        shutil.copy2(readme_src, overlays_dir / "README")


def _copy_glob(work_dir: Path, pattern: str, dst: Path) -> None:
    matches = sorted(work_dir.glob(pattern))
    if not matches:
        raise BuildError(f"no files matched kernel artifact pattern: {pattern}")
    dst.mkdir(parents=True, exist_ok=True)
    for item in matches:
        shutil.copy2(item, dst / item.name)


def _source_manifest(config: BuildConfig, source_dir: Path) -> dict[str, str]:
    result = run(
        ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
        capture=True,
        verbose=config.verbose,
    )
    return {
        "version": config.kernel.version,
        "repo": config.kernel.repo,
        "ref": config.kernel.ref,
        "commit": (result.stdout or "").strip(),
    }
