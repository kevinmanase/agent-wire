# SPDX-License-Identifier: AGPL-3.0-only
import json
import os
import stat
from pathlib import Path

from .errors import WireError

MAX_FRAME = 1_048_576


def default_state() -> Path:
    base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state"))
    return base / "agent-wire"


def private_directory(path: Path) -> Path:
    path = path.absolute()
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise WireError("unsafe_path", f"Expected a private directory owned by you: {path}")
    return path


def check_socket(path: str) -> tuple[int, int]:
    p = Path(path)
    if not p.is_absolute():
        raise WireError("unsafe_path", "Socket paths must be absolute")
    info = p.lstat()
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
        raise WireError("unsafe_path", "Expected a local socket owned by the current user")
    return info.st_dev, info.st_ino


def write_identity(state: Path, identity: dict) -> Path:
    directory = private_directory(state / "identities")
    path = directory / f"{identity['agent']['id']}.json"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(identity, stream)
        stream.write("\n")
    return path


def read_identity_record(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise WireError("unsafe_identity", "Identity file must be private and owned by you")
        data = json.load(stream)
    if not isinstance(data, dict) or not isinstance(data.get("session_handle"), str):
        raise WireError("unsafe_identity", "Invalid identity file")
    return data


def read_identity(path: Path) -> str:
    return read_identity_record(path)["session_handle"]
