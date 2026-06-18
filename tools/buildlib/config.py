from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib

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
    files_dir: Path
    work_dir: Path


@dataclass(frozen=True)
class RootfsConfig:
    stage3: str
    files_dir: Path
    work_dir: Path


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
    image: ImageConfig
    verbose: bool = False

    @property
    def kernel_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "kernel" / self.board.name

    @property
    def firmware_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "firmware" / self.board.name

    @property
    def rootfs_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "rootfs" / self.board.name

    @property
    def image_artifact_dir(self) -> Path:
        return self.paths.artifacts_dir / "images"

    @classmethod
    def load(cls, root: Path, config_path: Path, *, verbose: bool = False) -> "BuildConfig":
        if not config_path.exists():
            raise BuildError(f"configuration file does not exist: {config_path}")
        with config_path.open("rb") as handle:
            data = tomllib.load(handle)

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
            kernel = KernelConfig(
                repo=data["kernel"]["repo"],
                ref=data["kernel"]["ref"],
                source_dir=_path(root, data["kernel"]["source_dir"]),
                work_dir=_path(root, data["kernel"]["work_dir"]),
                check_dir=_path(root, data["kernel"]["check_dir"]),
                patches_dir=_path(root, data["kernel"]["patches_dir"]),
                defconfig=data["kernel"]["defconfig"],
                arch=data["kernel"]["arch"],
                cross_compile=data["kernel"].get("cross_compile", ""),
                image_path=data["kernel"]["image_path"],
                kernel_image_name=data["kernel"]["kernel_image_name"],
                dtb_glob=data["kernel"]["dtb_glob"],
                dtbo_glob=data["kernel"]["dtbo_glob"],
                overlays_readme=data["kernel"]["overlays_readme"],
            )
            firmware = FirmwareConfig(
                files_dir=_path(root, data["firmware"]["files_dir"]),
                work_dir=_path(root, data["firmware"]["work_dir"]),
            )
            rootfs = RootfsConfig(
                stage3=data["rootfs"].get("stage3", ""),
                files_dir=_path(root, data["rootfs"]["files_dir"]),
                work_dir=_path(root, data["rootfs"]["work_dir"]),
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
            image=image,
            verbose=verbose,
        )


def _path(root: Path, value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return root / path

