"""Derive a worker's outcome and final report from its stream-json events.

Handles bare ACP updates (``{"update": ...}``) and the AGY CLI envelopes
(``{"type": "sessionUpdate", "data": {"update": ...}}``,
``{"type": "runFinished", "data": {"status", "stopReason", "finalText",
"finalTextTruncated"}}`` and ``{"type": "runError", "data": {"message"}}``).
A run is only "finished" when it completed with a success status and an
``end_turn`` (or absent) stop reason.
"""

from __future__ import annotations

import json

SUCCESS_STATUSES = {"success", "succeeded", "completed", "complete", "ok", "done"}
CANCELLED = {"cancelled", "canceled"}
SUCCESS_STOP_REASONS = {None, "", "end_turn", "endTurn"}
EDIT_KINDS = {"edit", "delete", "move"}


def _update(event: dict) -> dict:
    for candidate in (event.get("update"), (event.get("data") or {}).get("update") if isinstance(event.get("data"), dict) else None,
                      (event.get("params") or {}).get("update") if isinstance(event.get("params"), dict) else None):
        if isinstance(candidate, dict):
            return candidate
    return {}


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    if isinstance(value, dict):
        for key in ("message", "text", "detail", "error"):
            found = _text(value.get(key))
            if found:
                return found
    return None


def error_text(event: dict) -> str:
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    message = _text(data.get("message")) or _text(data.get("error")) or _text(event.get("message")) or _text(event.get("error"))
    nested = data.get("error") if isinstance(data.get("error"), dict) else {}
    code = data.get("code") or nested.get("code")
    if message:
        return message + (" (code " + str(code) + ")" if code not in (None, "") else "")
    if data:
        return "Worker reported runError: " + json.dumps(data, ensure_ascii=False)[:2000]
    return "Worker reported runError without details"


def _paths(update: dict) -> list[str]:
    output = [item.get("path") for item in update.get("locations") or [] if isinstance(item, dict)]
    raw = update.get("rawInput") if isinstance(update.get("rawInput"), dict) else {}
    output += [raw.get("path"), raw.get("file_path")]
    output += [item for item in raw.get("paths") or [] if isinstance(item, str)]
    return [item for item in output if isinstance(item, str) and item]


def summarize(items: list[dict]) -> dict:
    completion: str | None = None
    final: dict = {}
    error: str | None = None
    details: object = None
    all_text: list[str] = []
    tail_text: list[str] = []  # assistant text since the last tool activity
    calls: dict[str, dict] = {}
    for item in items:
        event = item.get("event")
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind in ("runFinished", "runError"):
            completion = kind
            final = event.get("data") if isinstance(event.get("data"), dict) else {}
            error = error_text(event) if kind == "runError" else None
            details = final.get("details", event.get("details"))
            continue
        update = _update(event)
        session_update = update.get("sessionUpdate")
        if session_update == "agent_message_chunk":
            content = update.get("content") or {}
            if content.get("type") == "text" and isinstance(content.get("text"), str):
                all_text.append(content["text"])
                tail_text.append(content["text"])
        elif session_update in ("tool_call", "tool_call_update"):
            tail_text = []
            call = calls.setdefault(str(update.get("toolCallId") or len(calls)), {"kind": None, "status": None, "paths": []})
            call["kind"] = update.get("kind") or call["kind"]
            call["status"] = update.get("status") or call["status"]
            call["paths"] += [path for path in _paths(update) if path not in call["paths"]]

    status = final.get("status")
    stop_reason = final.get("stopReason") or ((final.get("result") or {}).get("stopReason") if isinstance(final.get("result"), dict) else None)
    normalized_status = str(status).lower() if status is not None else None
    outcome: str | None = None
    if completion == "runError":
        outcome = "failed"
    elif completion == "runFinished":
        if (stop_reason and str(stop_reason).lower() in CANCELLED) or normalized_status in CANCELLED:
            outcome, error = "cancelled", "Worker run was cancelled (stopReason " + str(stop_reason or status) + ")"
        elif normalized_status is not None and normalized_status not in SUCCESS_STATUSES:
            outcome = "failed"
            error = _text(final.get("message")) or _text(final.get("error")) or "Worker reported status " + str(status)
        elif stop_reason not in SUCCESS_STOP_REASONS:
            outcome, error = "failed", "Worker stopped with stopReason " + str(stop_reason)
        else:
            outcome = "finished"

    final_text = final.get("finalText") if isinstance(final.get("finalText"), str) else None
    truncated = bool(final.get("finalTextTruncated"))
    streamed_tail, streamed_all = "".join(tail_text), "".join(all_text)
    report_text = final_text if final_text else (streamed_tail or streamed_all or None)
    if final_text and truncated:
        probe = final_text.strip()[:40]
        if streamed_tail and len(streamed_tail) > len(final_text) and (not probe or probe in streamed_tail):
            report_text, truncated = streamed_tail, False  # complete text recovered from the stream
    changed = sorted({path for call in calls.values() if call["kind"] in EDIT_KINDS and call["status"] == "completed"
                      for path in call["paths"]})
    return {
        "completion": completion, "outcome": outcome, "status": status, "stopReason": stop_reason,
        "error": error, "errorDetails": details if outcome in ("failed", "cancelled") else None,
        "report": report_text, "reportTruncated": truncated, "changedFiles": changed,
    }
