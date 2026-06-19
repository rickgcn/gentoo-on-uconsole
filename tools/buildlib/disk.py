from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import secrets
import tomllib
from uuid import uuid4

from .config import BuildConfig
from .errors import BuildError
from .files import ensure_parent


@dataclass(frozen=True)
class DiskIdentity:
    partition_table: str
    disk_guid: str
    boot_partition_guid: str
    root_partition_guid: str
    boot_filesystem_id: str
    root_filesystem_uuid: str
    boot_label: str
    root_label: str

    @property
    def boot_partuuid(self) -> str:
        return self.boot_partition_guid

    @property
    def root_partuuid(self) -> str:
        return self.root_partition_guid


def load_or_create_disk_identity(config: BuildConfig) -> DiskIdentity:
    path = config.disk_identity_path
    if path.exists():
        return _load_disk_identity(config, path)

    identity = DiskIdentity(
        partition_table=config.disk.partition_table,
        disk_guid=str(uuid4()),
        boot_partition_guid=str(uuid4()),
        root_partition_guid=str(uuid4()),
        boot_filesystem_id=secrets.token_hex(4).upper(),
        root_filesystem_uuid=str(uuid4()),
        boot_label=config.disk.boot_label,
        root_label=config.disk.root_label,
    )
    _write_disk_identity(path, identity)
    return identity


def _load_disk_identity(config: BuildConfig, path: Path) -> DiskIdentity:
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    try:
        identity = DiskIdentity(
            partition_table=data["partition_table"],
            disk_guid=data["disk_guid"],
            boot_partition_guid=data["boot_partition_guid"],
            root_partition_guid=data["root_partition_guid"],
            boot_filesystem_id=data["boot_filesystem_id"],
            root_filesystem_uuid=data["root_filesystem_uuid"],
            boot_label=data["boot_label"],
            root_label=data["root_label"],
        )
    except KeyError as exc:
        raise BuildError(f"disk identity is missing required key: {exc}") from exc

    if identity.partition_table != config.disk.partition_table:
        raise BuildError(f"disk identity partition table does not match configuration: {path}")
    if identity.boot_label != config.disk.boot_label:
        raise BuildError(f"disk identity boot label does not match configuration: {path}")
    if identity.root_label != config.disk.root_label:
        raise BuildError(f"disk identity root label does not match configuration: {path}")
    return identity


def _write_disk_identity(path: Path, identity: DiskIdentity) -> None:
    ensure_parent(path)
    path.write_text(
        "\n".join(
            [
                f'partition_table = "{identity.partition_table}"',
                f'disk_guid = "{identity.disk_guid}"',
                f'boot_partition_guid = "{identity.boot_partition_guid}"',
                f'root_partition_guid = "{identity.root_partition_guid}"',
                f'boot_filesystem_id = "{identity.boot_filesystem_id}"',
                f'root_filesystem_uuid = "{identity.root_filesystem_uuid}"',
                f'boot_label = "{identity.boot_label}"',
                f'root_label = "{identity.root_label}"',
                "",
            ]
        ),
        encoding="utf-8",
    )
