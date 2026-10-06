#!/usr/bin/env python3
"""Small stdio MCP adapter. All process execution belongs to Codex tools.

Protocol output on stdout is reserved for JSON-RPC responses; anything a
callee prints is captured and discarded so the stream stays clean.
"""
import argparse
import contextlib
import io
import json
import re
import subprocess
import sys
import agy_live as live


def schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}

STR = {"type": "string"}
SECONDS = {"type": "integer", "minimum": 1, "maximum": live.MAX_LIMIT_SECONDS}
FILES = {"type": "array", "items": {"type": "string"}, "maxItems": 200,
         "description": "Implement mode only: workspace-relative files AGY may write. Omit to request workspace edits; scope is prompt-based, not OS enforcement."}
TOOLS = [
    {"name": "list_models", "description": "List available AGY models; does not launch a worker.", "inputSchema": schema({})},
    {"name": "start_job", "description": "Launch one bounded file-only task in a brand-new, unrelated AGY session (never resumes any existing session; use continue_job to keep the conversation). Launch is not completion: keep calling wait_job until done=true, then review the result before ending the response. AGY cannot execute terminal commands. File scope is an instruction boundary, not an OS sandbox. A Codex-owned supervisor enforces max runtime and idle timeout. Never opens a browser.", "inputSchema": schema({"workspace": STR, "mode": {"type": "string", "enum": ["review", "implement"]}, "model": STR, "effort": {"type": "string", "enum": ["low", "medium", "high"]}, "brief": STR, "files": FILES, "max_runtime_seconds": SECONDS, "idle_timeout_seconds": SECONDS}, ["workspace", "mode", "model", "effort", "brief"])},
    {"name": "continue_job", "description": "Send a new message to the SAME AGY session as a finished turn (e.g. command output/test results Codex ran, or follow-up instructions). Starts a new turn with exactly --conversation of that turn's captured sessionId, same mode/model/effort/limits and identical write scope, still code-only. Returns the new turn's jobId; wait_job on it. Fails with an explicit blocker (and starts nothing) if the session id is unknown or unavailable, if the turn is still running, or if it was already continued. request_id makes retries idempotent.", "inputSchema": schema({"job_id": STR, "message": {"type": "string", "minLength": 1, "maxLength": live.MAX_CONTINUE_MESSAGE}, "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{1,80}$"}}, ["job_id", "message", "request_id"])},
    {"name": "task_history", "description": "Ordered turns of the logical task (same rootTaskId) containing job_id: per turn jobId, turn, sessionId, state, the message sent, the complete raw report and reportPath. Paginate with offset/limit (nextOffset is null on the last page).", "inputSchema": schema({"job_id": STR, "offset": {"type": "integer", "minimum": 0, "maximum": 100000}, "limit": {"type": "integer", "minimum": 1, "maximum": live.MAX_HISTORY_PAGE}}, ["job_id"])},
    {"name": "job_status", "description": "Get compact status, report and error for one job. 'done' is true only for a durable terminal result; 'processAlive' reports the actual worker process. Includes sessionId, rootTaskId and turn.", "inputSchema": schema({"job_id": STR}, ["job_id"])},
    {"name": "wait_job", "description": "Wait up to 50 seconds for a durable terminal result. If nextAction=wait_superseding_job, the session was continued in a newer turn: wait on followJobId. If done=false or steerPending=true, continue waiting in this same turn; waitTimedOut only ends this tool call, never the job. Review report and verify edits before a final response.", "inputSchema": schema({"job_id": STR, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 50}}, ["job_id"])},
    {"name": "stop_job", "description": "Cancel only the identified AGY worker after the user requests it: SIGTERM then SIGKILL on its own verified process group, with exit confirmation. Leaves file edits and the AGY session intact.", "inputSchema": schema({"job_id": STR}, ["job_id"])},
    {"name": "ack_job", "description": "Mark that a finished turn's report was consumed. Only marks it: reports, job records and the AGY session are kept so the task can still be continued.", "inputSchema": schema({"job_id": STR}, ["job_id"])},
]


def _valid(field, value):
    kind = field.get("type")
    if kind == "string":
        return (isinstance(value, str) and ("enum" not in field or value in field["enum"])
                and field.get("minLength", 0) <= len(value) <= field.get("maxLength", len(value))
                and ("pattern" not in field or re.fullmatch(field["pattern"], value) is not None))
    if kind == "integer":
        return (isinstance(value, int) and not isinstance(value, bool)
                and field.get("minimum", value) <= value <= field.get("maximum", value))
    if kind == "array":
        return (isinstance(value, list) and len(value) <= field.get("maxItems", len(value))
                and all(_valid(field["items"], item) for item in value))
    return False


def call(name, args):
    definition = next((t for t in TOOLS if t["name"] == name), None)
    if not definition:
        raise ValueError("Unknown tool")
    spec = definition["inputSchema"]
    if not isinstance(args, dict) or set(args) - set(spec["properties"]) or set(spec["required"]) - set(args):
        raise ValueError("Invalid tool arguments")
    for key, value in args.items():
        if not _valid(spec["properties"][key], value):
            raise ValueError("Invalid argument: " + key)
    if name == "list_models":
        result = subprocess.run(["agy", "models"], capture_output=True, text=True, check=True, timeout=30)
        return {"models": result.stdout.splitlines()}
    with contextlib.redirect_stdout(io.StringIO()):
        if name == "start_job":
            return live.launch(argparse.Namespace(
                workspace=args["workspace"], mode=args["mode"], model=args["model"], effort=args["effort"],
                prompt=args["brief"], prompt_file=None, port=live.PORT, files=args.get("files"),
                max_runtime=args.get("max_runtime_seconds"), idle_timeout=args.get("idle_timeout_seconds"),
            ))
        if name == "job_status":
            return live.snapshot(args["job_id"], include_prompt=False)
        if name == "continue_job":
            return live.continue_job(args["job_id"], args["message"], args["request_id"], port=live.PORT)
        if name == "task_history":
            return live.task_history(args["job_id"], args.get("offset", 0), args.get("limit", live.HISTORY_PAGE))
        if name == "wait_job":
            return live.wait_job(args["job_id"], args.get("timeout_seconds", 50))
        if name == "stop_job":
            return live.stop_job(args["job_id"])
        return live.acknowledge_job(args["job_id"])


def dispatch(request):
    method = request.get("method")
    if method == "initialize":
        return {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "agy-delegator", "version": "0.2.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": TOOLS}
    if method == "tools/call":
        params = request.get("params", {})
        try:
            value = call(params["name"], params.get("arguments", {}))
            return {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}], "isError": False}
        except Exception as error:
            return {"content": [{"type": "text", "text": str(error)}], "isError": True}
    raise ValueError("Method not found")


def main():
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": dispatch(request)}
        except Exception as error:
            response = {"jsonrpc": "2.0", "id": request.get("id") if isinstance(request, dict) else None, "error": {"code": -32603, "message": str(error)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
