"""Durable job-record primitives shared by the CLI, supervisor, MCP adapter and dashboard.

* ``write_json`` is atomic and safe for concurrent writers: each writer uses its
  own unique temporary file in the destination directory, then ``os.replace``.
* ``locked`` serialises read-modify-write cycles with an advisory ``flock``.
  ``flock`` belongs to an open file description, so never nest ``locked`` on
  the same lock file within one process.
* ``seal_events`` terminates a trailing partial JSONL line once its writer has
  exited, so the complete-line reader can deliver it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Iterator


def write_json(path: Path, value: object) -> None:
    """Atomically replace ``path``; concurrent writers never share a temp file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


@contextlib.contextmanager
def locked(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive lock; raises ``FileNotFoundError`` if the directory is gone."""
    with open(lock_path, "a+") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def seal_events(path: Path) -> None:
    """Append a newline to a trailing partial line. Call only after the writer exited."""
    try:
        with path.open("rb+") as stream:
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                return
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                stream.write(b"\n")
    except FileNotFoundError:
        return
