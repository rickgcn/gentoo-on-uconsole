from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib
from typing import Any

from .errors import BuildError


@dataclass(frozen=True)
class BoardConfig:
    name: str
    family: str
    arch: str


@dataclass(frozen=True)
class PathsConfig:
    build_dir: Path
    sources_dir: Path
    work_dir: Path
    artifacts_dir: Path
    logs_dir: Path


@dataclass(frozen=True)
class KernelConfig:
    version: str
    repo: str
    ref: str
    source_dir: Path
    work_dir: Path
    check_dir: Path
    patches_dir: Path
    defconfig: str
    arch: str
    cross_compile: str
    image_path: str
    kernel_image_name: str
    dtb_glob: str
    dtbo_glob: str
    overlays_readme: str


@dataclass(frozen=True)
class FirmwareConfig:
    repo: str
    ref: str
    source_dir: Path
    work_dir: Path
    boot_files: tuple[str, ...]
    config_txt: tuple[str, ...]
    cmdline: tuple[str, ...]


@dataclass(frozen=True)
class RootfsConfig:
    stage3: str
    stage3_url: str
    stage3_sha512: str
    stage3_size: int
    repository_url: str
    repository_ref: str
    repository_dir: Path
    distfiles_mirrors: tuple[str, ...]
    overlay_dir: Path
    work_dir: Path
    hostname: str
    timezone: str
    locale: str
    keymap: str
    user: RootfsUserConfig


@dataclass(frozen=True)
class RootfsUserConfig:
    name: str
    password_hash: str
    groups: tuple[str, ...]
    ssh_authorized_keys: Path | None


@dataclass(frozen=True)
class DiskConfig:
    partition_table: str
    boot_label: str
    root_label: str


@dataclass(frozen=True)
class ImageConfig:
    name: str
    size_mib: int
    boot_size_mib: int
    work_dir: Path
    compress: bool


@dataclass(frozen=True)
class BuildConfig:
    root: Path
    config_path: Path
    board: BoardConfig
    paths: PathsConfig
    kernel: KernelConfig
    firmware: FirmwareConfig
    rootfs: RootfsConfig
    disk: DiskConfig
    image: ImageConfig
    verbose: bool = False

    @property
    def kernel_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "kernel" / self.kernel.version / self.board.name

    @property
    def firmware_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "firmware" / self.kernel.version / self.board.name

    @property
    def rootfs_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "rootfs" / self.kernel.version / self.board.name

    @property
    def image_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "images" / self.kernel.version / self.board.name

    @property
    def disk_identity_path(self) -> Path:
        return self.paths.work_dir / "disk" / self.kernel.version / self.board.name / "identity.toml"

    @classmethod
    def load(
        cls,
        root: Path,
        config_path: Path,
        *,
        local_config_paths: list[Path] | None = None,
        verbose: bool = False,
    ) -> "BuildConfig":
        if not config_path.exists():
            raise BuildError(f"configuration file does not exist: {config_path}")
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)
        for local_config_path in local_config_paths or []:
            if not local_config_path.exists():
                raise BuildError(f"local configuration file does not exist: {local_config_path}")
            with local_config_path.open("rb") as handle:
                data = _merge_dict(data, tomllib.load(handle))

        try:
            paths = PathsConfig(
                build_dir=_path(root, data["paths"]["build_dir"]),
                sources_dir=_path(root, data["paths"]["sources_dir"]),
                work_dir=_path(root, data["paths"]["work_dir"]),
                artifacts_dir=_path(root, data["paths"]["artifacts_dir"]),
                logs_dir=_path(root, data["paths"]["logs_dir"]),
            )
            board = BoardConfig(
                name=data["board"]["name"],
                family=data["board"]["family"],
                arch=data["board"]["arch"],
            )
            kernel_data = data["kernel"]
            kernel_version = kernel_data.get("version") or _kernel_version_from_ref(kernel_data["ref"])
            kernel = KernelConfig(
                version=kernel_version,
                repo=kernel_data["repo"],
                ref=kernel_data["ref"],
                source_dir=_path(
                    root,
                    kernel_data.get("source_dir", str(paths.sources_dir / "kernel" / kernel_version / "linux")),
                ),
                work_dir=_path(
                    root,
                    kernel_data.get("work_dir", str(paths.work_dir / "kernel" / kernel_version / "linux")),
                ),
                check_dir=_path(
                    root,
                    kernel_data.get("check_dir", str(paths.work_dir / "kernel" / kernel_version / "apply-check")),
                ),
                patches_dir=_path(root, kernel_data.get("patches_dir", f"kernel/{kernel_version}/patches")),
                defconfig=kernel_data["defconfig"],
                arch=kernel_data["arch"],
                cross_compile=kernel_data.get("cross_compile", ""),
                image_path=kernel_data["image_path"],
                kernel_image_name=kernel_data["kernel_image_name"],
                dtb_glob=kernel_data["dtb_glob"],
                dtbo_glob=kernel_data["dtbo_glob"],
                overlays_readme=kernel_data["overlays_readme"],
            )
            firmware = FirmwareConfig(
                repo=data["firmware"]["repo"],
                ref=data["firmware"]["ref"],
                source_dir=_path(root, data["firmware"]["source_dir"]),
                work_dir=_path(root, data["firmware"]["work_dir"]),
                boot_files=tuple(data["firmware"]["boot_files"]),
                config_txt=tuple(data["firmware"]["config_txt"]),
                cmdline=tuple(data["firmware"]["cmdline"]),
            )
            rootfs_data = data["rootfs"]
            rootfs_user_data = rootfs_data.get("user", {})
            rootfs = RootfsConfig(
                stage3=rootfs_data.get("stage3", ""),
                stage3_url=rootfs_data.get("stage3_url", ""),
                stage3_sha512=rootfs_data.get("stage3_sha512", ""),
                stage3_size=int(rootfs_data.get("stage3_size", 0)),
                repository_url=rootfs_data.get("repository_url", ""),
                repository_ref=rootfs_data.get("repository_ref", ""),
                repository_dir=_path(root, rootfs_data["repository_dir"]),
                distfiles_mirrors=tuple(rootfs_data.get("distfiles_mirrors", [])),
                overlay_dir=_path(root, rootfs_data["overlay_dir"]),
                work_dir=_path(root, rootfs_data["work_dir"]),
                hostname=rootfs_data["hostname"],
                timezone=rootfs_data["timezone"],
                locale=rootfs_data["locale"],
                keymap=rootfs_data["keymap"],
                user=RootfsUserConfig(
                    name=rootfs_user_data.get("name", ""),
                    password_hash=rootfs_user_data.get("password_hash", ""),
                    groups=tuple(rootfs_user_data.get("groups", [])),
                    ssh_authorized_keys=_optional_path(root, rootfs_user_data.get("ssh_authorized_keys")),
                ),
            )
            disk_data = data["disk"]
            disk = DiskConfig(
                partition_table=_partition_table(disk_data["partition_table"]),
                boot_label=_label(disk_data["boot_label"], "disk.boot_label", max_length=11),
                root_label=_label(disk_data["root_label"], "disk.root_label", max_length=16),
            )
            image = ImageConfig(
                name=data["image"]["name"],
                size_mib=int(data["image"]["size_mib"]),
                boot_size_mib=int(data["image"]["boot_size_mib"]),
                work_dir=_path(root, data["image"]["work_dir"]),
                compress=bool(data["image"].get("compress", True)),
            )
        except KeyError as exc:
            raise BuildError(f"missing required configuration key: {exc}") from exc

        return cls(
            root=root,
            config_path=config_path,
            board=board,
            paths=paths,
            kernel=kernel,
            firmware=firmware,
            rootfs=rootfs,
            disk=disk,
            image=image,
            verbose=verbose,
        )


def _path(root: Path, value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return root / path


def _optional_path(root: Path, value: str | None) -> Path | None:
    if not value:
        return None
    return _path(root, value)


def _kernel_version_from_ref(ref: str) -> str:
    if ref.startswith("rpi-"):
        return ref.removeprefix("rpi-")
    return ref


def _merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_dict(merged[key], value)
        else:
            merged[key] = value
    return merged


def _partition_table(value: str) -> str:
    normalized = value.lower()
    if normalized != "gpt":
        raise BuildError(f"unsupported disk.partition_table: {value}")
    return normalized


def _label(value: str, key: str, *, max_length: int) -> str:
    if not value:
        raise BuildError(f"{key} must not be empty")
    if len(value) > max_length:
        raise BuildError(f"{key} must be {max_length} characters or fewer: {value}")
    return value
