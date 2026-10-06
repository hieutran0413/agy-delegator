#!/usr/bin/env python3
"""Block AGY's direct execution tools; preserve file edits in project scope."""
import json,sys
from pathlib import Path
EXEC={'run_command','command_status','send_command_input','run_workflow','notebook_execution','invoke_subagent','define_subagent','manage_subagents','send_message','schedule','open_browser_url','execute_browser_javascript'}
SAFE_SERVERS={'shared-executor','figma','eagle','eagle-mcp'}

def decide(payload):
    call=payload.get('toolCall') or {};name=call.get('name','');args=call.get('args') or {}
    if name in EXEC or name.startswith('browser_'):
        return {'decision':'deny','reason':'Use shared-executor MCP for coordinator-configured actions. Do not execute directly or delegate execution.'}
    if name=='call_mcp_tool':
        server=next((args[k] for k in ('ServerName','serverName','server_name','server') if isinstance(args.get(k),str)),None)
        if server not in SAFE_SERVERS:return {'decision':'deny','reason':'This MCP server is outside the approved execution bridge.'}
    if name in {'write_to_file','replace_file_content','multi_replace_file_content','notebook_edit'}:
        target=args.get('TargetFile') or args.get('target_file') or args.get('FilePath')
        if not target:return {'decision':'deny','reason':'Cannot confirm write target'}
        p=Path(target).expanduser().resolve();home=Path.home()
        roots=[home/'.codex',home/'.gemini/config',home/'.gemini/antigravity-cli',home/'plugins',home/'.local/share/agy-executor']
        if any(p==r or r in p.parents for r in roots) or '.git' in p.parts or p.name=='hooks.json':
            return {'decision':'deny','reason':'Execution policy and tool configuration are coordinator-owned.'}
    # Fall through to normal permissions, never blanket auto-approve.
    return {'decision':'ask','reason':'Use ordinary AGY permissions for file and approved MCP operations.'}
if __name__=='__main__':
    try:print(json.dumps(decide(json.load(sys.stdin))))
    except Exception:print(json.dumps({'decision':'deny','reason':'Execution gate could not validate the request'}))
