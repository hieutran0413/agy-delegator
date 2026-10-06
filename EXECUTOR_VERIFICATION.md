# Shared executor verification — 2026-10-06

Implemented fixed workspace/action execution via MCP, without model calls.
Actions are registered by the coordinator; AGY cannot register commands through
MCP. Execution returns a run ID, compact status and a durable full output log.
Runtime/output limits and identity-checked cancellation are supported.

Evidence:
- 12 backend checks passed.
- Real AGY native run_command attempt was denied by PreToolUse; sentinel file
  /tmp/agy-direct-execution-should-not-exist was not created.
- Real AGY called shared-executor start_action and action_status successfully.
  Output: SHARED-EXECUTOR-OK. The captured conversation was resumed successfully.
- Coordinator-configured backend_tests ran through the installed MCP server:
  exit code 0, all 12 checks passed.
- Separate cancellation and runtime-limit probes returned cancelled/timed_out.
- Codex and AGY local installations include the shared-executor MCP service.

Scope: hook enforcement, not an OS sandbox. Registered commands may execute
project code and must be authorized before registration. Existing native AGY
execution grants do not override hook deny. No claim of zero total account usage:
AGY inference and Codex setup/review still use their respective model budgets.

Configuration:
- ~/.local/share/agy-executor/policy.json
- ~/.gemini/config/hooks.json (shared-execution-gate)
- ~/.gemini/antigravity-cli/settings.json (three exact MCP grants)
- ~/.local/share/agy-executor/runs/ (durable full logs)
