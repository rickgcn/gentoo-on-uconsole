from __future__ import annotations

import re
from pathlib import Path

from .errors import BuildError
from .manifest import sha256_file


PATCH_RE = re.compile(r"^(?P<number>\d{4})-.+\.patch$")


def discover_patches(patches_dir: Path) -> list[Path]:
    if not patches_dir.exists():
        raise BuildError(f"kernel patch directory does not exist: {patches_dir}")

    patch_files = sorted(patches_dir.glob("*.patch"))
    if not patch_files:
        raise BuildError(f"no kernel patches found in {patches_dir}")

    numbers: list[int] = []
    seen: set[int] = set()
    invalid: list[str] = []
    for patch in patch_files:
        match = PATCH_RE.match(patch.name)
        if not match:
            invalid.append(patch.name)
            continue
        number = int(match.group("number"))
        if number in seen:
            raise BuildError(f"duplicate kernel patch number: {number:04d}")
        seen.add(number)
        numbers.append(number)

    if invalid:
        names = ", ".join(invalid)
        raise BuildError(f"invalid kernel patch names: {names}")

    expected = list(range(1, len(patch_files) + 1))
    if numbers != expected:
        expected_text = ", ".join(f"{number:04d}" for number in expected)
        actual_text = ", ".join(f"{number:04d}" for number in numbers)
        raise BuildError(f"kernel patch numbers must be contiguous; expected {expected_text}, got {actual_text}")

    return patch_files


def print_patch_queue(patches: list[Path]) -> None:
    print("kernel patch queue:", flush=True)
    for patch in patches:
        subject = _patch_subject(patch)
        digest = sha256_file(patch)[:12]
        suffix = f" - {subject}" if subject else ""
        print(f"  {patch.name} [{digest}]{suffix}", flush=True)


def _patch_subject(path: Path) -> str:
    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("Subject:"):
                return line.removeprefix("Subject:").strip()
    return ""
