# SPDX-License-Identifier: MIT
"""What the executor tells the worker about its sandbox, and the files it sets up there.

The isolation probe (D43) reports facts about the machine and this process, read from
files only, and whether sentinel paths can be seen; ``configure`` writes its files,
keeping any it did not write or that changed since.

The executor (executor.py) loads this file beside itself in a sandbox, so it depends
only on the standard library.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import socket
import tempfile
from pathlib import Path
from typing import Any, cast

Message = dict[str, Any]

PROBE_TEXT = 256
"""The longest value the isolation probe reports."""
CONFIG_RECORD = "configured.json"
"""In the executor's own directory: what ``configure`` wrote, by path and SHA-256."""


def probe_facts() -> Message:
    """Facts about the machine and this process, for the worker's isolation check (D43).

    The worker reads the same facts on its own host and compares them. Only files
    are read, no tools run; a value that cannot be read is ``None``.
    """
    status = _process_status()
    return {
        "kernel_release": _clip(platform.release()),
        "boot_id": _first_line("/proc/sys/kernel/random/boot_id"),
        "hostname": _clip(socket.gethostname()),
        "machine_id": _first_line("/etc/machine-id"),
        "seccomp": {"0": "disabled", "1": "strict", "2": "filter"}.get(status.get("Seccomp", "")),
        "no_new_privs": {"0": False, "1": True}.get(status.get("NoNewPrivs", "")),
        "capabilities": _clip(status.get("CapEff", "")),
        "uid": os.getuid(),
        "hypervisor": _cpu_flag("hypervisor"),
        "sys_vendor": _first_line("/sys/class/dmi/id/sys_vendor"),
        "product_name": _first_line("/sys/class/dmi/id/product_name"),
    }


def sentinel_state(path: str) -> str:
    """``present`` or ``absent`` for ``path`` here, or ``unknown`` when that cannot be told.

    The file need not be readable: seeing it at all means sharing the worker's files.
    """
    if not os.path.isabs(path):
        return "unknown"
    try:
        os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return "absent"
    except OSError:  # such as a directory on the way that this user cannot search
        return "unknown"
    return "present"


def _clip(text: str) -> str | None:
    return text.strip()[:PROBE_TEXT] or None


def _first_line(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read(PROBE_TEXT)
    except OSError:
        return None
    return _clip(data.decode("utf-8", "replace").split("\n", 1)[0])


def _process_status() -> dict[str, str]:
    """``/proc/self/status`` as name to value."""
    try:
        with open("/proc/self/status", encoding="utf-8", errors="replace") as handle:
            lines = handle.read(65536).splitlines()
    except OSError:
        return {}
    return {name: value.strip() for name, _, value in (line.partition(":") for line in lines)}


def _cpu_flag(flag: str) -> bool | None:
    """Whether the first CPU's flags include ``flag``; ``None`` if no flags are listed."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                name, _, value = line.partition(":")
                if name.strip() == "flags":
                    return flag in value.split()
    except OSError:
        return None
    return None


def write_config_files(
    state: Path, files: list[tuple[Path, str, str | None]]
) -> tuple[list[str], list[Message], dict[str, str]]:
    """Write ``configure``'s files: (paths written, files kept with the reason, variables).

    ``state`` holds the record of what was written, by path and SHA-256, so a
    file changed since is recognized and kept. Each variable names its file,
    once the file is there.
    """
    record_path = state / CONFIG_RECORD
    try:
        loaded: object = json.loads(record_path.read_text())
    except (OSError, ValueError):
        loaded = {}
    record = cast(dict[str, object], loaded) if isinstance(loaded, dict) else {}
    written: list[str] = []
    skipped: list[Message] = []
    variables: dict[str, str] = {}
    for target, content, variable in files:
        data = content.encode()
        digest = hashlib.sha256(data).hexdigest()
        try:
            changed, reason = _write_config_file(target, data, digest, record.get(str(target)))
        except OSError as exc:
            changed, reason = False, f"cannot write it: {exc}"
        if reason is not None:
            skipped.append({"path": str(target), "reason": reason})
        else:
            record[str(target)] = digest
            if changed:
                written.append(str(target))
        if variable is not None and target.is_file():
            variables[variable] = str(target)
    try:
        _replace_file(record_path, json.dumps(record, indent=1).encode())
    except OSError as exc:
        skipped.append({"path": str(record_path), "reason": f"cannot write it: {exc}"})
    return written, skipped, variables


def _write_config_file(
    target: Path, data: bytes, digest: str, recorded: object
) -> tuple[bool, str | None]:
    """Write ``data`` unless another file is there: (whether it wrote, why it kept the file)."""
    if target.is_symlink() or (target.exists() and not target.is_file()):
        return False, "it is not a regular file"
    if target.exists():
        current = hashlib.sha256(target.read_bytes()).hexdigest()
        if current == digest:
            return False, None
        if current != recorded:
            if recorded is None:
                return False, "the executor did not write it"
            return False, "it changed after the executor wrote it"
    _replace_file(target, data)
    return True, None


def _replace_file(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=".configure-")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
