from __future__ import annotations

from dataclasses import dataclass
import hashlib
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import tomllib
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
CHROOT_MOUNT_NAMES = ("proc", "sys", "dev", "run", "var/cache/distfiles", "var/cache/binpkgs")
ROOTFS_PROFILE_DIR = Path("rootfs/profiles")
BASE_PROFILE = "base"
SERVICE_RUNLEVELS = ("boot", "default", "nonetwork", "shutdown", "sysinit")
LOCAL_REPOSITORY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
EMERGE_ROOTFS_INSTALL_OPTIONS = (
    "--usepkg=y",
    "--getbinpkg=y",
    "--buildpkg=y",
    "--binpkg-respect-use=y",
    "--with-bdeps=n",
    "--noreplace",
)
EMERGE_ROOTFS_BOOTSTRAP_OPTIONS = (
    "--usepkg=y",
    "--getbinpkg=y",
    "--buildpkg=y",
    "--binpkg-respect-use=y",
    "--with-bdeps=n",
    "--oneshot",
)
EMERGE_ROOTFS_REBUILD_OPTIONS = (
    "--usepkg=y",
    "--getbinpkg=y",
    "--buildpkg=y",
    "--binpkg-respect-use=y",
    "--with-bdeps=n",
    "--oneshot",
    "--newuse",
)


@dataclass(frozen=True)
class RootfsService:
    name: str
    runlevel: str


@dataclass(frozen=True)
class RootfsProfileFile:
    source: Path
    target: Path
    mode: int | None


@dataclass(frozen=True)
class RootfsBootstrap:
    packages: tuple[str, ...]
    use: tuple[str, ...]
    rebuild: tuple[str, ...]


@dataclass(frozen=True)
class RootfsMakeConf:
    key: str
    value: str


@dataclass(frozen=True)
class RootfsProfile:
    name: str
    paths: tuple[Path, ...]
    portage_profile: str
    packages: tuple[str, ...]
    rebuild: tuple[str, ...]
    groups: tuple[str, ...]
    bootstrap: tuple[RootfsBootstrap, ...]
    make_conf: tuple[RootfsMakeConf, ...]
    package_use: tuple[str, ...]
    accept_keywords: tuple[str, ...]
    package_license: tuple[str, ...]
    services: tuple[RootfsService, ...]
    files: tuple[RootfsProfileFile, ...]


@dataclass(frozen=True)
class RootfsCache:
    distfiles_dir: Path
    binpkgs_dir: Path


@dataclass(frozen=True)
class LocalPortageRepository:
    name: str
    source: Path


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
    profile = _load_rootfs_profile(config)
    local_portage_repositories = _local_portage_repositories(config)
    _validate_rootfs_build_config(config, profile)
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
    cache = _prepare_rootfs_cache(config)
    _cleanup_chroot_mounts(config, work_dir)
    _seed_rootfs_cache_from_workdir(work_dir, cache)
    require_empty_or_force(work_dir, force=force, allowed_root=config.paths.build_dir)
    require_empty_or_force(artifact_dir, force=force, allowed_root=config.paths.artifacts_dir)

    work_dir.mkdir(parents=True, exist_ok=True)
    extract_archive(stage3, work_dir, verbose=config.verbose)
    extract_archive(modules_archive, work_dir, keep_directory_symlink=True, verbose=config.verbose)
    _install_gentoo_repository(config, work_dir)
    _install_local_portage_repositories(local_portage_repositories, work_dir)
    _configure_portage_profile(profile, work_dir)
    _require_rootfs_profile(work_dir)
    _write_generated_files(config, identity, profile, work_dir)
    copy_tree_contents(config.rootfs.overlay_dir, work_dir)
    _configure_chroot(config, profile, work_dir)

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
                "portage_repositories_dir": _optional_relative(config.rootfs.portage_repositories_dir, config.root),
                "portage_repositories": [
                    {
                        "name": item.name,
                        "source": _relative_or_absolute(item.source, config.root),
                    }
                    for item in local_portage_repositories
                ],
                "modules": str(modules_archive.relative_to(config.root)),
                "overlay_dir": _optional_relative(config.rootfs.overlay_dir, config.root),
                "disk_identity": str(config.disk_identity_path.relative_to(config.root)),
                "profiles": [str(path.relative_to(config.root)) for path in profile.paths],
                "portage_profile": profile.portage_profile,
                "cache": {
                    "distfiles": _relative_or_absolute(cache.distfiles_dir, config.root),
                    "binpkgs": _relative_or_absolute(cache.binpkgs_dir, config.root),
                },
            },
            "rootfs": {
                "hostname": config.rootfs.hostname,
                "timezone": config.rootfs.timezone,
                "locale": config.rootfs.locale,
                "keymap": config.rootfs.keymap,
                "profile": profile.name,
                "jobs": config.rootfs.jobs,
                "emerge_jobs": config.rootfs.emerge_jobs,
                "emerge_load_average": config.rootfs.emerge_load_average,
                "user": config.rootfs.user.name,
                "groups": list(_user_groups(config, profile)),
                "ssh_authorized_keys": config.rootfs.user.ssh_authorized_keys is not None,
                "bootstrap": [
                    {
                        "packages": list(item.packages),
                        "use": list(item.use),
                        "rebuild": list(item.rebuild),
                    }
                    for item in profile.bootstrap
                ],
                "make_conf": {item.key: item.value for item in profile.make_conf},
                "package_use": list(profile.package_use),
                "packages": list(profile.packages),
                "rebuild": list(profile.rebuild),
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


def _local_portage_repositories(config: BuildConfig) -> tuple[LocalPortageRepository, ...]:
    repositories_dir = config.rootfs.portage_repositories_dir
    if not repositories_dir.exists():
        return ()
    if not repositories_dir.is_dir():
        raise BuildError(f"rootfs.portage_repositories_dir is not a directory: {repositories_dir}")

    repositories = []
    for source in sorted(path for path in repositories_dir.iterdir() if path.is_dir()):
        repositories.append(_read_local_portage_repository(source))
    return tuple(repositories)


def _read_local_portage_repository(source: Path) -> LocalPortageRepository:
    repo_name_path = source / "profiles/repo_name"
    layout_path = source / "metadata/layout.conf"
    if not repo_name_path.exists():
        raise BuildError(f"local Portage repository is missing profiles/repo_name: {source}")
    if not layout_path.exists():
        raise BuildError(f"local Portage repository is missing metadata/layout.conf: {source}")

    name = repo_name_path.read_text(encoding="utf-8").strip()
    if not LOCAL_REPOSITORY_NAME_PATTERN.fullmatch(name):
        raise BuildError(f"local Portage repository name is invalid: {name}")
    if name != source.name:
        raise BuildError(f"local Portage repository directory must match repo_name: {source.name} != {name}")
    return LocalPortageRepository(name=name, source=source)


def _install_local_portage_repositories(repositories: tuple[LocalPortageRepository, ...], root: Path) -> None:
    for repository in repositories:
        target = root / "var/db/repos" / repository.name
        if target.exists():
            safe_rmtree(target, root)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(repository.source, target, symlinks=True, ignore=shutil.ignore_patterns(".git"))
        _write_local_portage_repository_config(repository, root)


def _write_local_portage_repository_config(repository: LocalPortageRepository, root: Path) -> None:
    config = (
        f"[{repository.name}]\n"
        f"location = /var/db/repos/{repository.name}\n"
        "masters = gentoo\n"
        "auto-sync = no\n"
    )
    repos_conf = root / "etc/portage/repos.conf"
    if repos_conf.exists() and repos_conf.is_file():
        existing = repos_conf.read_text(encoding="utf-8")
        separator = "" if existing.endswith("\n") else "\n"
        repos_conf.write_text(existing + separator + "\n" + config, encoding="utf-8")
        return
    _write_text(repos_conf / f"{repository.name}.conf", config)


def _configure_portage_profile(profile: RootfsProfile, root: Path) -> None:
    if not profile.portage_profile:
        return
    profile_target = root / "var/db/repos/gentoo/profiles" / profile.portage_profile
    if not profile_target.exists():
        raise BuildError(f"Portage profile does not exist: {profile.portage_profile}")

    make_profile = root / "etc/portage/make.profile"
    ensure_parent(make_profile)
    if make_profile.exists() and make_profile.is_dir() and not make_profile.is_symlink():
        raise BuildError(f"refusing to replace directory with Portage profile symlink: {make_profile}")
    if make_profile.exists() or make_profile.is_symlink():
        make_profile.unlink()
    make_profile.symlink_to(os.path.relpath(profile_target, make_profile.parent))


def _require_rootfs_profile(root: Path) -> None:
    profile = root / "etc/portage/make.profile"
    if not profile.is_symlink():
        raise BuildError(f"rootfs Portage profile must be a symlink: {profile}")
    target = profile.resolve(strict=False)
    if not target.exists():
        raise BuildError(f"rootfs Portage profile target does not exist: {profile} -> {target}")


def _load_rootfs_profile(config: BuildConfig) -> RootfsProfile:
    names = [BASE_PROFILE]
    if config.rootfs.desktop.profile != "none":
        names.append(config.rootfs.desktop.profile)
    profiles = [_load_rootfs_profile_file(config, name) for name in names]
    return _merge_rootfs_profiles(profiles)


def _load_rootfs_profile_file(config: BuildConfig, name: str) -> RootfsProfile:
    path = config.root / ROOTFS_PROFILE_DIR / f"{name}.toml"
    if not path.exists():
        raise BuildError(f"rootfs profile does not exist: {path}")
    with path.open("rb") as handle:
        data = tomllib.load(handle)

    profile_data = data.get("profile", {})
    if not isinstance(profile_data, dict):
        raise BuildError(f"rootfs profile [profile] must be a table: {path}")
    profile_name = _profile_name(profile_data.get("name", name))
    if profile_name != name:
        raise BuildError(f"rootfs profile name mismatch: expected {name}, got {profile_name}")

    return RootfsProfile(
        name=profile_name,
        paths=(path,),
        portage_profile=_profile_portage_profile(profile_data),
        packages=_string_tuple(profile_data, "packages"),
        rebuild=_string_tuple(profile_data, "rebuild"),
        groups=_string_tuple(profile_data, "groups"),
        bootstrap=_profile_bootstrap(profile_data),
        make_conf=_profile_make_conf(profile_data),
        package_use=_string_tuple(profile_data, "package_use"),
        accept_keywords=_string_tuple(profile_data, "accept_keywords"),
        package_license=_string_tuple(profile_data, "package_license"),
        services=_profile_services(profile_data),
        files=_profile_files(path, profile_data),
    )


def _merge_rootfs_profiles(profiles: list[RootfsProfile]) -> RootfsProfile:
    services: dict[str, RootfsService] = {}
    for profile in profiles:
        for service in profile.services:
            services[service.name] = service
    portage_profiles = _dedupe(profile.portage_profile for profile in profiles if profile.portage_profile)
    if len(portage_profiles) > 1:
        joined = ", ".join(portage_profiles)
        raise BuildError(f"rootfs profiles select conflicting Portage profiles: {joined}")

    return RootfsProfile(
        name="+".join(profile.name for profile in profiles),
        paths=tuple(path for profile in profiles for path in profile.paths),
        portage_profile=portage_profiles[0] if portage_profiles else "",
        packages=_dedupe(item for profile in profiles for item in profile.packages),
        rebuild=_dedupe(item for profile in profiles for item in profile.rebuild),
        groups=_dedupe(item for profile in profiles for item in profile.groups),
        bootstrap=tuple(item for profile in profiles for item in profile.bootstrap),
        make_conf=_merge_make_conf(profile.make_conf for profile in profiles),
        package_use=_dedupe(item for profile in profiles for item in profile.package_use),
        accept_keywords=_dedupe(item for profile in profiles for item in profile.accept_keywords),
        package_license=_dedupe(item for profile in profiles for item in profile.package_license),
        services=tuple(services.values()),
        files=tuple(item for profile in profiles for item in profile.files),
    )


def _profile_name(value: object) -> str:
    if not isinstance(value, str):
        raise BuildError(f"invalid rootfs profile name: {value}")
    normalized = value.lower()
    if not normalized or not normalized.replace("-", "").replace("_", "").isalnum():
        raise BuildError(f"invalid rootfs profile name: {value}")
    return normalized


def _profile_portage_profile(data: dict[str, object]) -> str:
    value = data.get("portage_profile", "")
    if not isinstance(value, str):
        raise BuildError("rootfs profile portage_profile must be a string")
    if not value:
        return ""
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise BuildError(f"rootfs profile portage_profile must be a relative Gentoo profile path: {value}")
    return path.as_posix()


def _string_tuple(data: dict[str, object], key: str) -> tuple[str, ...]:
    values = data.get(key, [])
    if not isinstance(values, list):
        raise BuildError(f"rootfs profile {key} must be a list")
    result = []
    for value in values:
        if not isinstance(value, str) or not value:
            raise BuildError(f"rootfs profile {key} entries must be non-empty strings")
        result.append(value)
    return tuple(result)


def _profile_bootstrap(data: dict[str, object]) -> tuple[RootfsBootstrap, ...]:
    values = data.get("bootstrap", [])
    if not isinstance(values, list):
        raise BuildError("rootfs profile bootstrap must be a list")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise BuildError("rootfs profile bootstrap entries must be tables")
        packages = _string_tuple(value, "packages")
        if not packages:
            raise BuildError("rootfs profile bootstrap packages must not be empty")
        result.append(
            RootfsBootstrap(
                packages=packages,
                use=_string_tuple(value, "use"),
                rebuild=_string_tuple(value, "rebuild"),
            )
        )
    return tuple(result)


def _profile_make_conf(data: dict[str, object]) -> tuple[RootfsMakeConf, ...]:
    values = data.get("make_conf", [])
    if not isinstance(values, list):
        raise BuildError("rootfs profile make_conf must be a list")
    result = []
    for value in values:
        if not isinstance(value, dict):
            raise BuildError("rootfs profile make_conf entries must be tables")
        key = value.get("key")
        make_conf_value = value.get("value")
        if not isinstance(key, str) or not key or not key.replace("_", "").isalnum() or key[0].isdigit():
            raise BuildError(f"rootfs profile make_conf.key is invalid: {key}")
        if not isinstance(make_conf_value, str):
            raise BuildError(f"rootfs profile make_conf.value must be a string for {key}")
        result.append(RootfsMakeConf(key=key, value=make_conf_value))
    return tuple(result)


def _merge_make_conf(values: object) -> tuple[RootfsMakeConf, ...]:
    settings: dict[str, str] = {}
    for profile_values in values:
        for item in profile_values:
            settings[item.key] = item.value
    return tuple(RootfsMakeConf(key=key, value=value) for key, value in settings.items())


def _profile_services(data: dict[str, object]) -> tuple[RootfsService, ...]:
    values = data.get("services", [])
    if not isinstance(values, list):
        raise BuildError("rootfs profile services must be a list")
    services = []
    for value in values:
        if not isinstance(value, dict):
            raise BuildError("rootfs profile service entries must be tables")
        name = value.get("name")
        runlevel = value.get("runlevel")
        if not isinstance(name, str) or not name:
            raise BuildError("rootfs profile service.name must be a non-empty string")
        if not isinstance(runlevel, str) or runlevel not in SERVICE_RUNLEVELS:
            raise BuildError(f"rootfs profile service.runlevel is invalid for {name}: {runlevel}")
        services.append(RootfsService(name=name, runlevel=runlevel))
    return tuple(services)


def _profile_files(profile_path: Path, data: dict[str, object]) -> tuple[RootfsProfileFile, ...]:
    values = data.get("files", [])
    if not isinstance(values, list):
        raise BuildError("rootfs profile files must be a list")
    files = []
    for value in values:
        if not isinstance(value, dict):
            raise BuildError("rootfs profile file entries must be tables")
        source = value.get("source")
        target = value.get("target")
        mode = value.get("mode")
        if not isinstance(source, str) or not source:
            raise BuildError("rootfs profile file.source must be a non-empty string")
        if Path(source).is_absolute():
            raise BuildError(f"rootfs profile file.source must be relative: {source}")
        if not isinstance(target, str) or not target.startswith("/"):
            raise BuildError(f"rootfs profile file.target must be an absolute path: {target}")
        source_path = profile_path.parent / source
        if not source_path.exists():
            raise BuildError(f"rootfs profile file source does not exist: {source_path}")
        files.append(
            RootfsProfileFile(
                source=source_path,
                target=Path(target),
                mode=_profile_file_mode(mode),
            )
        )
    return tuple(files)


def _profile_file_mode(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value, 8)
        except ValueError as exc:
            raise BuildError(f"rootfs profile file.mode must be an octal string: {value}") from exc
    raise BuildError(f"rootfs profile file.mode must be an octal string: {value}")


def _dedupe(values: object) -> tuple:
    result = []
    seen = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _git_head(path: Path) -> str:
    if not (path / ".git").exists():
        return ""
    try:
        result = run(["git", "-C", str(path), "rev-parse", "HEAD"], capture=True)
    except BuildError:
        return ""
    return (result.stdout or "").strip()


def _validate_rootfs_build_config(config: BuildConfig, profile: RootfsProfile) -> None:
    user = config.rootfs.user
    if not user.name:
        raise BuildError("rootfs.user.name is required")
    if not USERNAME_PATTERN.fullmatch(user.name):
        raise BuildError(f"rootfs.user.name is not a valid Linux user name: {user.name}")
    if not user.password_hash:
        raise BuildError("rootfs.user.password_hash is required")
    if not user.password_hash.startswith("$"):
        raise BuildError("rootfs.user.password_hash must be a hashed password")
    if "wheel" not in _user_groups(config, profile):
        raise BuildError("rootfs.user.groups must include wheel for sudo access")
    if user.ssh_authorized_keys and not user.ssh_authorized_keys.exists():
        raise BuildError(f"SSH authorized_keys file does not exist: {user.ssh_authorized_keys}")


def _prepare_rootfs_cache(config: BuildConfig) -> RootfsCache:
    cache = _rootfs_cache(config)
    cache.distfiles_dir.mkdir(parents=True, exist_ok=True)
    cache.binpkgs_dir.mkdir(parents=True, exist_ok=True)
    print(f"rootfs cache: {config.paths.cache_dir / 'rootfs'}")
    return cache


def _rootfs_cache(config: BuildConfig) -> RootfsCache:
    cache_dir = config.paths.cache_dir / "rootfs"
    return RootfsCache(
        distfiles_dir=cache_dir / "distfiles",
        binpkgs_dir=cache_dir / "binpkgs",
    )


def _seed_rootfs_cache_from_workdir(work_dir: Path, cache: RootfsCache) -> None:
    _seed_cache_dir(work_dir / "var/cache/distfiles", cache.distfiles_dir)
    _seed_cache_dir(work_dir / "var/cache/binpkgs", cache.binpkgs_dir)


def _seed_cache_dir(source: Path, target: Path) -> None:
    if not source.exists() or not source.is_dir():
        return
    entries = list(source.iterdir())
    if not entries:
        return
    if source.resolve(strict=False) == target.resolve(strict=False):
        return
    target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for item in entries:
        destination = target / item.name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
            copied += 1
        elif _should_copy_cache_file(item, destination):
            shutil.copy2(item, destination)
            copied += 1
    if copied:
        print(f"rootfs cache seeded from {source}: {copied} entries")


def _should_copy_cache_file(source: Path, target: Path) -> bool:
    if not target.exists():
        return True
    try:
        source_stat = source.stat()
        target_stat = target.stat()
    except OSError:
        return True
    return source_stat.st_size != target_stat.st_size


def _write_generated_files(config: BuildConfig, identity: DiskIdentity, profile: RootfsProfile, root: Path) -> None:
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
    _write_portage_make_conf(config, profile, root)
    _write_profile_portage_config(profile, root)
    _install_profile_files(profile, root)
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


def _user_groups(config: BuildConfig, profile: RootfsProfile) -> tuple[str, ...]:
    return _dedupe((*config.rootfs.user.groups, *profile.groups))


def _write_portage_make_conf(config: BuildConfig, profile: RootfsProfile, root: Path) -> None:
    make_conf = root / "etc/portage/make.conf"
    ensure_parent(make_conf)
    existing = make_conf.read_text(encoding="utf-8") if make_conf.exists() else ""
    settings = {
        "DISTDIR": "/var/cache/distfiles",
        "MAKEOPTS": f"-j{config.rootfs.jobs} -l{config.rootfs.jobs}",
        "NINJAOPTS": f"-j{config.rootfs.jobs} -l{config.rootfs.jobs}",
        "PKGDIR": "/var/cache/binpkgs",
    }
    if config.rootfs.distfiles_mirrors:
        settings["GENTOO_MIRRORS"] = " ".join(config.rootfs.distfiles_mirrors)
    for item in profile.make_conf:
        settings[item.key] = item.value
    managed_keys = {"DISTDIR", "GENTOO_MIRRORS", "MAKEOPTS", "NINJAOPTS", "PKGDIR", "USE", *settings.keys()}
    lines = [
        line
        for line in existing.splitlines()
        if not any(line.strip().startswith(f"{key}=") for key in managed_keys)
    ]
    for key, value in settings.items():
        lines.append(f'{key}="{_make_conf_quote(value)}"')
    make_conf.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_conf_quote(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def _write_profile_portage_config(profile: RootfsProfile, root: Path) -> None:
    _write_portage_lines(root / "etc/portage/package.use/gentoo-on-uconsole", profile.package_use)
    _write_portage_lines(root / "etc/portage/package.accept_keywords/gentoo-on-uconsole", profile.accept_keywords)
    _write_portage_lines(root / "etc/portage/package.license/gentoo-on-uconsole", profile.package_license)


def _write_portage_lines(path: Path, lines: tuple[str, ...]) -> None:
    if not lines:
        return
    _write_text(path, "\n".join(lines) + "\n")


def _install_profile_files(profile: RootfsProfile, root: Path) -> None:
    for item in profile.files:
        target = root / item.target.relative_to("/")
        ensure_parent(target)
        shutil.copy2(item.source, target)
        if item.mode is not None:
            target.chmod(item.mode)


def _configure_chroot(config: BuildConfig, profile: RootfsProfile, root: Path) -> None:
    qemu_copied = _install_qemu_static(root)
    script = root / "tmp/gonu-rootfs-config.sh"
    authorized_keys = _install_authorized_keys(config, root)
    resolv_backup = _install_build_resolv_conf(root)
    _write_text(script, _chroot_script(config, profile, authorized_keys is not None), mode=0o700)

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
    cache = _rootfs_cache(config)
    mounts = [
        (["mount", "-t", "proc", "proc", str(root / "proc")], root / "proc", False),
        (["mount", "--rbind", "/sys", str(root / "sys")], root / "sys", True),
        (["mount", "--rbind", "/dev", str(root / "dev")], root / "dev", True),
        (["mount", "--rbind", "/run", str(root / "run")], root / "run", True),
        (
            ["mount", "--bind", str(cache.distfiles_dir), str(root / "var/cache/distfiles")],
            root / "var/cache/distfiles",
            False,
        ),
        (
            ["mount", "--bind", str(cache.binpkgs_dir), str(root / "var/cache/binpkgs")],
            root / "var/cache/binpkgs",
            False,
        ),
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


def _chroot_script(config: BuildConfig, profile: RootfsProfile, has_authorized_keys: bool) -> str:
    user = config.rootfs.user
    user_groups = _user_groups(config, profile)
    groups = ",".join(user_groups)
    lines = [
        "#!/bin/bash",
        "set -euo pipefail",
        'export ACCEPT_LICENSE="*"',
        'export FEATURES="-ipc-sandbox -mount-sandbox -network-sandbox -pid-sandbox"',
    ]
    for item in profile.bootstrap:
        lines.append(_emerge_command(config, EMERGE_ROOTFS_BOOTSTRAP_OPTIONS, item.packages, use=item.use))
    if profile.packages:
        lines.append(_emerge_command(config, EMERGE_ROOTFS_INSTALL_OPTIONS, profile.packages))
    for item in profile.bootstrap:
        if item.rebuild:
            lines.append(_emerge_command(config, EMERGE_ROOTFS_REBUILD_OPTIONS, item.rebuild))
    if profile.rebuild:
        lines.append(_emerge_command(config, EMERGE_ROOTFS_REBUILD_OPTIONS, profile.rebuild))
    lines.extend(
        [
            f"for group in {sh_quote_list(user_groups)}; do getent group \"$group\" >/dev/null || groupadd \"$group\"; done",
            f"if id -u {sh_quote(user.name)} >/dev/null 2>&1; then",
            f"    usermod -a -G {sh_quote(groups)} {sh_quote(user.name)}",
            "else",
            f"    useradd -m -s /bin/bash -G {sh_quote(groups)} {sh_quote(user.name)}",
            "fi",
            f"printf '%s:%s\\n' {sh_quote(user.name)} {sh_quote(user.password_hash)} | chpasswd -e",
        ]
    )
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
        ]
    )
    for service in profile.services:
        lines.append(f"add_service {sh_quote(service.name)} {sh_quote(service.runlevel)}")
    return "\n".join(lines) + "\n"


def _emerge_command(
    config: BuildConfig,
    options: tuple[str, ...],
    packages: tuple[str, ...],
    *,
    use: tuple[str, ...] = (),
) -> str:
    command_options = (
        *options,
        f"--jobs={config.rootfs.emerge_jobs}",
        f"--load-average={config.rootfs.emerge_load_average}",
    )
    command = "emerge " + " ".join(command_options) + " " + sh_quote_list(packages)
    if use:
        command = "USE=" + sh_quote(" ".join(use)) + " " + command
    return command


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
