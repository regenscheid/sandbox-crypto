# SPDX-License-Identifier: MIT
"""What the launcher keeps in its state directory, all of it private to its user.

```
<state_dir>/                  mode 700
    launcher.lock             held while ``up`` runs
    launcher.json             the instance running: its process and its parts'
    workspace-login           the workspace's login address, mode 600
    tokens/<name>.token       each token, mode 600: made once, kept, never printed
    worker/worker.toml        the worker's settings, written at each ``up``
    worker/state/             the worker's state directory, with its lock
    control/                  the control store the run service and workspace share
    logs/<part>.log           what each part writes
```

Delete ``tokens/`` with the launcher stopped to have new tokens made; never delete the
worker's state directory while its sandboxes run (docs/WORKER.md).
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import secrets
import stat
from pathlib import Path

from .config import LauncherError

MIN_TOKEN = 32


class Layout:
    """The paths under one state directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lock = root / "launcher.lock"
        self.record = root / "launcher.json"
        self.login = root / "workspace-login"
        self.tokens = root / "tokens"
        self.worker = root / "worker"
        self.worker_settings = self.worker / "worker.toml"
        self.worker_state = self.worker / "state"
        self.control = root / "control"
        self.logs = root / "logs"

    def token(self, name: str) -> Path:
        return self.tokens / f"{name}.token"

    def log(self, part: str) -> Path:
        return self.logs / f"{part}.log"

    def make(self) -> None:
        """Make the directories, each mode 700."""
        for directory in (self.root, self.tokens, self.worker, self.control, self.logs):
            private_dir(directory)


def private_dir(path: Path) -> Path:
    """``path`` as a directory of this user's that only they can open.

    One the launcher makes is mode 700. One that is there already must be this user's
    and mode 700: the launcher refuses any other rather than change it, so that a
    ``state_dir`` of ``~`` never changes the home directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        made = False
    else:
        made = True
    found = path.lstat()
    if not stat.S_ISDIR(found.st_mode):
        raise LauncherError(f"{path} is not a directory")
    if found.st_uid != os.getuid():
        raise LauncherError(f"{path} is not yours: choose another state_dir")
    if made:
        path.chmod(0o700)  # whatever the umask took away or left
    elif stat.S_IMODE(found.st_mode) != 0o700:
        raise LauncherError(
            f"{path} is open to other users (mode {stat.S_IMODE(found.st_mode):o}): make it "
            f"mode 700 (chmod 700 {path}), or choose another state_dir"
        )
    return path


def check_private(path: Path) -> None:
    """Refuse ``path`` if it is there but not a directory of this user's of mode 700, as
    ``private_dir`` does, before anything in it is read."""
    try:
        found = path.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(found.st_mode):
        raise LauncherError(f"{path} is not a directory")
    if found.st_uid != os.getuid():
        raise LauncherError(f"{path} is not yours: choose another state_dir")
    if stat.S_IMODE(found.st_mode) != 0o700:
        raise LauncherError(
            f"{path} is open to other users (mode {stat.S_IMODE(found.st_mode):o}): make it "
            f"mode 700 (chmod 700 {path}), or choose another state_dir"
        )


def ensure_token(path: Path) -> str:
    """The SHA-256 of the token in ``path``, which is made first if it is missing.

    A new token is 32 random bytes, URL-safe, in a file made with mode 600. The token
    itself never leaves its file: the parts that need it read the file, and the
    settings that check it hold its digest.
    """
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
    if path.is_symlink() or not path.is_file():
        raise LauncherError(f"{path} is not a token file")
    if path.stat().st_mode & 0o077:
        raise LauncherError(f"{path} is open to other users: chmod 600 {path}")
    token = path.read_text(encoding="utf-8").strip()
    if len(token) < MIN_TOKEN:
        raise LauncherError(f"{path} holds no token: delete it, and the next up makes one")
    return hashlib.sha256(token.encode()).hexdigest()


def write_private(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step, as a file of mode 600.

    The text goes to a temporary file of a name no other writer uses, which then takes
    ``path``'s place, so two processes writing the same file never share one."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        temporary.replace(path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise
