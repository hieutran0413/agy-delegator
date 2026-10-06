#!/usr/bin/env python3
"""Start AGY headlessly and expose its JSONL activity on a local dashboard."""

from __future__ import annotations

import argparse
import calendar
import contextlib
import fcntl
import hashlib
import json
import mimetypes
import os
import re
import secrets
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable, Iterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
import agy_agent as agent  # noqa: E402
import agy_proc as proc  # noqa: E402
import agy_report as report  # noqa: E402
import agy_stream as adapter
import agy_store as store  # noqa: E402

ROOT = Path(os.environ.get("AGY_DELEGATOR_HOME", Path.home() / ".agy-delegator")) / "live"
PORT = 8775
DEFAULT_EVENT_PAGE = 30
# Live event logs may be compacted (removed) this long after a durable
# result.json exists, but only once the AGY session id and the full assistant
# report are durable. Job records, result.json and report.md are never removed
# automatically: they are the lineage needed to continue a AGY session.
RETENTION_SECONDS = int(os.environ.get("AGY_LIVE_RETENTION_SECONDS", "900"))
# A job record exists briefly with ``pid=None`` between creation and Popen.
# Within this window it is reported as "starting" rather than finalized failed.
STARTUP_GRACE_SECONDS = int(os.environ.get("AGY_LIVE_STARTUP_GRACE_SECONDS", "60"))
DEFAULT_MAX_RUNTIME = int(os.environ.get("AGY_LIVE_MAX_RUNTIME_SECONDS", "3600"))
DEFAULT_IDLE_TIMEOUT = int(os.environ.get("AGY_LIVE_IDLE_TIMEOUT_SECONDS", "900"))
MAX_LIMIT_SECONDS = 24 * 3600
EXIT_GRACE_SECONDS = 30.0  # a worker must exit this soon after its completion event
TERM_GRACE_SECONDS = 5.0
KILL_GRACE_SECONDS = 3.0
TERMINAL_STATES = {"finished", "failed", "cancelled", "timed_out", "stalled"}
FORCED_STATES = {"cancelled", "timed_out", "stalled"}
DASHBOARD_VERSION = 9
# Dashboard mutations (stop / continue). Same-origin only; bounded bodies.
MAX_MUTATION_BODY = 20000
MAX_STEER_MESSAGE = 8000
# Codex continuations may carry command output as execution evidence.
MAX_CONTINUE_MESSAGE = 100000
HISTORY_PAGE = 20
MAX_HISTORY_PAGE = 50
REQUEST_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,80}")
# AGY session ids are passed as an argv value; never allow a leading "-".
SESSION_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
# Only these launch-time extras may be attached to a job record by continuation.
STEER_METADATA_KEYS = ("parentJobId", "steerRequestId", "originalBrief", "userMessages",
                       "rootTaskId", "turn", "turnMessage")
CODE_ONLY_POLICY = """Execution boundary (mandatory):
- You are a code-only AGY worker. You may inspect files and, in IMPLEMENT mode, edit source files.
- Do not invoke shell/terminal commands, builds, tests, formatters, Git, simulators, servers, browsers, package managers, or process-control tools.
- Do not directly start, poll, wait for, diagnose, interrupt, or kill external processes. Exception: use the three configured shared-executor MCP operations for registered checks.
- For configured checks, call shared-executor MCP start_action, then action_status until done; never call terminal tools directly. If no configured action fits, request the smallest command from the coordinating app.
- Report changed files and suggest verification commands, but never run those commands yourself.

"""


write_json = store.write_json  # unique temp file per writer + os.replace
read_json = store.read_json


def job_dir(job_id: str) -> Path:
    if not job_id or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in job_id):
        raise ValueError("Invalid job id")
    return ROOT / "jobs" / job_id


@contextlib.contextmanager
def _directory_lock(directory: Path) -> Iterator[None]:
    """Exclusive per-job lock; raises ``FileNotFoundError`` if the job is gone."""
    with (directory / "job.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def update_job(job_id: str, mutate: Callable[[dict], object]) -> dict:
    """Read-modify-write ``job.json`` under the job lock.

    ``mutate`` edits the record in place; returning ``False`` skips the write.
    A deleted job is never recreated.
    """
    directory = job_dir(job_id)
    with _directory_lock(directory):
        path = directory / "job.json"
        metadata = read_json(path)
        if not metadata:
            raise FileNotFoundError(job_id)
        if mutate(metadata) is not False:
            write_json(path, metadata)
        return metadata


def _created_at(metadata: dict) -> float | None:
    created = metadata.get("createdAt")
    if isinstance(created, (int, float)):
        return float(created)
    started = metadata.get("startedAt")
    if isinstance(started, str):
        try:
            return float(calendar.timegm(time.strptime(started, "%Y-%m-%dT%H:%M:%SZ")))
        except ValueError:
            return None
    return None


def in_startup_grace(metadata: dict, now: float | None = None) -> bool:
    """True while a just-created job has not yet recorded its worker PID."""
    if metadata.get("pid") or metadata.get("state") != "starting":
        return False
    created = _created_at(metadata)
    if created is None:
        return False
    return (time.time() if now is None else now) - created < STARTUP_GRACE_SECONDS


process_identity = proc.process_identity


def is_agy_worker_running(metadata: dict) -> bool:
    """True while the recorded PID is still this job's ``agy-cli``/``agy-cli-chat`` process."""
    return proc.worker_alive(metadata)


def is_supervisor_running(metadata: dict, job_id: str) -> bool:
    return proc.supervisor_alive(metadata, job_id)


def _scan(path: Path, offset: int = 0, limit: int | None = None,
          end: int | None = None) -> tuple[list[dict], int, int]:
    """Scan newline-complete JSONL lines.

    Returns ``(events, count, cursor)``: ``count`` is the number of complete
    lines; ``cursor`` is the line offset just past the last line consumed in
    ``[offset, end)`` (blank lines are consumed but not emitted). A trailing
    line without ``\\n`` is still being written and is neither counted nor
    consumed.
    """
    output: list[dict] = []
    count = 0
    cursor = offset
    if not path.exists():
        return output, count, cursor
    with path.open("rb") as stream:
        for index, line in enumerate(stream):
            if not line.endswith(b"\n"):
                break
            count = index + 1
            if index < offset or (end is not None and index >= end):
                continue
            if limit is not None and len(output) >= limit:
                continue
            cursor = index + 1
            raw = line.decode("utf-8", errors="replace").rstrip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                payload = {"type": "output", "text": raw}
            output.append({"index": index, "event": adapter.normalize(payload)})
    return output, count, cursor


def events(path: Path, offset: int = 0, limit: int | None = 250,
           end: int | None = None) -> tuple[list[dict], int]:
    output, count, _ = _scan(path, offset, limit, end)
    return output, count


def events_after(path: Path, after: int, limit: int = 200) -> tuple[list[dict], int]:
    """Return events from ``after`` and the coherent next cursor."""
    output, _, cursor = _scan(path, max(0, after), limit)
    return output, cursor


def event_page(path: Path, before: int | None, limit: int) -> dict:
    """One bounded page ending strictly before ``before`` (or at the tail)."""
    _, count = events(path, limit=0)
    end = count if before is None else min(max(0, before), count)
    start = max(0, end - max(1, min(limit, 200)))
    output, _ = events(path, offset=start, limit=None, end=end)
    return {"events": output, "oldestOffset": start, "hasOlder": start > 0, "nextOffset": count}


def event_window(path: Path, before: int | None, limit: int) -> tuple[list[dict], int, bool]:
    """Return one bounded event page ending before the requested offset."""
    page = event_page(path, before, limit)
    return page["events"], page["oldestOffset"], page["hasOlder"]


# --- AGY session identity -----------------------------------------------------

def valid_session_id(value: object) -> bool:
    return isinstance(value, str) and bool(SESSION_ID_PATTERN.fullmatch(value))


def event_session_id(event: object) -> str | None:
    """The session id an event envelope declares about itself.

    Only envelope positions count: top-level ``sessionId``, ``data``/``params``/
    ``result`` ``sessionId``, and the ACP ``update`` object's own ``sessionId``.
    Tool inputs/outputs (``rawInput``, ``rawOutput``, ``content``…) are never
    inspected, so a file or command that mentions a session id cannot spoof it.
    """
    if not isinstance(event, dict):
        return None
    candidates: list[object] = [event.get("sessionId"), event.get("session_id"), event.get("conversation_id")]
    containers = [event]
    for key in ("data", "params", "result"):
        value = event.get(key)
        if isinstance(value, dict):
            containers.append(value)
            candidates += [value.get("sessionId"), value.get("session_id")]
    for container in containers:
        update = container.get("update")
        if isinstance(update, dict):
            candidates += [update.get("sessionId"), update.get("session_id")]
    for candidate in candidates:
        if valid_session_id(candidate):
            return candidate  # type: ignore[return-value]
    return None


def session_id_from_events(activity: list[dict]) -> tuple[str | None, bool]:
    """Return ``(sessionId, conflict)`` from one job's own events.

    Several distinct ids in one job's log are ambiguous: ``(None, True)``.
    """
    seen: list[str] = []
    for item in activity:
        found = event_session_id(item.get("event"))
        if found and found not in seen:
            seen.append(found)
    if len(seen) > 1:
        return None, True
    return (seen[0] if seen else None), False


def _final_assistant_message(activity: list[dict]) -> str:
    """Assistant text chunks since the last tool activity. Thought chunks are never included."""
    tail: list[str] = []
    for item in activity:
        event = item.get("event")
        if not isinstance(event, dict):
            continue
        update = report._update(event)
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content") or {}
            if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
                tail.append(content["text"])
        elif kind in ("tool_call", "tool_call_update"):
            tail = []
    return "".join(tail)


def durable_report(activity: list[dict], summary: dict) -> tuple[str | None, bool]:
    """Full final assistant report for the durable result; never truncated by us.

    AGY may cap ``runFinished.finalText`` (``finalTextTruncated``). When the
    streamed final assistant message is longer, it is the complete raw text the
    model wrote and replaces the capped copy.
    """
    text, truncated = summary.get("report"), bool(summary.get("reportTruncated"))
    if truncated:
        streamed = _final_assistant_message(activity)
        if streamed.strip() and len(streamed) > len(text or ""):
            return streamed, False
    return text, truncated


def _write_report_file(directory: Path, text: str | None) -> str | None:
    """Atomically persist the raw report as ``report.md``; returns its path."""
    if not text:
        return None
    path = directory / "report.md"
    descriptor, temporary = tempfile.mkstemp(prefix=".report.md.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temporary)
        raise
    return str(path)


def _terminal_state(metadata: dict, summary: dict) -> tuple[str, str | None]:
    """Final state for a job whose worker is gone. Never success unless AGY said so."""
    outcome = summary["outcome"]
    if outcome == "finished":
        return "finished", None
    forced = metadata.get("forcedState")
    if forced in FORCED_STATES or metadata.get("cancelRequested"):
        state = forced if forced in FORCED_STATES else "cancelled"
        return state, metadata.get("forcedReason") or summary["error"] or "Cancelled at Codex request"
    if outcome:
        return outcome, summary["error"]
    if metadata.get("launchError"):
        return "failed", metadata["launchError"]
    if not metadata.get("pid") and metadata.get("state") == "starting":
        return "failed", "Worker failed to start"
    code = metadata.get("exitCode")
    return "failed", "Worker exited without a completion event" + (" (exit code " + str(code) + ")" if code is not None else "")


def finalize_job(job_id: str, check_alive: bool = True, **updates: object) -> dict | None:
    """Write the durable ``result.json`` once. Returns ``None`` if the job is still live.

    The supervisor calls this with ``check_alive=False`` after reaping the
    worker; every other caller re-checks liveness under the job lock so a
    starting or running job is never finalized early.
    """
    directory = job_dir(job_id)
    with _directory_lock(directory):
        existing = read_json(directory / "result.json")
        if existing:
            return existing
        metadata = read_json(directory / "job.json")
        if not metadata:
            raise FileNotFoundError(job_id)
        if check_alive and (is_agy_worker_running(metadata) or is_supervisor_running(metadata, job_id)
                            or in_startup_grace(metadata)):
            return None
        metadata.update(updates)
        event_path = directory / "events.jsonl"
        if not check_alive:
            # Only the supervisor, which reaped the worker, knows for certain the
            # writer is gone; then a trailing partial line is complete data.
            # Elsewhere identity is ps-based, so never mutate a possibly live log.
            store.seal_events(event_path)
        activity, count = events(event_path, limit=None)
        summary = report.summarize(activity)
        state, error = _terminal_state(metadata, summary)
        report_text, report_truncated = durable_report(activity, summary)
        observed, conflict = session_id_from_events(activity)
        resumed = metadata.get("resumeSessionId")
        # The authoritative id is the one this job's own events declare. A resumed
        # turn that reports a different session must never pass as continuous.
        mismatch = bool(resumed and observed and observed != resumed)
        session_id = None if (conflict or mismatch) else (observed or (resumed if valid_session_id(resumed) else None))
        if mismatch and state == "finished":
            state = "failed"
        if mismatch:
            error = ("AGY reported session " + str(observed) + " instead of resumed session " + str(resumed)
                     + "; continuity is not guaranteed" + ("; " + error if error else ""))
        # report.md is written before result.json, so a durable result always has its report.
        saved_report = _write_report_file(directory, report_text)
        result = {
            "state": state, "error": error, "errorDetails": summary["errorDetails"],
            "completion": summary["completion"], "finishStatus": summary["status"],
            "stopReason": summary["stopReason"], "report": report_text, "reportPath": saved_report,
            "finalTextTruncated": report_truncated, "changedFiles": summary["changedFiles"],
            "exitCode": metadata.get("exitCode"), "termination": metadata.get("termination"),
            "eventCount": count, "finishedAt": time.time(),
            "sessionId": session_id, "observedSessionId": observed, "resumeSessionId": resumed,
            "sessionIdConflict": conflict, "sessionMismatch": mismatch,
            "rootTaskId": metadata.get("rootTaskId") or root_task_id(metadata), "turn": metadata.get("turn") or 1,
        }
        write_json(directory / "result.json", result)
        metadata["state"] = state
        metadata.setdefault("finishedAt", result["finishedAt"])
        metadata["sessionId"] = session_id
        metadata["sessionMismatch"] = mismatch
        metadata["sessionIdConflict"] = conflict
        write_json(directory / "job.json", metadata)
    agent.remove_agent(metadata.get("agentFile"), metadata.get("agent"))
    return result


def _live_state(metadata: dict, summary: dict) -> str:
    if summary["completion"]:
        return "completing"  # completion event seen; process exit not yet confirmed
    if metadata.get("cancelRequested"):
        return "cancelling"
    return "running" if metadata.get("pid") else "starting"


def _steer_in_flight(metadata: dict, now: float | None = None) -> bool:
    """A steer is stopping/relaunching this job (stale markers from a crash expire)."""
    pending = metadata.get("steerPending")
    if not isinstance(pending, dict) or metadata.get("supersededBy"):
        return False
    at = pending.get("at")
    # Stop (~13s) + launch (~45s worst case) comfortably fit in this window.
    return isinstance(at, (int, float)) and (time.time() if now is None else now) - at < 300


def migrate_legacy_job(job_id: str) -> dict:
    """Bring a terminal job recorded before session tracking up to date, once.

    Recovers the session id from the job's *own* events when they still exist
    (never from other jobs or "most recent session" guesses) and writes
    ``report.md`` from the durable result. The original record is kept as
    ``job.orig.json``; ``result.json`` is never rewritten.
    """
    directory = job_dir(job_id)
    with _directory_lock(directory):
        metadata = read_json(directory / "job.json")
        result = read_json(directory / "result.json")
        if not metadata or not result or metadata.get("sessionScanned") or "sessionId" in result:
            return metadata
        original = directory / "job.orig.json"
        if not original.exists():
            shutil.copy2(directory / "job.json", original)
        activity, _ = events(directory / "events.jsonl", limit=None)
        observed, conflict = session_id_from_events(activity)
        metadata["sessionScanned"] = True
        metadata["sessionIdConflict"] = conflict
        if observed and not metadata.get("sessionId"):
            metadata["sessionId"] = observed
            metadata["sessionIdSource"] = "recovered"
        if not (directory / "report.md").exists() and result.get("report"):
            metadata["reportPath"] = _write_report_file(directory, result["report"])
        write_json(directory / "job.json", metadata)
        return metadata


def report_path(directory: Path, metadata: dict, result: dict) -> str | None:
    for candidate in (result.get("reportPath"), metadata.get("reportPath")):
        if isinstance(candidate, str) and Path(candidate).is_file():
            return candidate
    fallback = directory / "report.md"
    return str(fallback) if fallback.is_file() else None


def root_task_id(metadata: dict) -> str | None:
    """Logical task id shared by every turn of one conversation.

    New records store ``rootTaskId``; older steered records are linked through
    ``parentJobId`` and resolve to the first job of their chain.
    """
    stored = metadata.get("rootTaskId")
    if isinstance(stored, str) and stored:
        return stored
    current, seen = metadata, set()
    while len(seen) < 200:
        parent = current.get("parentJobId")
        if not isinstance(parent, str) or not parent or parent in seen:
            break
        seen.add(parent)
        try:
            parent_record = read_json(job_dir(parent) / "job.json")
        except ValueError:
            return parent
        if not parent_record:
            return parent
        if isinstance(parent_record.get("rootTaskId"), str) and parent_record["rootTaskId"]:
            return parent_record["rootTaskId"]
        current = parent_record
    return current.get("id") or metadata.get("id")


def turn_number(metadata: dict) -> int:
    turn = metadata.get("turn")
    if isinstance(turn, int) and not isinstance(turn, bool) and turn >= 1:
        return turn
    depth, current, seen = 1, metadata, set()
    while depth < 200:
        parent = current.get("parentJobId")
        if not isinstance(parent, str) or parent in seen:
            break
        seen.add(parent)
        try:
            current = read_json(job_dir(parent) / "job.json")
        except ValueError:
            break
        depth += 1
        if not current:
            break
    return depth


def snapshot(job_id: str, include_prompt: bool = True) -> dict:
    directory = job_dir(job_id)
    metadata = read_json(directory / "job.json")
    if not metadata:
        raise FileNotFoundError(job_id)
    event_path = directory / "events.jsonl"
    result = read_json(directory / "result.json")
    worker_alive = is_agy_worker_running(metadata)
    supervisor_alive = is_supervisor_running(metadata, job_id)
    summary: dict = {}
    live_session: str | None = None
    if not result:
        activity, count = events(event_path, limit=None)
        summary = report.summarize(activity)
        live_session = session_id_from_events(activity)[0]
        if not (worker_alive or supervisor_alive or in_startup_grace(metadata)):
            with contextlib.suppress(FileNotFoundError):
                result = finalize_job(job_id) or {}
                metadata = read_json(directory / "job.json") or metadata
    if result and "sessionId" not in result and not metadata.get("sessionScanned"):
        with contextlib.suppress(FileNotFoundError, OSError):
            metadata = migrate_legacy_job(job_id) or metadata
    if result:
        state = result["state"]
        count = events(event_path, limit=0)[1] or int(result.get("eventCount") or 0)
        body = result
    else:
        state = "starting" if in_startup_grace(metadata) and not worker_alive else _live_state(metadata, summary)
        body = {
            "error": None, "errorDetails": None, "completion": summary["completion"],
            "finishStatus": summary["status"], "stopReason": summary["stopReason"],
            # A completion event already carries the report even before exit is confirmed.
            "report": summary["report"] if state == "completing" else None,
            "finalTextTruncated": summary["reportTruncated"] if state == "completing" else False,
            "changedFiles": summary["changedFiles"], "exitCode": None, "termination": metadata.get("termination"),
        }
    session_id = metadata.get("sessionId") if result else (live_session or metadata.get("resumeSessionId"))
    output = {
        "id": job_id, "state": state, "done": state in TERMINAL_STATES, "pid": metadata.get("pid"),
        "mode": metadata.get("mode"), "workspace": metadata.get("workspace"), "startedAt": metadata.get("startedAt"),
        "model": metadata.get("model"), "effort": metadata.get("effort"), "eventCount": count,
        "processAlive": worker_alive, "supervisorAlive": supervisor_alive,
        "cancelRequested": bool(metadata.get("cancelRequested")),
        "maxRuntimeSeconds": metadata.get("maxRuntimeSeconds"), "idleTimeoutSeconds": metadata.get("idleTimeoutSeconds"),
        "writeScope": metadata.get("writeScope"),
        "acknowledged": bool(metadata.get("acknowledgedAt")), "resultDurable": bool(result),
        # Conversation lineage: every turn of one logical task shares rootTaskId;
        # a continued turn points at its successor through ``supersededBy``.
        "rootTaskId": root_task_id(metadata), "turn": turn_number(metadata),
        "sessionId": session_id if valid_session_id(session_id) else None,
        "resumeSessionId": metadata.get("resumeSessionId"),
        "sessionMismatch": bool(metadata.get("sessionMismatch")),
        "reportPath": report_path(directory, metadata, result) if result else None,
        "parentJobId": metadata.get("parentJobId"), "supersededBy": metadata.get("supersededBy"),
        "steerPending": _steer_in_flight(metadata),
        "autoFallbackError": metadata.get("autoFallbackError"),
        "turnMessage": metadata.get("turnMessage"),
        "userMessages": [
            {"requestId": item.get("requestId"), "message": item.get("message"), "at": item.get("at")}
            for item in metadata.get("userMessages") or [] if isinstance(item, dict)
        ],
    }
    for key in ("report", "finalTextTruncated", "changedFiles", "error", "errorDetails", "completion",
                "finishStatus", "stopReason", "exitCode", "termination"):
        output[key] = body.get(key)
    if include_prompt:
        output["prompt"] = metadata.get("prompt", "")
        output["originalBrief"] = metadata.get("originalBrief")
    return output


def acknowledge_job(job_id: str) -> dict:
    """Mark a finished job's report as consumed. Never deletes the report, record or session."""
    status = snapshot(job_id, include_prompt=False)
    if status["state"] not in TERMINAL_STATES or not status["resultDurable"]:
        raise RuntimeError("Job " + job_id + " is " + status["state"] + "; nothing to acknowledge yet")

    def mark(current: dict) -> object:
        if current.get("acknowledgedAt"):
            return False
        current["acknowledgedAt"] = time.time()
        return True

    update_job(job_id, mark)
    status["acknowledged"] = True
    return status


def job_catalog() -> list[dict]:
    """Return lightweight job choices for the shared AGY Live dashboard."""
    root = ROOT / "jobs"
    if not root.exists():
        return []
    output: list[dict] = []
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        metadata = read_json(directory / "job.json")
        if not metadata:
            continue
        try:
            result = read_json(directory / "result.json")
            if result:
                # Historical terminal jobs need no process probes or log replay.
                status = {
                    "state": result["state"],
                    "rootTaskId": root_task_id(metadata),
                    "turn": turn_number(metadata),
                    "sessionId": result.get("sessionId") or metadata.get("sessionId"),
                }
            else:
                status = snapshot(directory.name, include_prompt=False)
        except (OSError, ValueError, KeyError):
            continue
        output.append({
            "id": metadata.get("id", directory.name),
            "state": status["state"],
            "mode": metadata.get("mode"),
            "workspace": metadata.get("workspace"),
            "startedAt": metadata.get("startedAt"),
            "parentJobId": metadata.get("parentJobId"),
            "supersededBy": metadata.get("supersededBy"),
            "rootTaskId": status.get("rootTaskId"),
            "turn": status.get("turn"),
            "sessionId": status.get("sessionId"),
        })
    return sorted(output, key=lambda item: item.get("startedAt") or "", reverse=True)


def _compactable(metadata: dict, result: dict, directory: Path) -> bool:
    """The live log may go only once the session id and the full report are durable."""
    if not valid_session_id(metadata.get("sessionId")):
        return False
    if result.get("report"):
        return report_path(directory, metadata, result) is not None
    return True


def cleanup_finished_jobs(now: float | None = None) -> None:
    """Apply retention without ever touching live jobs, reports or lineage.

    * No durable result yet: finalize only if the worker is truly gone.
    * ``job.json``, ``result.json`` and ``report.md`` are never removed: they
      are what a later ``continue_job`` resumes from. Acknowledging only marks
      a report as consumed.
    * The live log (``events.jsonl``) is compacted ``RETENTION_SECONDS`` after
      the result is written, and only when the AGY session id and the full
      assistant report are already durable. AGY's own sessions are never touched.
    """
    root = ROOT / "jobs"
    if not root.exists():
        return
    now = time.time() if now is None else now
    for directory in root.iterdir():
        if not directory.is_dir():
            continue
        try:
            if not read_json(directory / "result.json"):
                if read_json(directory / "job.json"):
                    snapshot(directory.name, include_prompt=False)  # finalizes dead jobs only
                continue
            metadata = read_json(directory / "job.json")
            if metadata and "sessionId" not in read_json(directory / "result.json") and not metadata.get("sessionScanned"):
                metadata = migrate_legacy_job(directory.name) or metadata  # before any compaction
            with _directory_lock(directory):
                metadata = read_json(directory / "job.json")
                result = read_json(directory / "result.json")
                if not result or is_agy_worker_running(metadata) or is_supervisor_running(metadata, directory.name):
                    continue
                finished_at = float(result.get("finishedAt") or 0)
                if now - finished_at >= RETENTION_SECONDS and _compactable(metadata, result, directory):
                    with contextlib.suppress(FileNotFoundError):
                        (directory / "events.jsonl").unlink()
            agent.remove_agent(metadata.get("agentFile"), metadata.get("agent"))
        except (OSError, TypeError, ValueError, KeyError):
            continue


def reply(handler: BaseHTTPRequestHandler, value: object, status: HTTPStatus = HTTPStatus.OK) -> None:
    body = json.dumps(value, ensure_ascii=False).encode("utf-8")
    handler.send_response(status.value)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    # No CORS headers: the dashboard is same-origin and mutations must stay so.
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/health":
            reply(self, {"ok": True, "version": DASHBOARD_VERSION})
            return
        if parsed.path == "/api/jobs":
            reply(self, {"jobs": job_catalog()})
            return
        if parsed.path in {"/", "/index.html"} or parsed.path.startswith("/assets/"):
            dist = Path(__file__).resolve().parents[3] / "dashboard" / "dist"
            relative = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path.lstrip("/")
            target = (dist / relative).resolve()
            if dist.resolve() not in target.parents or not target.is_file():
                reply(self, {"error": "Dashboard assets unavailable; build dashboard first"}, HTTPStatus.NOT_FOUND)
                return
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mimetypes.guess_type(str(target))[0] or "application/octet-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path.startswith("/api/jobs/"):
            bits = parsed.path.split("/")
            try:
                job_id = bits[3]
                if len(bits) == 4:
                    reply(self, snapshot(job_id))
                    return
                if len(bits) == 5 and bits[4] == "history":
                    query = parse_qs(parsed.query)
                    offset = max(0, int(query.get("offset", ["0"])[0]))
                    limit = max(1, min(MAX_HISTORY_PAGE, int(query.get("limit", [str(HISTORY_PAGE)])[0])))
                    reply(self, task_history(job_id, offset, limit))
                    return
                if len(bits) == 5 and bits[4] == "events":
                    query = parse_qs(parsed.query)
                    after_value = query.get("after", [None])[0]
                    if after_value is not None:
                        activity, next_offset = events_after(
                            job_dir(job_id) / "events.jsonl", max(0, int(after_value)), 200
                        )
                        reply(self, {"jobId": job_id, "events": activity, "nextOffset": next_offset})
                        return
                    before_value = query.get("before", [None])[0]
                    before = int(before_value) if before_value is not None else None
                    limit = int(query.get("limit", [str(DEFAULT_EVENT_PAGE)])[0])
                    page = event_page(job_dir(job_id) / "events.jsonl", before, limit)
                    reply(self, {"jobId": job_id, **page})
                    return
            except (FileNotFoundError, ValueError):
                reply(self, {"error": "Job not found"}, HTTPStatus.NOT_FOUND)
                return
            except Exception as error:
                reply(self, {"error": "Dashboard request failed: " + str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)
                return
        reply(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        """Codex-owned process control for the dashboard: stop one job or continue its AGY session."""
        bits = urlparse(self.path).path.split("/")
        if not (len(bits) == 5 and bits[1] == "api" and bits[2] == "jobs" and bits[4] in {"stop", "steer", "continue"}):
            self.close_connection = True
            reply(self, {"error": "Not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            body = mutation_body(self)
        except MutationRejected as rejected:
            self.close_connection = True  # the body may be unread
            reply(self, {"error": str(rejected)}, rejected.status)
            return
        job_id = bits[3]
        try:
            job_dir(job_id)
        except ValueError:
            reply(self, {"error": "Job not found"}, HTTPStatus.NOT_FOUND)
            return
        try:
            if bits[4] == "stop":
                if body != {}:
                    raise RequestError("Stop takes an empty JSON object")
                status = stop_job(job_id)
                reply(self, status, HTTPStatus.OK if status.get("done") else HTTPStatus.CONFLICT)
                return
            message, request_id = validate_steer_body(body)
            # "steer" is the legacy name: both interrupt a running turn and resume the same session.
            reply(self, steer_job(job_id, message, request_id, port=self.server.server_port))
        except FileNotFoundError:
            reply(self, {"error": "Job not found"}, HTTPStatus.NOT_FOUND)
        except ValueError as error:  # includes RequestError
            reply(self, {"error": str(error)}, HTTPStatus.BAD_REQUEST)
        except RuntimeError as error:  # includes ConflictError, PolicyError, stop refusals
            reply(self, {"error": str(error)}, HTTPStatus.CONFLICT)
        except Exception as error:
            reply(self, {"error": "Dashboard request failed: " + str(error)}, HTTPStatus.INTERNAL_SERVER_ERROR)


class MutationRejected(Exception):
    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status


class RequestError(ValueError):
    """The client request is malformed (HTTP 400)."""


class ConflictError(RuntimeError):
    """The request is valid but cannot be applied in the job's current state (HTTP 409)."""


def mutation_body(handler: BaseHTTPRequestHandler) -> object:
    """Strict same-origin guard for state-changing requests; returns the parsed JSON body.

    Requires exactly one ``Host`` naming this loopback server, an ``Origin``
    equal to that host's URL, ``application/json`` and a bounded
    ``Content-Length``. No CORS headers are ever sent, so a cross-origin page
    cannot read responses, and these checks stop it from triggering effects.
    """
    port = handler.server.server_port
    hosts = handler.headers.get_all("Host") or []
    allowed = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
    if len(hosts) != 1 or hosts[0] not in allowed:
        raise MutationRejected(HTTPStatus.FORBIDDEN, "Host not allowed")
    origins = handler.headers.get_all("Origin") or []
    if len(origins) != 1 or origins[0] != "http://" + hosts[0]:
        raise MutationRejected(HTTPStatus.FORBIDDEN, "Cross-origin request rejected")
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if content_type != "application/json":
        raise MutationRejected(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "Content-Type must be application/json")
    if handler.headers.get("Transfer-Encoding"):
        raise MutationRejected(HTTPStatus.BAD_REQUEST, "Transfer-Encoding is not supported")
    lengths = handler.headers.get_all("Content-Length") or []
    if not lengths:
        raise MutationRejected(HTTPStatus.LENGTH_REQUIRED, "Content-Length required")
    if len(lengths) != 1 or not lengths[0].strip().isdigit():
        raise MutationRejected(HTTPStatus.BAD_REQUEST, "Invalid Content-Length")
    length = int(lengths[0].strip())
    if length > MAX_MUTATION_BODY:
        raise MutationRejected(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large")
    raw = handler.rfile.read(length) if length else b""
    if len(raw) != length:
        raise MutationRejected(HTTPStatus.BAD_REQUEST, "Incomplete request body")
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise MutationRejected(HTTPStatus.BAD_REQUEST, "Body must be valid JSON") from error


def validate_steer_body(body: object) -> tuple[str, str]:
    """Accept only ``{message, requestId}``; the client never chooses workspace/model/files."""
    if not isinstance(body, dict):
        raise RequestError("Body must be a JSON object")
    extra = sorted(set(body) - {"message", "requestId"})
    if extra:
        raise RequestError("Unsupported fields: " + ", ".join(extra))
    message, request_id = body.get("message"), body.get("requestId")
    if not isinstance(message, str) or not message.strip():
        raise RequestError("message must be a non-empty string")
    message = message.strip()
    if len(message) > MAX_STEER_MESSAGE:
        raise RequestError("message must be at most " + str(MAX_STEER_MESSAGE) + " characters")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise RequestError("requestId must be 1-80 characters of A-Z, a-z, 0-9, '_' or '-'")
    return message, request_id


def ready(port: int) -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:" + str(port) + "/health", timeout=0.4) as response:
            health = json.loads(response.read())
            return health == {"ok": True, "version": DASHBOARD_VERSION}
    except OSError:
        return False


def stop_outdated_dashboard(port: int) -> None:
    """Stop only an older AGY Live dashboard occupying this dedicated port.

    Restarting the dashboard never stops a AGY worker; it only makes the new
    renderer and status logic available to the existing JSONL job records.
    """
    if not shutil.which("lsof"):
        return
    listeners = subprocess.run(
        ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
        text=True, capture_output=True, check=False,
    ).stdout.splitlines()
    for raw_pid in listeners:
        try:
            pid = int(raw_pid.strip())
        except ValueError:
            continue
        command = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True, capture_output=True, check=False,
        ).stdout
        if Path(__file__).name in command and " serve " in command:
            os.kill(pid, signal.SIGTERM)
    for _ in range(20):
        listener_still_exists = subprocess.run(
            ["lsof", "-nP", "-iTCP:" + str(port), "-sTCP:LISTEN", "-t"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        ).returncode == 0
        if not listener_still_exists:
            return
        time.sleep(0.05)


def ensure_server(port: int) -> None:
    if ready(port):
        return
    ROOT.mkdir(parents=True, exist_ok=True)
    # Several Codex threads may launch workers at the same time. Serialize only
    # server creation; job creation itself remains fully parallel.
    with (ROOT / "server-start.lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if ready(port):
                return
            stop_outdated_dashboard(port)
            with (ROOT / "server.log").open("ab") as log:
                subprocess.Popen(
                    [sys.executable, str(Path(__file__).resolve()), "serve", "--port", str(port)],
                    cwd=ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            for _ in range(50):
                if ready(port):
                    return
                time.sleep(0.1)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
    raise RuntimeError("Dashboard failed to start; inspect " + str(ROOT / "server.log"))


def _seconds(value: object, default: int, label: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(label + " must be an integer number of seconds")
    number = int(value)
    if not 1 <= number <= MAX_LIMIT_SECONDS:
        raise ValueError(label + " must be between 1 and " + str(MAX_LIMIT_SECONDS) + " seconds")
    return number


def worker_command(metadata: dict) -> list[str]:
    command = ["agy", "--output-format", "stream-json", "--print-timeout", "0",
               "--disable-slash-commands", "--mode", "accept-edits" if metadata["mode"] == "implement" else "plan",
               "--agent", metadata["agent"], "--model", metadata["model"]]
    if metadata.get("effort"):
        command += ["--effort", metadata["effort"]]
    resume = metadata.get("resumeSessionId")
    if resume is not None:
        if not valid_session_id(resume):
            raise ValueError("Invalid conversation ID")
        command += ["--conversation", resume]
    return command + ["--print", metadata["prompt"]]


def resolve_effort(model: str, requested: str | None, display_name: str = "") -> str | None:
    """Thinking models choose their own reasoning level and reject --effort."""
    if model.endswith("-thinking") or "(Thinking)" in display_name:
        if requested is not None:
            raise ValueError("This Thinking model does not accept --effort; omit the effort option")
        return None
    return requested or "low"


def spawn_supervisor(job_id: str) -> subprocess.Popen:
    """Start the Codex-owned supervisor that launches, watches and reaps the worker."""
    directory = job_dir(job_id)
    with (directory / "supervisor.log").open("ab") as log:
        supervisor = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), "supervise", job_id],
            cwd=directory, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    # A long-lived caller (the MCP server) must reap it to avoid zombies.
    threading.Thread(target=supervisor.wait, daemon=True).start()
    return supervisor


def wait_for_launch(job_id: str, timeout: float = 15.0) -> dict:
    """Wait until the supervisor persisted the worker PID (or the job ended)."""
    directory = job_dir(job_id)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        metadata = read_json(directory / "job.json")
        if metadata.get("pid") or (directory / "result.json").exists():
            return metadata
        time.sleep(0.1)
    return read_json(directory / "job.json")


def launch(args: argparse.Namespace) -> dict:
    if not shutil.which("agy"):
        raise RuntimeError("agy-cli is not in PATH")
    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.is_dir():
        raise RuntimeError("Workspace does not exist: " + str(workspace))
    files = list(getattr(args, "files", None) or [])
    resume_session = getattr(args, "resume_session_id", None)
    if resume_session is not None and not valid_session_id(resume_session):
        raise ValueError("Invalid resume session id")
    max_runtime = _seconds(getattr(args, "max_runtime", None), DEFAULT_MAX_RUNTIME, "Maximum runtime")
    idle_timeout = _seconds(getattr(args, "idle_timeout", None), DEFAULT_IDLE_TIMEOUT, "Idle timeout")
    job_id = "job-" + str(int(time.time())) + "-" + secrets.token_hex(4)
    name = getattr(args, "resume_agent", None) or agent.agent_name(args.mode, job_id)
    # Fail closed before any job state exists if the restriction cannot be enforced.
    policy = agent.write_policy(workspace, args.mode, files)
    shadows = agent.shadowing_configs(workspace, name)
    if shadows:
        raise agent.PolicyError("Workspace agent profile would shadow the isolated worker profile: "
                                + ", ".join(str(path) for path in shadows))
    available = subprocess.run(["agy", "models"], capture_output=True, text=True, timeout=30, check=True)
    model_labels = {}
    for line in available.stdout.splitlines():
        parts = line.split('\t', 1)
        if len(parts) == 2:
            model_labels[parts[0].strip()] = parts[1].strip()
    model_ids = set(model_labels)
    if args.model not in model_ids:
        raise RuntimeError("Model unavailable: " + args.model)
    effort = resolve_effort(args.model, getattr(args, "effort", None), model_labels[args.model])
    ensure_server(args.port)
    directory = job_dir(job_id)
    brief = Path(args.prompt_file).read_text(encoding="utf-8") if args.prompt_file else args.prompt
    executor_policy = read_json(Path.home() / ".local/share/agy-executor/policy.json")
    available_actions = list(executor_policy.get("workspaces", {}).get(str(workspace), {}))
    worker_prompt = "Configured shared-executor actions for this workspace: " + json.dumps(available_actions) + "\n" + CODE_ONLY_POLICY + "Mode: " + args.mode.upper() + "\nAllowed files: " + json.dumps(policy["allowedPaths"]) + "\n" + brief
    # AGY is intentionally code-only. Codex owns shell execution, builds,
    # tests, Git, simulators, servers, browsers, and process control. Each
    # launch gets its own exclusively created profile; the user's general AGY
    # profiles are never modified.
    config = agent.agent_config(name, args.mode, policy, CODE_ONLY_POLICY + "Mode: " + args.mode.upper())
    existing_profile = agent.agents_dir() / (name + ".md")
    agent_file = existing_profile if resume_session and existing_profile.exists() else agent.install_agent(name, config)
    metadata = {
        "id": job_id, "state": "starting", "pid": None, "mode": args.mode, "workspace": str(workspace),
        "startedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "createdAt": time.time(),
        "model": args.model, "effort": effort, "prompt": worker_prompt,
        "agent": name, "agentFile": str(agent_file), "tools": policy["tools"],
        "trustedTools": list(agent.TRUSTED_TOOLS), "writeScope": policy["allowedPaths"],
        "maxRuntimeSeconds": max_runtime, "idleTimeoutSeconds": idle_timeout, "supervisorPid": None,
        "originalBrief": brief,
        "autoQuotaFallback": True, "dashboardPort": args.port,
        # A fresh launch is turn 1 of its own logical task with no session yet.
        "rootTaskId": job_id, "turn": 1, "resumeSessionId": resume_session, "sessionId": None,
    }
    # Continuation lineage only; never core launch fields.
    extras = getattr(args, "extra_metadata", None) or {}
    metadata.update({key: extras[key] for key in STEER_METADATA_KEYS if key in extras})
    try:
        directory.mkdir(parents=True)
        write_json(directory / "job.json", metadata)
    except BaseException:
        agent.remove_agent(str(agent_file), name)
        raise
    try:
        supervisor = spawn_supervisor(job_id)
    except OSError as error:
        update_job(job_id, lambda current: current.update(launchError="Could not start supervisor: " + str(error)))
        finalize_job(job_id, check_alive=False)
        raise RuntimeError("Could not start the AGY Live supervisor: " + str(error)) from error
    update_job(job_id, lambda current: current.update(supervisorPid=supervisor.pid))
    current = wait_for_launch(job_id)
    dashboard_root = "http://127.0.0.1:" + str(args.port) + "/"
    return {
        "jobId": job_id, "dashboardRoot": dashboard_root, "dashboardUrl": dashboard_root + "?job=" + job_id,
        "pid": current.get("pid"), "state": snapshot(job_id, include_prompt=False)["state"],
        "agent": name, "writeScope": policy["allowedPaths"],
        "maxRuntimeSeconds": max_runtime, "idleTimeoutSeconds": idle_timeout,
        "rootTaskId": metadata.get("rootTaskId"), "turn": metadata.get("turn"),
        "resumeSessionId": resume_session,
    }


def start(args: argparse.Namespace) -> None:
    print(json.dumps(launch(args), ensure_ascii=False))


_SUPERVISOR_SIGNALLED = threading.Event()


def monitor(job_id: str, worker: subprocess.Popen, poll: float = 0.25,
            exit_grace: float | None = None) -> tuple[str | None, str | None, dict | None]:
    """Watch one worker until it exits; enforce cancel, max runtime, idle and exit grace.

    Returns ``(forcedState, reason, termination)``; all ``None`` on a natural exit.
    """
    exit_grace = EXIT_GRACE_SECONDS if exit_grace is None else exit_grace
    directory = job_dir(job_id)
    path = directory / "events.jsonl"
    started = last_activity = time.monotonic()
    size, scanned, tail, completed_at = -1, 0, b"", None
    while worker.poll() is None:
        now = time.monotonic()
        try:
            current = path.stat().st_size
        except FileNotFoundError:
            current = 0
        if current != size:
            size, last_activity = current, now
            if completed_at is None and current > scanned:
                with path.open("rb") as stream:
                    stream.seek(scanned)
                    chunk = tail + stream.read(current - scanned)
                scanned, tail = current, chunk[-32:]
                if b'"runFinished"' in chunk or b'"runError"' in chunk:
                    completed_at = now
        metadata = read_json(directory / "job.json")
        max_runtime = float(metadata.get("maxRuntimeSeconds") or DEFAULT_MAX_RUNTIME)
        idle_timeout = float(metadata.get("idleTimeoutSeconds") or DEFAULT_IDLE_TIMEOUT)
        forced = reason = None
        if metadata.get("cancelRequested") or _SUPERVISOR_SIGNALLED.is_set():
            forced, reason = "cancelled", "Cancelled at Codex request"
        elif now - started >= max_runtime:
            forced, reason = "timed_out", "Worker exceeded the maximum runtime of " + format(max_runtime, "g") + "s"
        elif completed_at is not None:
            if now - completed_at >= exit_grace:
                reason = "exitGrace"  # finished but did not exit; outcome stays AGY's own
        elif now - last_activity >= idle_timeout:
            forced, reason = "stalled", "Worker produced no output for " + format(idle_timeout, "g") + "s"
        if reason:
            # The worker is our unreaped child, so its PID/PGID cannot have been reused.
            termination = proc.terminate_group(worker.pid, TERM_GRACE_SECONDS, KILL_GRACE_SECONDS, reap=worker.poll)
            termination["reason"] = forced or reason
            return forced, (reason if forced else None), termination
        time.sleep(poll)
    return None, None, None


def supervise(args: argparse.Namespace) -> None:
    """Codex-owned supervisor: launch, monitor, reap, sweep, finalize. Never a worker tool."""
    job_id = args.job_id
    directory = job_dir(job_id)
    signal.signal(signal.SIGTERM, lambda *_: _SUPERVISOR_SIGNALLED.set())
    own = proc.process_identity(os.getpid())
    identity_update = {"supervisorPid": os.getpid()}
    if own:
        identity_update["supervisorStarted"] = own[0]
    metadata = update_job(job_id, lambda current: current.update(identity_update))
    if metadata.get("cancelRequested"):
        finalize_job(job_id, check_alive=False, forcedState="cancelled", forcedReason="Cancelled before launch")
        return
    try:
        command = worker_command(metadata)
        with (directory / "events.jsonl").open("ab") as output:
            worker = subprocess.Popen(
                command, cwd=metadata["workspace"], stdin=subprocess.DEVNULL,
                stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
            )
    except (OSError, ValueError) as error:
        finalize_job(job_id, check_alive=False, launchError="Could not launch agy-cli: " + str(error))
        return
    identity = proc.process_identity(worker.pid)
    update_job(job_id, lambda current: current.update(
        pid=worker.pid, pgid=worker.pid, state="running", launchedAt=time.time(),
        processStarted=identity[0] if identity else proc.NO_IDENTITY,
    ))
    forced, reason, termination = monitor(job_id, worker)
    with contextlib.suppress(subprocess.TimeoutExpired):
        worker.wait(timeout=KILL_GRACE_SECONDS)
    # Sweep any members of the worker's group that outlived it so nothing leaks.
    sweep = proc.terminate_group(worker.pid, TERM_GRACE_SECONDS, KILL_GRACE_SECONDS) if proc.group_alive(worker.pid) else None
    updates: dict = {"exitCode": worker.returncode, "exitedAt": time.time(), "termination": termination, "sweep": sweep}
    if forced:
        updates.update(forcedState=forced, forcedReason=reason)
    finalize_job(job_id, check_alive=False, **updates)


def stop_job(job_id: str, wait_seconds: float | None = None) -> dict:
    """Cancel one job: the supervisor escalates; without one, signal only a verified worker."""
    directory = job_dir(job_id)
    if (directory / "result.json").exists():
        return snapshot(job_id, include_prompt=False)
    metadata = update_job(job_id, lambda current: current.update(
        cancelRequested=True, cancelRequestedAt=current.get("cancelRequestedAt") or time.time()))
    wait = TERM_GRACE_SECONDS + KILL_GRACE_SECONDS + 5 if wait_seconds is None else wait_seconds
    deadline = time.monotonic() + wait
    while (time.monotonic() < deadline and not (directory / "result.json").exists()
           and is_supervisor_running(metadata, job_id)):
        time.sleep(0.2)
    if (directory / "result.json").exists():
        return snapshot(job_id, include_prompt=False)
    metadata = read_json(directory / "job.json")
    if is_supervisor_running(metadata, job_id):
        status = snapshot(job_id, include_prompt=False)
        status["error"] = "Cancellation requested; supervisor has not confirmed worker exit yet"
        return status
    if is_agy_worker_running(metadata):
        pid = metadata.get("pid")
        if not proc.owns_worker(metadata):
            raise RuntimeError("Refusing to signal PID " + str(pid) + ": no verifiable process start token for this job")
        try:
            group = os.getpgid(pid)
        except ProcessLookupError:
            group = None
        if group is not None:
            if group != pid:
                raise RuntimeError("Refusing to signal PID " + str(pid) + ": it no longer leads its own process group")
            termination = proc.terminate_group(pid, TERM_GRACE_SECONDS, KILL_GRACE_SECONDS)
            termination["reason"] = "cancelled"
            update_job(job_id, lambda current: current.update(termination=termination))
            if not termination["exited"]:
                status = snapshot(job_id, include_prompt=False)
                status["error"] = "Worker did not exit after SIGKILL: " + str(termination.get("error"))
                return status
    finalize_job(job_id, forcedState="cancelled", forcedReason="Cancelled at Codex request")
    return snapshot(job_id, include_prompt=False)


@contextlib.contextmanager
def _steer_lock(directory: Path) -> Iterator[None]:
    """Serialize steering per parent. Separate from ``job.lock`` so stop/finalize can run inside."""
    with (directory / "steer.lock").open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def original_brief(metadata: dict) -> str:
    """The Codex brief without policy prefix or steering additions (never re-nested)."""
    stored = metadata.get("originalBrief")
    if isinstance(stored, str) and stored.strip():
        return stored
    prompt = str(metadata.get("prompt") or "")
    prefix = CODE_ONLY_POLICY + "Mode: " + str(metadata.get("mode") or "").upper() + "\n"
    return prompt[len(prefix):] if prompt.startswith(prefix) else prompt


def steer_files(metadata: dict) -> list[str] | None:
    """Reconstruct the exact prior write scope as ``launch`` file arguments.

    ``None`` means "no explicit files": the whole workspace in implement mode,
    nothing in review mode. Anything not reproducible exactly fails closed.
    """
    workspace, mode, scope = metadata.get("workspace"), metadata.get("mode"), metadata.get("writeScope")
    if not isinstance(workspace, str) or not workspace:
        raise ConflictError("Job has no recorded workspace")
    if not isinstance(scope, list) or not all(isinstance(item, str) for item in scope):
        raise ConflictError("Job has no recorded write scope; cannot preserve it")
    root = Path(workspace)
    if mode == "review":
        if scope:
            raise ConflictError("Review job unexpectedly has a write scope")
        return None
    if mode != "implement":
        raise ConflictError("Unknown job mode: " + str(mode))
    if scope == [str(root), str(root) + "/**"]:
        return None
    if not scope:
        raise ConflictError("Implement job has an empty write scope")
    files = []
    for item in scope:
        path = Path(item)
        if not path.is_absolute() or "*" in item or path == root or root not in path.parents:
            raise ConflictError("Write scope entry is not a workspace file: " + item)
        files.append(path.relative_to(root).as_posix())
    return sorted(files)


def _scope_description(metadata: dict) -> str:
    mode, scope, workspace = metadata.get("mode"), metadata.get("writeScope") or [], metadata.get("workspace")
    if mode == "review":
        return "REVIEW mode: read-only, no file writes."
    if scope == [str(workspace), str(workspace) + "/**"]:
        return "IMPLEMENT mode: writes allowed in the workspace except Git/AGY/secret paths."
    return "IMPLEMENT mode: writes allowed only in: " + ", ".join(scope)


def continuation_message(metadata: dict, previous: dict, message: str, turn: int) -> str:
    """The new user message for a resumed turn.

    AGY already holds the earlier conversation (original brief and its own
    reports) in the resumed session, so neither is re-inserted here. Only the
    code-only reminder (prefixed by ``launch``), the unchanged scope, the
    previous turn's execution evidence and the new message are sent.
    """
    state = str(previous.get("state") or "unknown")
    evidence = "Previous turn (" + str(previous.get("id") or metadata.get("id")) + ") ended with state: " + state
    if previous.get("error"):
        evidence += " — " + str(previous["error"])
    if state == "cancelled":
        evidence += " (stopped so this message could be applied)"
    parts = [
        "Continuation: turn " + str(turn) + " of this same AGY session. Your earlier turns and reports above "
        "are your context; do not restart the task or repeat completed work.",
        "The execution boundary above still applies on this turn. Write scope is unchanged: "
        + _scope_description(metadata),
        "File edits from earlier turns are kept in the workspace: re-read files before editing.",
        "",
        "Execution evidence:",
        evidence + ".",
        "",
        "New message (highest priority; may include command output from Codex):",
        message,
    ]
    return "\n".join(parts) + "\n"


class SessionUnavailable(ConflictError):
    """The job's AGY session cannot be resumed; never replaced by a fresh session."""


def session_available(session_id: str, workspace: str) -> bool:
    # CLI has no machine-readable listing. Only an ID from this task's own
    # stream can reach this function. Exact --conversation resume fails closed.
    return valid_session_id(session_id)


def resumable_session(job_id: str) -> str:
    """The exact AGY session this job's turn ran in, or raise ``SessionUnavailable``.

    Sources, in order: the durable ``sessionId``; the job's own events; the id
    this turn was resumed with (only when its events reported none). Never a
    guess such as the most recent workspace session.
    """
    directory = job_dir(job_id)
    metadata = read_json(directory / "job.json")
    if not metadata:
        raise FileNotFoundError(job_id)
    blocker = ("No new session was started. Start an explicitly fresh task with start_job only if losing "
               "the conversation is acceptable.")
    if metadata.get("sessionMismatch"):
        raise SessionUnavailable("Job " + job_id + " reported a different AGY session than the one it resumed; "
                                 "refusing to continue an ambiguous session. " + blocker)
    session_id = metadata.get("sessionId")
    if not valid_session_id(session_id):
        observed, conflict = session_id_from_events(events(directory / "events.jsonl", limit=None)[0])
        if conflict:
            raise SessionUnavailable("Job " + job_id + " events name several AGY sessions; refusing to guess. " + blocker)
        resumed = metadata.get("resumeSessionId")
        if observed and valid_session_id(resumed) and observed != resumed:
            raise SessionUnavailable("Job " + job_id + " ran in session " + observed + ", not resumed session "
                                     + str(resumed) + ". " + blocker)
        session_id = observed or (resumed if valid_session_id(resumed) else None)
    if not valid_session_id(session_id):
        raise SessionUnavailable("AGY session id for job " + job_id + " is unknown (its events never reported one). "
                                 + blocker)
    if not session_available(session_id, str(metadata.get("workspace") or "")):
        raise SessionUnavailable("AGY session " + session_id + " for job " + job_id + " is not available from "
                                 "`agy-cli chat --list-sessions --format json` in " + str(metadata.get("workspace"))
                                 + ". " + blocker)
    return session_id


@contextlib.contextmanager
def _session_lock(session_id: str) -> Iterator[None]:
    """Serialize turn decisions per AGY session, across jobs and processes."""
    directory = ROOT / "sessions"
    directory.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32] + ".lock"
    with (directory / name).open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _active_session_job(session_id: str, exclude: str) -> str | None:
    """Another live job already running a turn in this AGY session."""
    root = ROOT / "jobs"
    if not root.exists():
        return None
    for directory in root.iterdir():
        if not directory.is_dir() or directory.name == exclude:
            continue
        metadata = read_json(directory / "job.json")
        if not metadata or session_id not in (metadata.get("sessionId"), metadata.get("resumeSessionId")):
            continue
        if (directory / "result.json").exists() or metadata.get("steerRejected"):
            continue
        if is_agy_worker_running(metadata) or is_supervisor_running(metadata, directory.name) or in_startup_grace(metadata):
            return directory.name
    return None


def _receipt(parent_id: str, child_id: str, request_id: str, port: int, state: object = None,
             session_id: object = None, root_id: object = None, turn: object = None) -> dict:
    root = "http://127.0.0.1:" + str(port) + "/"
    return {"jobId": child_id, "parentJobId": parent_id, "requestId": request_id,
            "dashboardUrl": root + "?job=" + child_id, "state": state, "createdAt": time.time(),
            "sessionId": session_id, "rootTaskId": root_id, "turn": turn, "sameSession": True}


def _find_child(parent_id: str, request_id: object) -> dict | None:
    """A launched child for this steering request (recovers an interrupted steer)."""
    root = ROOT / "jobs"
    if not isinstance(request_id, str) or not root.exists():
        return None
    for directory in root.iterdir():
        metadata = read_json(directory / "job.json") if directory.is_dir() else {}
        if (metadata and metadata.get("parentJobId") == parent_id and metadata.get("steerRequestId") == request_id
                and not metadata.get("steerRejected")):
            return metadata
    return None


def _record_child(parent_id: str, receipt: dict) -> dict:
    def record(current: dict) -> None:
        current["supersededBy"] = receipt["jobId"]
        receipts = current.get("steeringReceipts")
        if not isinstance(receipts, dict):
            receipts = current["steeringReceipts"] = {}
        receipts.setdefault(receipt["requestId"], receipt)
        current.pop("steerPending", None)

    return update_job(parent_id, record)


def validate_continue_args(message: object, request_id: object, limit: int = MAX_CONTINUE_MESSAGE) -> tuple[str, str]:
    if not isinstance(message, str) or not message.strip():
        raise RequestError("message must be a non-empty string")
    message = message.strip()
    if len(message) > limit:
        raise RequestError("message must be at most " + str(limit) + " characters")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise RequestError("request_id must be 1-80 characters of A-Z, a-z, 0-9, '_' or '-'")
    return message, request_id


def continue_job(job_id: str, message: str, request_id: str, port: int = PORT, interrupt: bool = False,
                 fallback_model: str | None = None) -> dict:
    """Send a new user message to the *same* AGY session as a new turn. Codex-owned process control.

    The new turn is a fresh ``agy-cli`` process launched with exactly
    ``--resume-id <sessionId>`` captured from this job's own events, with a
    fresh isolated code-only profile and the identical write scope. If the
    session is unknown or unavailable, ``SessionUnavailable`` is raised and no
    process is started: a continuation never silently becomes a new session.

    Serialized per job (``steer.lock``) and per AGY session; ``request_id``
    makes retries idempotent; at most one successor per turn. A running turn
    is rejected unless ``interrupt`` (dashboard) asks to stop it first.
    """
    message, request_id = validate_continue_args(message, request_id)
    directory = job_dir(job_id)
    if not read_json(directory / "job.json"):
        raise FileNotFoundError(job_id)
    with _steer_lock(directory):
        metadata = read_json(directory / "job.json")
        if not metadata:
            raise FileNotFoundError(job_id)
        pending = metadata.get("steerPending")
        if isinstance(pending, dict) and not metadata.get("supersededBy"):
            # A previous continuation died mid-flight; adopt its child rather than launching another.
            child = _find_child(job_id, pending.get("requestId"))
            if child:
                metadata = _record_child(job_id, _receipt(job_id, child["id"], pending["requestId"], port,
                                                          session_id=child.get("resumeSessionId"),
                                                          root_id=child.get("rootTaskId"), turn=child.get("turn")))
            else:
                metadata = update_job(job_id, lambda current: current.pop("steerPending", None) and None)
        receipts = metadata.get("steeringReceipts") if isinstance(metadata.get("steeringReceipts"), dict) else {}
        if request_id in receipts:
            return {**receipts[request_id], "duplicate": True}
        if metadata.get("supersededBy"):
            raise ConflictError("Job " + job_id + " was already continued in " + str(metadata["supersededBy"])
                                + "; continue the latest turn instead")
        files = steer_files(metadata)  # validate before stopping anything
        for key in ("mode", "model"):
            if not metadata.get(key):
                raise ConflictError("Job record is missing " + key + "; cannot continue it")
        status = snapshot(job_id, include_prompt=False)
        if not status.get("done") and not interrupt:
            raise ConflictError("Turn " + job_id + " is still " + str(status.get("state"))
                                + "; wait_job until done (or stop_job) before continuing the session")
        if fallback_model is not None and (not status.get("done") or status.get("state") != "failed"
                or fallback_model != next_quota_model(metadata.get("model"), status.get("error"))):
            raise ConflictError("Model fallback is only allowed after the current model exhausts quota")
        selected_model = fallback_model or metadata["model"]
        selected_effort = ("high" if selected_model == "gemini-3.8-flash-high" else None) if fallback_model else metadata.get("effort")
        session_id = resumable_session(job_id)  # explicit blocker; nothing has been stopped yet
        root_id = root_task_id(metadata) or job_id
        turn = turn_number(metadata) + 1
        with _session_lock(session_id):
            other = _active_session_job(session_id, exclude=job_id)
            if other:
                raise ConflictError("AGY session " + session_id + " already has a running turn (" + other
                                    + "); wait for it instead of starting a concurrent turn")
            original = original_brief(metadata)
            history = [item for item in metadata.get("userMessages") or [] if isinstance(item, dict)]
            update_job(job_id, lambda current: current.update(steerPending={"requestId": request_id, "at": time.time()}))
            recorded = False
            try:
                if not status.get("done"):
                    stopped = stop_job(job_id)
                    status = snapshot(job_id, include_prompt=False)
                    if not status.get("done") or status.get("processAlive"):
                        detail = stopped.get("error") if isinstance(stopped, dict) else None
                        raise ConflictError("Previous turn has not confirmed exit; no new turn was started"
                                            + (": " + str(detail) if detail else ""))
                entry = {"requestId": request_id, "message": message, "at": time.time(), "parentJobId": job_id}
                launched = launch(argparse.Namespace(
                    workspace=metadata["workspace"], mode=metadata["mode"], model=selected_model,
                    effort=selected_effort, prompt=continuation_message(metadata, status, message, turn),
                    prompt_file=None, port=port, files=files, resume_session_id=session_id, resume_agent=metadata["agent"],
                    max_runtime=metadata.get("maxRuntimeSeconds"), idle_timeout=metadata.get("idleTimeoutSeconds"),
                    extra_metadata={"parentJobId": job_id, "steerRequestId": request_id, "originalBrief": original,
                                    "userMessages": history + [entry], "rootTaskId": root_id, "turn": turn,
                                    "turnMessage": message},
                ))
                child_id = launched["jobId"]
                child = read_json(job_dir(child_id) / "job.json")
                problem = None
                if launched.get("writeScope") != metadata.get("writeScope"):
                    problem = "Continued write scope differs from the original"
                elif child.get("resumeSessionId") != session_id:
                    problem = "Continued turn was not bound to session " + session_id
                if problem:
                    with contextlib.suppress(Exception):
                        update_job(child_id, lambda current: current.update(steerRejected=True))
                        stop_job(child_id)
                    raise ConflictError(problem + "; the new turn was stopped")
                receipt = _receipt(job_id, child_id, request_id, port, launched.get("state"),
                                   session_id=session_id, root_id=root_id, turn=turn)
                _record_child(job_id, receipt)
                recorded = True
                return receipt
            finally:
                if not recorded:
                    with contextlib.suppress(FileNotFoundError):
                        child = _find_child(job_id, request_id)
                        if child:  # launched but failed afterwards: keep it as the one successor
                            _record_child(job_id, _receipt(job_id, child["id"], request_id, port,
                                                           session_id=session_id, root_id=root_id, turn=turn))
                        else:
                            update_job(job_id, lambda current: False if current.pop("steerPending", None) is None else None)


def steer_job(job_id: str, message: str, request_id: str, port: int = PORT) -> dict:
    """Dashboard message: stop the running turn if needed, then continue the same session."""
    return continue_job(job_id, message, request_id, port=port, interrupt=True)


def task_history(job_id: str, offset: int = 0, limit: int = HISTORY_PAGE) -> dict:
    """Ordered turns of the logical task containing ``job_id``, with full reports.

    Paginated by turn (``offset``/``limit``, max ``MAX_HISTORY_PAGE``) so a
    long conversation never yields one unbounded response; every report is
    complete and also available at ``reportPath``.
    """
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise RequestError("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_HISTORY_PAGE:
        raise RequestError("limit must be an integer from 1 to " + str(MAX_HISTORY_PAGE))
    metadata = read_json(job_dir(job_id) / "job.json")
    if not metadata:
        raise FileNotFoundError(job_id)
    root = root_task_id(metadata) or job_id
    members: list[dict] = []
    for directory in (ROOT / "jobs").iterdir():
        record = read_json(directory / "job.json") if directory.is_dir() else {}
        if record and not record.get("steerRejected") and root_task_id(record) == root:
            members.append(record)
    members.sort(key=lambda record: (turn_number(record), _created_at(record) or 0.0, record.get("id") or ""))
    turns: list[dict] = []
    for record in members[offset:offset + limit]:
        status = snapshot(record["id"], include_prompt=False)
        record = read_json(job_dir(record["id"]) / "job.json") or record
        turns.append({
            "jobId": record["id"], "turn": status["turn"], "parentJobId": status.get("parentJobId"),
            "supersededBy": status.get("supersededBy"), "requestId": record.get("steerRequestId"),
            "sessionId": status.get("sessionId"), "resumeSessionId": status.get("resumeSessionId"),
            "state": status["state"], "done": status["done"], "acknowledged": status["acknowledged"],
            "startedAt": record.get("startedAt"), "finishedAt": record.get("finishedAt"),
            # Turn 1's message is the Codex brief; later turns carry their own new message only.
            "message": record.get("turnMessage") if record.get("parentJobId") else original_brief(record),
            "report": status.get("report"), "reportPath": status.get("reportPath"),
            "finalTextTruncated": status.get("finalTextTruncated"), "changedFiles": status.get("changedFiles"),
            "error": status.get("error"),
        })
    latest = next((record for record in reversed(members) if not record.get("supersededBy")), members[-1] if members else metadata)
    end = offset + len(turns)
    return {
        "rootTaskId": root, "latestJobId": latest.get("id"), "sessionId": latest.get("sessionId") or latest.get("resumeSessionId"),
        "originalBrief": original_brief(members[0]) if members else original_brief(metadata),
        "turnCount": len(members), "offset": offset, "limit": limit,
        "nextOffset": end if end < len(members) else None, "turns": turns,
    }


def next_quota_model(model: object, error: object) -> str | None:
    # Only an explicit model-quota error triggers this user-approved sequence.
    if not isinstance(error, str) or "individual quota reached" not in error.lower():
        return None
    return {
        "claude-opus-4-6-thinking": "claude-sonnet-4-6",
        "claude-sonnet-4-6": "gemini-3.8-flash-high",
    }.get(model)


def auto_quota_fallback(job_id: str, status: dict) -> dict | None:
    metadata = read_json(job_dir(job_id) / "job.json")
    if (not metadata.get("autoQuotaFallback") or metadata.get("supersededBy")
            or metadata.get("autoFallbackError") or status.get("state") != "failed"
            or not status.get("done")):
        return None
    model = next_quota_model(metadata.get("model"), status.get("error"))
    if not model:
        return None
    try:
        return continue_job(
            job_id,
            "The previous model exhausted its quota. Continue the unfinished objective using " + model
            + " in this same conversation. Preserve existing edits, scope and full results. Do not repeat completed work.",
            "auto-quota-fallback", port=metadata.get("dashboardPort", PORT), fallback_model=model,
        )
    except Exception as error:
        # Fail closed if resume/catalog/scope validation fails. Never start a new
        # conversation or retry a failed fallback endlessly in the background.
        update_job(job_id, lambda current: current.update(autoFallbackError=str(error)))
        return None


def serve(args: argparse.Namespace) -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    maintenance_stop = threading.Event()

    def maintain() -> None:
        # Retention must not delay health requests, assets or worker startup.
        while not maintenance_stop.is_set():
            try:
                cleanup_finished_jobs()
                jobs_root = ROOT / "jobs"
                if jobs_root.exists():
                    for directory in jobs_root.iterdir():
                        if not directory.is_dir():
                            continue
                        metadata = read_json(directory / "job.json")
                        result = read_json(directory / "result.json")
                        if metadata.get("autoQuotaFallback") and result.get("state") == "failed":
                            auto_quota_fallback(directory.name, {**result, "done": True})
            except Exception as error:
                print("AGY Live maintenance failed: " + str(error), file=sys.stderr, flush=True)
            maintenance_stop.wait(60)

    threading.Thread(target=maintain, name="agy-live-maintenance", daemon=True).start()
    print("AGY Live listening on http://127.0.0.1:" + str(args.port), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        maintenance_stop.set()
        server.server_close()


def wait_job(job_id: str, timeout_seconds: int = 50) -> dict:
    """Wait briefly for a durable result; expiration never ends the worker."""
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= 50:
        raise ValueError("timeout_seconds must be an integer from 1 to 50")
    deadline = time.monotonic() + timeout_seconds
    while True:
        status = snapshot(job_id, include_prompt=False)
        if status.get("done") and status.get("supersededBy"):
            # Redirected from the dashboard: this run is obsolete; wait on its successor.
            return {**status, "waitTimedOut": False, "nextAction": "wait_superseding_job",
                    "followJobId": status["supersededBy"]}
        if status.get("done") and not status.get("steerPending"):
            successor = auto_quota_fallback(job_id, status)
            if successor:
                return {**status, "waitTimedOut": False, "nextAction": "wait_superseding_job",
                        "followJobId": successor["jobId"], "fallbackModel": next_quota_model(status.get("model"), status.get("error"))}
            latest = read_json(job_dir(job_id) / "job.json")
            if latest.get("supersededBy"):
                return {**status, "waitTimedOut": False, "nextAction": "wait_superseding_job",
                        "followJobId": latest["supersededBy"]}
            return {**status, "autoFallbackError": latest.get("autoFallbackError"),
                    "waitTimedOut": False, "nextAction": "review_result"}
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return {**status, "waitTimedOut": True, "nextAction": "continue_waiting"}
        time.sleep(min(1, remaining))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    launch = commands.add_parser("start", help="Start AGY and print the AGY Live URL")
    launch.add_argument("--workspace", required=True)
    launch.add_argument("--mode", required=True, choices=("review", "implement"))
    brief = launch.add_mutually_exclusive_group(required=True)
    brief.add_argument("--prompt")
    brief.add_argument("--prompt-file", help="UTF-8 brief file; avoids shell interpolation")
    launch.add_argument("--model", default="gemini-3.8-flash-low")
    launch.add_argument("--effort", default=None, choices=("low", "medium", "high"),
                        help="Reasoning effort; omitted for Thinking models and defaults to low otherwise")
    launch.add_argument("--port", type=int, default=PORT)
    launch.add_argument("--file", dest="files", action="append", default=None,
                        help="Implement mode: restrict writes to this workspace file (repeatable)")
    launch.add_argument("--max-runtime", dest="max_runtime", type=int, default=None,
                        help="Seconds before the supervisor terminates the worker (default %d)" % DEFAULT_MAX_RUNTIME)
    launch.add_argument("--idle-timeout", dest="idle_timeout", type=int, default=None,
                        help="Seconds without output before the worker is treated as stalled (default %d)" % DEFAULT_IDLE_TIMEOUT)
    launch.set_defaults(handler=start)
    web = commands.add_parser("serve", help="Serve the local dashboard")
    web.add_argument("--port", type=int, default=PORT)
    web.set_defaults(handler=serve)
    supervisor = commands.add_parser("supervise", help=argparse.SUPPRESS)
    supervisor.add_argument("job_id")
    supervisor.set_defaults(handler=supervise)
    stop = commands.add_parser("stop", help="Cancel one job and confirm its worker exited")
    stop.add_argument("job_id")
    stop.set_defaults(handler=lambda args: print(json.dumps(stop_job(args.job_id), ensure_ascii=False, indent=2)))
    wait = commands.add_parser("wait", help="Wait up to 50 seconds; repeat until done is true")
    wait.add_argument("job_id")
    wait.add_argument("--timeout-seconds", type=int, default=50)
    wait.set_defaults(handler=lambda args: print(json.dumps(wait_job(args.job_id, args.timeout_seconds), ensure_ascii=False, indent=2)))
    status = commands.add_parser("status", help="Show one job status")
    status.add_argument("job_id")
    status.set_defaults(handler=lambda args: print(json.dumps(snapshot(args.job_id), ensure_ascii=False, indent=2)))
    ack = commands.add_parser("ack", help="Mark a finished job's report as consumed")
    ack.add_argument("job_id")
    ack.set_defaults(handler=lambda args: print(json.dumps(acknowledge_job(args.job_id), ensure_ascii=False, indent=2)))
    resume = commands.add_parser("continue", help="Send a new message to the same AGY session as a new turn")
    resume.add_argument("job_id")
    resume.add_argument("--message-file", required=True, help="UTF-8 message file; avoids shell interpolation")
    resume.add_argument("--request-id", required=True, help="Idempotency key (A-Z, a-z, 0-9, _ or -)")
    resume.add_argument("--port", type=int, default=PORT)
    resume.set_defaults(handler=lambda args: print(json.dumps(continue_job(
        args.job_id, Path(args.message_file).read_text(encoding="utf-8"), args.request_id, port=args.port),
        ensure_ascii=False, indent=2)))
    past = commands.add_parser("history", help="Show every turn of the logical task with full reports")
    past.add_argument("job_id")
    past.add_argument("--offset", type=int, default=0)
    past.add_argument("--limit", type=int, default=HISTORY_PAGE)
    past.set_defaults(handler=lambda args: print(json.dumps(task_history(args.job_id, args.offset, args.limit),
                                                            ensure_ascii=False, indent=2)))
    args = parser.parse_args()
    try:
        args.handler(args)
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError, KeyError) as error:
        print("agy-live: " + str(error), file=sys.stderr)
        return 1
    return 0


PAGE = """<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AGY Live</title><style>
:root{color-scheme:dark;--bg:#101114;--panel:#181a20;--line:#2c3039;--text:#eef0f5;--muted:#9ba3b3;--green:#66d19e;--yellow:#e9c46a;--purple:#c99cff;--red:#ff7b84}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,sans-serif}header{position:sticky;top:0;display:flex;justify-content:space-between;align-items:center;padding:18px max(22px,calc((100vw - 1200px)/2));background:#101114ee;backdrop-filter:blur(12px);border-bottom:1px solid var(--line)}h1{font-size:19px;margin:0}.sub,.hint{color:var(--muted);font-size:12px}.badge{border:1px solid var(--line);border-radius:99px;padding:5px 10px;font-size:12px;font-weight:650}.running{color:var(--green)}main{max-width:1200px;margin:auto;padding:22px}.cards{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:12px}.card,.panel{background:var(--panel);border:1px solid var(--line);border-radius:12px}.card{padding:13px 15px}.label{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em}.value{margin-top:5px;font-weight:650;overflow-wrap:anywhere}.head{display:flex;justify-content:space-between;align-items:center;padding:13px 15px;border-bottom:1px solid var(--line)}h2{margin:0;font-size:13px}pre{margin:0;white-space:pre-wrap;word-break:break-word;font:12px/1.45 ui-monospace,monospace;color:#d9deea}.brief{padding:14px;max-height:170px;overflow:auto}.event{border-left:3px solid var(--line);margin:12px 15px;padding:9px 12px;background:#13151a;border-radius:0 8px 8px 0}.thinking{border-color:var(--purple)}.tool{border-color:var(--yellow)}.error{border-color:var(--red)}.meta{display:flex;gap:10px;color:var(--muted);font-size:12px;margin-bottom:7px}.kind{color:var(--text);font-weight:650}.empty{color:var(--muted);text-align:center;padding:26px}.older{display:block;margin:14px auto 2px;padding:7px 11px;background:#222631;color:var(--text);border:1px solid #3a4050;border-radius:7px;cursor:pointer;font-weight:650}.older[hidden]{display:none}@media(max-width:720px){.cards{grid-template-columns:repeat(2,1fr)}main{padding:15px}header{padding:15px}}</style>
<header><div><h1>AGY Live</h1><div id="sub" class="sub">Connecting…</div></div><div id="state" class="badge">…</div></header>
<main><section class="cards"><div class="card"><div class="label">Mode</div><div id="mode" class="value">—</div></div><div class="card"><div class="label">Elapsed</div><div id="elapsed" class="value">—</div></div><div class="card"><div class="label">Events</div><div id="count" class="value">0</div></div><div class="card"><div class="label">Model</div><div id="model" class="value">—</div></div></section>
<section class="panel" style="margin-bottom:12px"><div class="head"><h2>Worker brief</h2><span class="hint">Local only · 127.0.0.1</span></div><pre id="brief" class="brief"></pre></section><section class="panel"><div class="head"><h2>Live activity</h2><span class="hint">Latest 50 · Refreshes every 0.8s</span></div><div id="events"><button id="older" class="older" hidden>Load older</button><div class="empty">Waiting for AGY to emit an event…</div></div></section></main>
<script>const job=new URLSearchParams(location.search).get('job'),PAGE=50,MAX=12000;let oldest=null,next=0,loaded=false;const q=s=>document.querySelector(s);function type(e){return e.type||e.method||e.event||e.kind||'event'}function style(e){let x=JSON.stringify(e).toLowerCase();return x.includes('error')||x.includes('failed')?'error':x.includes('thinking')||x.includes('reasoning')?'thinking':x.includes('tool')||x.includes('command')||x.includes('terminal')?'tool':''}function detail(e){let v;if(typeof e==='string')v=e;else{for(const k of ['text','message','content','params','data','result','payload'])if(e[k]!=null){v=typeof e[k]==='string'?e[k]:JSON.stringify(e[k],null,2);break}v??=JSON.stringify(e,null,2)}return v.length>MAX?v.slice(0,MAX)+'\\n\\n… truncated in UI ('+v.length.toLocaleString()+' chars) …':v}function node(item){let box=document.createElement('article');box.className='event '+style(item.event);let meta=document.createElement('div');meta.className='meta';let a=document.createElement('span');a.className='kind';a.textContent=type(item.event);let b=document.createElement('span');b.textContent='#'+(item.index+1);meta.append(a,b);let body=document.createElement('pre');body.textContent=detail(item.event);box.append(meta,body);return box}function show(items,prepend){let target=q('#events'),empty=target.querySelector('.empty');if(empty)empty.remove();let button=q('#older');for(const item of items){let box=node(item);prepend?target.insertBefore(box,button.nextSibling):target.append(box)}}function elapsed(value){if(!value)return '—';let s=Math.max(0,Math.floor((Date.now()-new Date(value))/1000)),m=Math.floor(s/60),h=Math.floor(m/60);return h?h+'h '+m%60+'m':m+'m '+s%60+'s'}async function activity(before,prepend){let u='/api/jobs/'+job+'/events?limit='+PAGE+(before===null?'':'&before='+before),stream=await fetch(u,{cache:'no-store'}).then(r=>r.json());show(stream.events,prepend);oldest=stream.oldestOffset;next=stream.nextOffset;q('#older').hidden=!stream.hasOlder}async function fresh(){let stream=await fetch('/api/jobs/'+job+'/events?after='+next,{cache:'no-store'}).then(r=>r.json());show(stream.events,false);next=stream.nextOffset}async function tick(){if(!job){q('#sub').textContent='No job selected';return}try{let s=await fetch('/api/jobs/'+job,{cache:'no-store'}).then(r=>r.json());q('#sub').textContent=s.workspace||'';q('#state').textContent=s.state;q('#state').className='badge '+s.state;q('#mode').textContent=s.mode||'—';q('#elapsed').textContent=elapsed(s.startedAt);q('#count').textContent=s.eventCount||0;q('#model').textContent=(s.model||'—')+' / '+(s.effort||'—');q('#brief').textContent=s.prompt||'';if(!loaded){loaded=true;await activity(null,false)}else await fresh()}catch(e){q('#sub').textContent='Dashboard unavailable: '+e.message}}q('#older').onclick=async()=>{q('#older').disabled=true;await activity(oldest,true);q('#older').disabled=false};tick();setInterval(tick,800)</script>"""

PAGE = re.sub(r"<script>.*?</script>", "", PAGE, flags=re.DOTALL)
PAGE = PAGE.replace(
    '<div id="sub" class="sub">Connecting…</div></div><div id="state"',
    '<div id="sub" class="sub">Connecting…</div></div><label class="sub">Job <select id="job-picker"></select></label><div id="state"',
).replace('Latest 50 · Refreshes every 0.8s', 'Latest 30 · Refreshes every 2.5s')

DASHBOARD_SCRIPT = r"""
const PAGE_SIZE=30, REFRESH_MS=2500, MAX_CHARS=12000;
let job=new URLSearchParams(location.search).get('job'), oldest=null, next=0, loaded=false, ticking=false, epoch=0;
const q=selector=>document.querySelector(selector);
const json=async url=>{const response=await fetch(url,{cache:'no-store'});if(!response.ok)throw new Error('HTTP '+response.status);return response.json()};
const type=event=>event.type||event.method||event.event||event.kind||'event';
const style=event=>{const value=JSON.stringify(event).toLowerCase();return value.includes('error')||value.includes('failed')?'error':value.includes('thinking')||value.includes('reasoning')?'thinking':value.includes('tool')||value.includes('command')||value.includes('terminal')?'tool':''};
const chunkText=event=>{const update=event&&(event.update||(event.data&&event.data.update)),content=update&&update.content;return update&&update.sessionUpdate==='agent_message_chunk'&&content&&content.type==='text'&&typeof content.text==='string'?content.text:null};
function compact(items){const output=[];for(const item of items){const text=chunkText(item.event),previous=output.at(-1);if(text!==null&&previous&&previous.chunk){const update=previous.event.update||(previous.event.data&&previous.event.data.update);update.content.text+=text;previous.endIndex=item.index;continue}output.push({...item,endIndex:item.index,chunk:text!==null})}return output}
function detail(event){const text=chunkText(event);if(text!==null)return text;let value;if(typeof event==='string')value=event;else{for(const key of ['text','message','content','params','data','result','payload'])if(event[key]!=null){value=typeof event[key]==='string'?event[key]:JSON.stringify(event[key],null,2);break}value??=JSON.stringify(event,null,2)}return value.length>MAX_CHARS?value.slice(0,MAX_CHARS)+'\n\n… truncated in UI ('+value.length.toLocaleString()+' chars) …':value}
function eventNode(item){const box=document.createElement('article');box.className='event '+style(item.event);const meta=document.createElement('div');meta.className='meta';const kind=document.createElement('span');kind.className='kind';kind.textContent=type(item.event);const index=document.createElement('span');index.textContent='#'+(item.index+1)+(item.endIndex!==item.index?'–'+(item.endIndex+1):'');meta.append(kind,index);const body=document.createElement('pre');body.textContent=detail(item.event);box.append(meta,body);return box}
function show(items,prepend){const target=q('#events'),empty=target.querySelector('.empty');if(empty)empty.remove();const button=q('#older');for(const item of (prepend?compact(items).reverse():compact(items))){const box=eventNode(item);prepend?target.insertBefore(box,button.nextSibling):target.append(box)}}
function elapsed(value){if(!value)return '—';const seconds=Math.max(0,Math.floor((Date.now()-new Date(value))/1000)),minutes=Math.floor(seconds/60),hours=Math.floor(minutes/60);return hours?hours+'h '+minutes%60+'m':minutes+'m '+seconds%60+'s'}
function resetEvents(){oldest=null;next=0;loaded=false;const target=q('#events');target.replaceChildren();const button=document.createElement('button');button.id='older';button.className='older';button.hidden=true;button.textContent='Load older';button.onclick=loadOlder;target.append(button);const empty=document.createElement('div');empty.className='empty';empty.textContent='Waiting for AGY to emit an event…';target.append(empty)}
function selectJob(id){if(id===job)return;job=id;epoch++;history.replaceState(null,'','?job='+encodeURIComponent(id));resetEvents()}
async function refreshPicker(){const catalog=await json('/api/jobs'),jobs=catalog.jobs||[],picker=q('#job-picker');if(!jobs.length){picker.replaceChildren();return false}if(!job||!jobs.some(item=>item.id===job))selectJob(jobs[0].id);picker.replaceChildren(...jobs.map(item=>{const option=document.createElement('option');option.value=item.id;option.textContent=(item.state||'unknown')+' · '+(item.mode||'task')+' · '+item.id;return option}));picker.value=job;return true}
// Every response is tied to the job and epoch that requested it; a response
// arriving after the user switched jobs (or after the cursor moved) is dropped.
const current=(mine,stream)=>mine.epoch===epoch&&mine.job===job&&stream.jobId===job;
async function activity(before,prepend){const mine={epoch,job},url='/api/jobs/'+encodeURIComponent(job)+'/events?limit='+PAGE_SIZE+(before===null?'':'&before='+before),stream=await json(url);if(!current(mine,stream))return;if(prepend&&before!==oldest)return;show(stream.events,prepend);oldest=stream.oldestOffset;if(!prepend)next=stream.nextOffset;q('#older').hidden=!stream.hasOlder}
async function loadOlder(){const button=q('#older');button.disabled=true;try{await activity(oldest,true)}finally{button.disabled=false}}
async function fresh(){const mine={epoch,job},from=next,stream=await json('/api/jobs/'+encodeURIComponent(job)+'/events?after='+from);if(!current(mine,stream)||next!==from)return;show(stream.events,false);next=stream.nextOffset}
async function tick(){if(ticking)return;ticking=true;try{if(!await refreshPicker()){q('#sub').textContent='No recent AGY jobs';return}const mine={epoch,job},status=await json('/api/jobs/'+encodeURIComponent(job));if(mine.epoch!==epoch||status.id!==job)return;q('#sub').textContent=status.workspace||'';q('#state').textContent=status.state+(status.processAlive?'':' · exited');q('#state').className='badge '+status.state;q('#mode').textContent=status.mode||'—';q('#elapsed').textContent=elapsed(status.startedAt);q('#count').textContent=status.eventCount||0;q('#model').textContent=(status.model||'—')+' / '+(status.effort||'—');q('#brief').textContent=status.prompt||'';if(!loaded){loaded=true;await activity(null,false)}else await fresh()}catch(error){q('#sub').textContent='Dashboard unavailable: '+error.message}finally{ticking=false}}
q('#job-picker').onchange=event=>{selectJob(event.target.value);tick()};resetEvents();tick();setInterval(tick,REFRESH_MS);
"""


if __name__ == "__main__":
    raise SystemExit(main())
