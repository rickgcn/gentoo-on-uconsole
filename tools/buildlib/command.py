from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Iterable

from .errors import BuildError


def format_command(args: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(arg)) for arg in args)


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    capture: bool = False,
    verbose: bool = False,
) -> subprocess.CompletedProcess[str]:
    if verbose:
        prefix = f"cd {cwd} && " if cwd else ""
        print(f"+ {prefix}{format_command(args)}")

    process_env = os.environ.copy()
    if env:
        process_env.update(env)

    result = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        env=process_env,
        input=input_text,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        check=False,
    )
    if result.returncode != 0:
        message = f"command failed ({result.returncode}): {format_command(args)}"
        if capture:
            stderr = (result.stderr or "").strip()
            stdout = (result.stdout or "").strip()
            if stderr:
                message += f"\n{stderr}"
            elif stdout:
                message += f"\n{stdout}"
        raise BuildError(message)
    return result

