"""Adapt AGY stream events to the existing Live dashboard event contract.

Original NDJSON stays on disk. Only the read model is normalized.
"""


def normalize(event):
    if not isinstance(event, dict):
        return event
    kind = event.get('event')
    if kind == 'init':
        return {'type': 'metadata', 'sessionId': event.get('conversation_id'), 'data': event.get('init', {})}
    if kind == 'result':
        result = event.get('result') or {}
        denied = result.get('denied_actions') or []
        text = result.get('response')
        ok = result.get('status') == 'SUCCESS' and isinstance(text, str) and bool(text.strip()) and not denied
        return {'type': 'runFinished' if ok else 'runError',
                'sessionId': result.get('conversation_id'),
                'data': {'status': 'success' if ok else 'failed', 'finalText': text,
                         'message': result.get('error') or ('Permission denied: ' + str(denied) if denied else 'AGY returned no report'),
                         'details': result}}
    if kind == 'step_update':
        step = event.get('step_update') or {}
        sid = step.get('conversation_id')
        if step.get('step_type') == 'agent_response' and isinstance(step.get('text_delta'), str):
            return {'sessionId': sid, 'update': {'sessionUpdate': 'agent_message_chunk',
                    'content': {'type': 'text', 'text': step['text_delta']}}}
        if step.get('step_type') == 'tool':
            info = step.get('tool_info') or {}
            name = step.get('tool_name') or info.get('name') or 'tool'
            params = info.get('parameters') or {}
            paths = [value for key, value in params.items()
                     if key in ('TargetFile', 'AbsolutePath', 'SearchPath', 'FilePath', 'path') and isinstance(value, str)]
            status = 'failed' if info.get('error') else ('completed' if step.get('state') == 'DONE' else 'in_progress')
            return {'sessionId': sid, 'update': {'sessionUpdate': 'tool_call_update',
                    'toolCallId': str(step.get('step_index')), 'title': name,
                    'kind': 'execute' if name == 'call_mcp_tool' and params.get('ServerName') == 'shared-executor' else 'edit' if name == 'replace_file_content' else 'search' if name == 'grep_search' else 'read',
                    'status': status, 'locations': [{'path': path} for path in paths], 'rawInput': params}}
        return {'type': 'metadata', 'sessionId': sid, 'data': step}
    return event
