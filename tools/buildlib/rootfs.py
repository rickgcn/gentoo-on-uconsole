from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.error import URLError

from .command import format_command, run
from .config import BuildConfig
from .disk import DiskIdentity, load_or_create_disk_identity
from .errors import BuildError
from .files import (
    archive_zst,
    copy_tree_contents,
    ensure_parent,
    extract_archive,
    require_empty_or_force,
    require_root,
    safe_rmtree,
)
from .manifest import file_manifest, write_manifest


QEMU_AARCH64_STATIC = Path("/usr/bin/qemu-aarch64-static")
USERNAME_PATTERN = re.compile(r"^[a-z_][a-z0-9_-]*[$]?$")
CHROOT_MOUNT_NAMES = ("proc", "sys", "dev", "run")


def prepare_rootfs(config: BuildConfig, *, force: bool = False) -> None:
    _prepare_stage3(config, force=force)
    _prepare_gentoo_repository(config, force=force)


def _prepare_stage3(config: BuildConfig, *, force: bool = False) -> None:
    stage3 = _stage3_path(config)
    if stage3.exists():
        if not force:
            _verify_stage3(config, stage3)
            print(f"rootfs stage3: {stage3}")
            return
        sources_dir = config.paths.sources_dir.resolve(strict=False)
        if sources_dir not in stage3.resolve(strict=False).parents:
            raise BuildError(f"refusing to replace stage3 outside sources directory: {stage3}")
        stage3.unlink()

    if not config.rootfs.stage3_url:
        raise BuildError("rootfs.stage3_url is not configured")

    _download_stage3(config, stage3)
    _verify_stage3(config, stage3)
    print(f"rootfs stage3: {stage3}")


def _prepare_gentoo_repository(config: BuildConfig, *, force: bool = False) -> None:
    repository_dir = config.rootfs.repository_dir
    if repository_dir.exists():
        if not force:
            _require_gentoo_repository(repository_dir)
            print(f"rootfs repository: {repository_dir}")
            return
        sources_dir = config.paths.sources_dir.resolve(strict=False)
        if sources_dir not in repository_dir.resolve(strict=False).parents:
            raise BuildError(f"refusing to replace repository outside sources directory: {repository_dir}")
        safe_rmtree(repository_dir, config.paths.sources_dir)

    if not config.rootfs.repository_url:
        raise BuildError("rootfs.repository_url is not configured")
    if not config.rootfs.repository_ref:
        raise BuildError("rootfs.repository_ref is not configured")

    repository_dir.mkdir(parents=True, exist_ok=True)
    run(["git", "-C", str(repository_dir), "init"], verbose=config.verbose)
    run(["git", "-C", str(repository_dir), "remote", "add", "origin", config.rootfs.repository_url], verbose=config.verbose)
    run(["git", "-C", str(repository_dir), "fetch", "--depth", "1", "origin", config.rootfs.repository_ref], verbose=config.verbose)
    run(["git", "-C", str(repository_dir), "checkout", "--force", "FETCH_HEAD"], verbose=config.verbose)
    _require_gentoo_repository(repository_dir)
    print(f"rootfs repository: {repository_dir}")


def build_rootfs(config: BuildConfig, *, force: bool = False, prepare: bool = True) -> None:
    _validate_rootfs_build_config(config)
    if prepare:
        prepare_rootfs(config)

    stage3 = _stage3_path(config)
    if not stage3.exists():
        raise BuildError(f"Gentoo stage3 archive does not exist: {stage3}")
    _verify_stage3(config, stage3)
    _require_gentoo_repository(config.rootfs.repository_dir)

    modules_archive = config.kernel_artifact_dir / "modules.tar.zst"
    if not modules_archive.exists():
        raise BuildError(f"kernel modules artifact is missing, run kernel build first: {modules_archive}")

    require_root("rootfs build")

    identity = load_or_create_disk_identity(config)
    artifact_dir = config.rootfs_artifact_dir
    work_dir = config.rootfs.work_dir
    _cleanup_chroot_mounts(config, work_dir)
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    extract_archive(stage3, work_dir, verbose=config.verbose)
    extract_archive(modules_archive, work_dir, keep_directory_symlink=True, verbose=config.verbose)
    _install_gentoo_repository(config, work_dir)
    _require_rootfs_profile(work_dir)
    _write_generated_files(config, identity, work_dir)
    copy_tree_contents(config.rootfs.overlay_dir, work_dir)
    _configure_chroot(config, work_dir)

    artifact_dir.mkdir(parents=True, exist_ok=True)
    archive = artifact_dir / "rootfs.tar.zst"
    archive_zst(work_dir, archive, verbose=config.verbose)
    write_manifest(
        artifact_dir / "manifest.json",
        {
            "kind": "rootfs",
            "board": config.board.name,
            "kernel_version": config.kernel.version,
            "inputs": {
                "stage3": str(stage3),
                "stage3_url": config.rootfs.stage3_url,
                "stage3_sha512": config.rootfs.stage3_sha512,
                "repository": _relative_or_absolute(config.rootfs.repository_dir, config.root),
                "repository_url": config.rootfs.repository_url,
                "repository_ref": config.rootfs.repository_ref,
                "repository_commit": _git_head(config.rootfs.repository_dir),
                "modules": str(modules_archive.relative_to(config.root)),
                "overlay_dir": _optional_relative(config.rootfs.overlay_dir, config.root),
                "disk_identity": str(config.disk_identity_path.relative_to(config.root)),
            },
            "rootfs": {
                "hostname": config.rootfs.hostname,
                "timezone": config.rootfs.timezone,
                "locale": config.rootfs.locale,
                "keymap": config.rootfs.keymap,
                "user": config.rootfs.user.name,
                "groups": list(config.rootfs.user.groups),
                "ssh_authorized_keys": config.rootfs.user.ssh_authorized_keys is not None,
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
    print(f"rootfs artifact: {archive}")


def _stage3_path(config: BuildConfig) -> Path:
    if config.rootfs.stage3:
        stage3 = Path(config.rootfs.stage3).expanduser()
        if not stage3.is_absolute():
            stage3 = config.root / stage3
        return stage3
    if not config.rootfs.stage3_url:
        raise BuildError("rootfs.stage3 or rootfs.stage3_url must be configured")
    return config.paths.sources_dir / "rootfs" / Path(config.rootfs.stage3_url).name


def _download_stage3(config: BuildConfig, stage3: Path) -> None:
    ensure_parent(stage3)
    part = stage3.with_name(stage3.name + ".part")
    if part.exists():
        part.unlink()
    try:
        with urllib.request.urlopen(config.rootfs.stage3_url) as response, part.open("wb") as handle:
            expected_size = _download_size(config, response)
            downloaded = 0
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                downloaded += len(chunk)
                _print_download_progress(downloaded, expected_size)
    except URLError as exc:
        if part.exists():
            part.unlink()
        raise BuildError(f"failed to download Gentoo stage3: {exc}") from exc
    print()
    part.replace(stage3)


def _download_size(config: BuildConfig, response: object) -> int:
    content_length = response.headers.get("Content-Length")
    if content_length:
        return int(content_length)
    return config.rootfs.stage3_size


def _print_download_progress(downloaded: int, expected_size: int) -> None:
    downloaded_mib = downloaded / 1024 / 1024
    if expected_size:
        expected_mib = expected_size / 1024 / 1024
        percent = downloaded / expected_size * 100
        print(f"\rdownloading stage3: {downloaded_mib:.1f}/{expected_mib:.1f} MiB ({percent:.1f}%)", end="", flush=True)
    else:
        print(f"\rdownloading stage3: {downloaded_mib:.1f} MiB", end="", flush=True)


def _verify_stage3(config: BuildConfig, stage3: Path) -> None:
    if config.rootfs.stage3_size and stage3.stat().st_size != config.rootfs.stage3_size:
        raise BuildError(f"Gentoo stage3 size does not match configuration: {stage3}")
    if config.rootfs.stage3_sha512:
        digest = _sha512(stage3)
        if digest != config.rootfs.stage3_sha512.lower():
            raise BuildError(f"Gentoo stage3 SHA512 does not match configuration: {stage3}")


def _sha512(path: Path) -> str:
    digest = hashlib.sha512()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_gentoo_repository(path: Path) -> None:
    if not path.is_dir():
        raise BuildError(f"Gentoo repository does not exist, run rootfs prepare first: {path}")
    required = [
        path / "eclass",
        path / "metadata",
        path / "profiles",
        path / "profiles/profiles.desc",
    ]
    missing = [item for item in required if not item.exists()]
    if missing:
        joined = ", ".join(str(item) for item in missing)
        raise BuildError(f"Gentoo repository is incomplete: {joined}")


def _install_gentoo_repository(config: BuildConfig, root: Path) -> None:
    source = config.rootfs.repository_dir
    _require_gentoo_repository(source)
    target = root / "var/db/repos/gentoo"
    if target.exists():
        safe_rmtree(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))


def _require_rootfs_profile(root: Path) -> None:
    profile = root / "etc/portage/make.profile"
    if not profile.is_symlink():
        raise BuildError(f"rootfs Portage profile must be a symlink: {profile}")
    target = profile.resolve(strict=False)
    if not target.exists():
        raise BuildError(f"rootfs Portage profile target does not exist: {profile} -> {target}")


def _git_head(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        result = run(["git", "-C", str(path), "rev-parse", "HEAD"], capture=True)
    except BuildError:
        return ""
    return (result.stdout or "").strip()


def _validate_rootfs_build_config(config: BuildConfig) -> None:
    user = config.rootfs.user
    if not user.name:
        raise BuildError("rootfs.user.name is required")
    if not USERNAME_PATTERN.fullmatch(user.name):
        raise BuildError(f"rootfs.user.name is not a valid Linux user name: {user.name}")
    if not user.password_hash:
        raise BuildError("rootfs.user.password_hash is required")
    if not user.password_hash.startswith("$"):
        raise BuildError("rootfs.user.password_hash must be a hashed password")
    if "wheel" not in user.groups:
        raise BuildError("rootfs.user.groups must include wheel for sudo access")
    if user.ssh_authorized_keys and not user.ssh_authorized_keys.exists():
        raise BuildError(f"SSH authorized_keys file does not exist: {user.ssh_authorized_keys}")


def _write_generated_files(config: BuildConfig, identity: DiskIdentity, root: Path) -> None:
    _write_text(
        root / "etc/fstab",
        (
            "# <filesystem> <mountpoint> <type> <options> <dump> <pass>\n"
            f"PARTUUID={identity.root_partuuid} / ext4 defaults,noatime 0 1\n"
            f"PARTUUID={identity.boot_partuuid} /boot vfat defaults,noatime 0 2\n"
        ),
    )
    _write_text(root / "etc/conf.d/hostname", f'hostname="{config.rootfs.hostname}"\n')
    _write_text(root / "etc/conf.d/keymaps", f'keymap="{config.rootfs.keymap}"\n')
    _write_text(root / "etc/timezone", f"{config.rootfs.timezone}\n")
    _write_text(root / "etc/env.d/02locale", f'LANG="{config.rootfs.locale}"\n')
    _ensure_line(root / "etc/locale.gen", f"{config.rootfs.locale} UTF-8")
    _write_portage_make_conf(config, root)
    _write_text(root / "etc/portage/package.license/gentoo-on-uconsole", "sys-kernel/linux-firmware *\n")
    _write_text(root / "etc/sudoers.d/wheel", "%wheel ALL=(ALL:ALL) ALL\n", mode=0o440)
    _write_text(
        root / "etc/init.d/ssh-hostkeys",
        (
            "#!/sbin/openrc-run\n"
            "\n"
            'description="Generate SSH host keys on first boot"\n'
            "\n"
            "depend() {\n"
            "    before sshd\n"
            "}\n"
            "\n"
            "start() {\n"
            "    if ! ls /etc/ssh/ssh_host_*_key >/dev/null 2>&1; then\n"
            '        ebegin "Generating SSH host keys"\n'
            "        ssh-keygen -A\n"
            "        eend $?\n"
            "    fi\n"
            "}\n"
        ),
        mode=0o755,
    )


def _write_portage_make_conf(config: BuildConfig, root: Path) -> None:
    if not config.rootfs.distfiles_mirrors:
        return
    make_conf = root / "etc/portage/make.conf"
    ensure_parent(make_conf)
    existing = make_conf.read_text(encoding="utf-8") if make_conf.exists() else ""
    lines = [
        line
        for line in existing.splitlines()
        if not line.strip().startswith("GENTOO_MIRRORS=")
    ]
    mirrors = " ".join(config.rootfs.distfiles_mirrors)
    lines.append(f'GENTOO_MIRRORS="{mirrors}"')
    make_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _configure_chroot(config: BuildConfig, root: Path) -> None:
    qemu_copied = _install_qemu_static(root)
    script = root / "tmp/gonu-rootfs-config.sh"
    authorized_keys = _install_authorized_keys(config, root)
    resolv_backup = _install_build_resolv_conf(root)
    _write_text(script, _chroot_script(config, authorized_keys is not None), mode=0o700)

    try:
        _mount_chroot(config, root)
        _run_chroot_config(config, root, qemu_copied)
    finally:
        _cleanup_chroot_mounts(config, root)
        _restore_resolv_conf(root, resolv_backup)
        _safe_unlink(script)
        if authorized_keys:
            _safe_unlink(authorized_keys)
        if qemu_copied:
            _safe_unlink(root / "usr/bin/qemu-aarch64-static")


def _install_qemu_static(root: Path) -> bool:
    if platform.machine() in {"aarch64", "arm64"}:
        return False
    if not QEMU_AARCH64_STATIC.exists():
        raise BuildError(f"qemu-aarch64-static is required for arm64 chroot on this host: {QEMU_AARCH64_STATIC}")
    target = root / "usr/bin/qemu-aarch64-static"
    ensure_parent(target)
    shutil.copy2(QEMU_AARCH64_STATIC, target)
    return True


def _install_authorized_keys(config: BuildConfig, root: Path) -> Path | None:
    source = config.rootfs.user.ssh_authorized_keys
    if not source:
        return None
    target = root / "tmp/gonu-authorized_keys"
    shutil.copy2(source, target)
    target.chmod(0o600)
    return target


def _install_build_resolv_conf(root: Path) -> Path | None:
    source = Path("/etc/resolv.conf")
    target = root / "etc/resolv.conf"
    backup = root / "etc/resolv.conf.gonu-backup"
    if target.exists():
        shutil.copy2(target, backup)
    if source.exists():
        shutil.copy2(source, target)
    return backup if backup.exists() else None


def _restore_resolv_conf(root: Path, backup: Path | None) -> None:
    target = root / "etc/resolv.conf"
    if backup:
        shutil.move(str(backup), target)
    else:
        _safe_unlink(target)


def _mount_chroot(config: BuildConfig, root: Path) -> None:
    mounts = [
        (["mount", "-t", "proc", "proc", str(root / "proc")], root / "proc", False),
        (["mount", "--rbind", "/sys", str(root / "sys")], root / "sys", True),
        (["mount", "--rbind", "/dev", str(root / "dev")], root / "dev", True),
        (["mount", "--rbind", "/run", str(root / "run")], root / "run", True),
    ]
    for args, mountpoint, make_rslave in mounts:
        mountpoint.mkdir(parents=True, exist_ok=True)
        run(args, verbose=config.verbose)
        if make_rslave:
            run(["mount", "--make-rslave", str(mountpoint)], verbose=config.verbose)


def _run_chroot_config(config: BuildConfig, root: Path, qemu_copied: bool) -> None:
    if qemu_copied:
        args = ["chroot", str(root), "/usr/bin/qemu-aarch64-static", "/bin/bash", "/tmp/gonu-rootfs-config.sh"]
    else:
        args = ["chroot", str(root), "/bin/bash", "/tmp/gonu-rootfs-config.sh"]
    _run_interruptible_process_group(args, env=_chroot_environment(), verbose=config.verbose)


def _chroot_environment() -> dict[str, str]:
    return {
        "HOME": "/root",
        "LANG": "C",
        "LC_ALL": "C",
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "SHELL": "/bin/bash",
        "TERM": os.environ.get("TERM") or "linux",
    }


def _run_interruptible_process_group(
    args: list[str],
    *,
    env: dict[str, str] | None = None,
    verbose: bool = False,
) -> None:
    if verbose:
        print(f"+ {format_command(args)}")
    process = subprocess.Popen(args, env=env, start_new_session=True)
    try:
        returncode = process.wait()
    except KeyboardInterrupt:
        _terminate_process_group(process)
        raise
    if returncode != 0:
        raise BuildError(f"command failed ({returncode}): {format_command(args)}")


def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()


def _chroot_script(config: BuildConfig, has_authorized_keys: bool) -> str:
    user = config.rootfs.user
    groups = ",".join(user.groups)
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        'export ACCEPT_LICENSE="*"',
        'export FEATURES="-ipc-sandbox -mount-sandbox -network-sandbox -pid-sandbox"',
        "emerge --usepkg=y --getbinpkg=y --with-bdeps=n --noreplace app-admin/sudo sys-kernel/linux-firmware",
        f"for group in {sh_quote_list(user.groups)}; do getent group \"$group\" >/dev/null || groupadd \"$group\"; done",
        f"if id -u {sh_quote(user.name)} >/dev/null 2>&1; then",
        f"    usermod -a -G {sh_quote(groups)} {sh_quote(user.name)}",
        "else",
        f"    useradd -m -s /bin/bash -G {sh_quote(groups)} {sh_quote(user.name)}",
        "fi",
        f"printf '%s:%s\\n' {sh_quote(user.name)} {sh_quote(user.password_hash)} | chpasswd -e",
    ]
    if has_authorized_keys:
        lines.extend(
            [
                f"install -d -m 0700 -o {sh_quote(user.name)} -g {sh_quote(user.name)} /home/{sh_quote(user.name)}/.ssh",
                (
                    f"install -m 0600 -o {sh_quote(user.name)} -g {sh_quote(user.name)} "
                    f"/tmp/gonu-authorized_keys /home/{sh_quote(user.name)}/.ssh/authorized_keys"
                ),
            ]
        )
    lines.extend(
        [
            "locale-gen",
            "env-update",
            "rm -f /etc/ssh/ssh_host_*_key /etc/ssh/ssh_host_*_key.pub",
            "add_service() { rc-update show \"$2\" | awk '{ print $1 }' | grep -qx \"$1\" || rc-update add \"$1\" \"$2\"; }",
            "add_service dhcpcd default",
            "add_service ssh-hostkeys default",
            "add_service sshd default",
        ]
    )
    return "\n".join(lines) + "\n"


def sh_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def sh_quote_list(values: tuple[str, ...]) -> str:
    return " ".join(sh_quote(value) for value in values)


def _write_text(path: Path, content: str, *, mode: int = 0o644) -> None:
    ensure_parent(path)
    path.write_text(content, encoding="utf-8")
    path.chmod(mode)


def _ensure_line(path: Path, line: str) -> None:
    ensure_parent(path)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    lines = existing.splitlines()
    if line not in lines:
        lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _cleanup_chroot_mounts(config: BuildConfig, root: Path) -> None:
    for mountpoint in _chroot_mountpoints(root):
        _best_effort_unmount(config, mountpoint)


def _chroot_mountpoints(root: Path) -> list[Path]:
    root = root.resolve(strict=False)
    roots = [root / name for name in CHROOT_MOUNT_NAMES]
    mountpoints = []
    for mountpoint in _current_mountpoints():
        if any(mountpoint == item or item in mountpoint.parents for item in roots):
            mountpoints.append(mountpoint)
    return sorted(mountpoints, key=lambda item: len(item.parts), reverse=True)


def _current_mountpoints() -> list[Path]:
    mountinfo = Path("/proc/self/mountinfo")
    try:
        lines = mountinfo.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    result = []
    for line in lines:
        fields = line.split()
        if len(fields) > 4:
            result.append(Path(_decode_mountinfo_path(fields[4])).resolve(strict=False))
    return result


def _decode_mountinfo_path(value: str) -> str:
    return bytes(value, "utf-8").decode("unicode_escape")


def _is_mountpoint(path: Path) -> bool:
    target = path.resolve(strict=False)
    return target in _current_mountpoints()


def _best_effort_unmount(config: BuildConfig, path: Path) -> None:
    if not _is_mountpoint(path):
        return
    try:
        run(["umount", "--recursive", str(path)], verbose=config.verbose)
    except BuildError:
        if not _is_mountpoint(path):
            return
        try:
            run(["umount", "--recursive", "--lazy", str(path)], verbose=config.verbose)
        except BuildError as exc:
            if _is_mountpoint(path):
                print(f"warning: failed to unmount {path}: {exc}", file=sys.stderr)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _optional_relative(path: Path, root: Path) -> str | None:
    if not path.exists():
        return None
    return str(path.relative_to(root))


def _relative_or_absolute(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
