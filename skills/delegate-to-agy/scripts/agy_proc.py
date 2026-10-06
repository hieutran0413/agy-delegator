"""Process identity and bounded termination for AGY worker process groups.

A bare PID is not a durable identity: macOS reuses PIDs.  A job record stores
the worker PID, its ``ps`` start time token, and the per-job agent name; a
process is treated as this job's worker only when all of them match.  Each
worker runs in its own session, so its PID is also its process-group id.
POSIX does not reuse a PID while a process group with that id still exists,
which makes signalling the group safe once the leader identity was verified.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

WORKER_EXECUTABLES = {"agy"}
NO_IDENTITY = "unavailable"  # recorded when the start token could not be read; never matches
_LSTART = re.compile(r"^\s*([A-Z][a-z]{2}\s+[A-Z][a-z]{2}\s+\d+\s+\d{2}:\d{2}:\d{2}\s+\d{4})\s+(.+?)\s*$")


def process_identity(pid: int | None) -> tuple[str, str] | None:
    """Return ``(start token, command line)`` for a live PID, else ``None``."""
    if not isinstance(pid, int) or pid <= 1:
        return None
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "lstart=", "-o", "command="],
        text=True, capture_output=True, check=False,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )
    match = _LSTART.match(result.stdout)
    if not match:
        return None
    return match.group(1), match.group(2)


def is_worker_command(command: str, agent: str | None) -> bool:
    """Accept ``agy-cli chat ...`` or ``agy-cli-chat ...`` launched for ``agent``."""
    words = command.split()
    if not words:
        return False
    name = Path(words[0]).name
    if name not in WORKER_EXECUTABLES:
        return False
    if agent and "--agent" in words:
        position = words.index("--agent")  # the real flag precedes the free-text prompt
        if words[position + 1:position + 2] != [agent]:
            return False
    return True


def worker_alive(metadata: dict) -> bool:
    """True while the recorded PID is still this job's AGY chat process.

    Legacy records without a start token are reported as live when a AGY chat
    process holds the PID, but ``owns_worker`` never lets them be signalled.
    """
    identity = process_identity(metadata.get("pid"))
    if not identity:
        return False
    started, command = identity
    if not is_worker_command(command, metadata.get("agent")):
        return False
    expected = metadata.get("processStarted")
    return not expected or expected == started


def owns_worker(metadata: dict) -> bool:
    """Strict identity used before sending any signal: the start token is mandatory."""
    expected = metadata.get("processStarted")
    if not expected or expected == NO_IDENTITY:
        return False
    identity = process_identity(metadata.get("pid"))
    return bool(identity) and identity[0] == expected and is_worker_command(identity[1], metadata.get("agent"))


def supervisor_alive(metadata: dict, job_id: str) -> bool:
    identity = process_identity(metadata.get("supervisorPid"))
    if not identity:
        return False
    started, command = identity
    words = command.split()
    expected = metadata.get("supervisorStarted")
    if expected and expected != started:
        return False
    return "supervise" in words and job_id in words


def group_alive(pgid: int | None) -> bool:
    if not isinstance(pgid, int) or pgid <= 1:
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, but is not ours; callers must not claim it exited
    return True


def terminate_group(
    pgid: int,
    term_grace: float = 5.0,
    kill_grace: float = 3.0,
    reap: Callable[[], object] | None = None,
    poll: float = 0.1,
) -> dict:
    """Escalate SIGTERM -> SIGKILL on one already-verified process group.

    ``reap`` lets a parent collect its zombie leader so the group can vanish.
    Returns the signals sent and whether the group's exit was confirmed.
    """
    sent: list[str] = []
    if not isinstance(pgid, int) or pgid <= 1:
        return {"signals": sent, "exited": False, "error": "Invalid process group"}
    for number, grace in ((signal.SIGTERM, term_grace), (signal.SIGKILL, kill_grace)):
        if reap:
            reap()
        if not group_alive(pgid):
            return {"signals": sent, "exited": True}
        try:
            os.killpg(pgid, number)
        except ProcessLookupError:
            return {"signals": sent, "exited": True}
        except PermissionError:
            return {"signals": sent, "exited": False, "error": "Permission denied signalling process group"}
        sent.append(number.name)
        deadline = time.monotonic() + max(0.0, grace)
        while time.monotonic() < deadline:
            if reap:
                reap()
            if not group_alive(pgid):
                return {"signals": sent, "exited": True}
            time.sleep(poll)
    if reap:
        reap()
    exited = not group_alive(pgid)
    result = {"signals": sent, "exited": exited}
    if not exited:
        result["error"] = "Process group still present after SIGKILL"
    return result
