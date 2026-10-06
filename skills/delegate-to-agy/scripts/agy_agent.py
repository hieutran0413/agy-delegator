"""AGY profile generation and advisory file-scope validation.
Tool isolation for primary AGY agents is not independently confirmed.
"""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path

AGENT_PREFIX = "codex_agy_files_"
READ_TOOLS = ["view_file", "grep_search"]
WRITE_TOOLS = READ_TOOLS + ["replace_file_content"]
TRUSTED_TOOLS = READ_TOOLS  # fs_write is never blanket-trusted
PROTECTED_DIRS = (".git", ".agy", ".amazonq")
SECRET_DIRS = (".ssh", ".aws", ".gnupg")
SECRET_NAMES = (
    ".env", ".env.*", "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".netrc", ".npmrc", ".pypirc", "credentials", "credentials.*", "*.keychain", "*.keychain-db",
)


class PolicyError(RuntimeError):
    """The launch cannot be restricted as required; fail closed."""


def agents_dir() -> Path:
    return Path.home() / ".gemini" / "config" / "agents"


def agent_name(mode: str, job_id: str) -> str:
    return AGENT_PREFIX + mode + "_" + job_id.replace("-", "_")


def _inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def is_protected(relative: Path) -> bool:
    parts = relative.parts
    if any(part in PROTECTED_DIRS or part in SECRET_DIRS for part in parts):
        return True
    return bool(parts) and any(fnmatch.fnmatch(parts[-1], pattern) for pattern in SECRET_NAMES)


def secret_paths(workspace: Path) -> list[str]:
    root, home = str(workspace), str(Path.home())
    output = []
    for name in SECRET_DIRS:
        output += [f"{root}/{name}/**", f"{root}/**/{name}/**", f"{home}/{name}/**"]
    for name in SECRET_NAMES:
        output += [f"{root}/{name}", f"{root}/**/{name}"]
    return output


def denied_write_paths(workspace: Path) -> list[str]:
    root, home = str(workspace), str(Path.home())
    output = secret_paths(workspace)
    for name in PROTECTED_DIRS:
        output += [f"{root}/{name}", f"{root}/{name}/**", f"{root}/**/{name}", f"{root}/**/{name}/**"]
    output += [f"{home}/.agy/**", f"{home}/.aws/**"]
    return sorted(set(output))


def escaping_symlinks(workspace: Path, limit: int | None = None) -> list[Path]:
    """Find symlinks whose target leaves the workspace or enters a protected path."""
    limit = limit or int(os.environ.get("AGY_LIVE_SYMLINK_SCAN_LIMIT", "50000"))
    found: list[Path] = []
    seen = 0
    for root, directories, files in os.walk(workspace, followlinks=False):
        base = Path(root)
        directories[:] = [name for name in directories if name != ".git"]  # denied wholesale
        for name in [*directories, *files]:
            seen += 1
            if seen > limit:
                raise PolicyError(
                    "Workspace has more than " + str(limit) + " entries, so its write scope cannot be audited "
                    "for escaping symlinks; pass explicit implement files instead"
                )
            path = base / name
            if not path.is_symlink():
                continue
            target = Path(os.path.realpath(path))
            if not _inside(target, workspace) or is_protected(target.relative_to(workspace)):
                found.append(path)
    return found


def validate_file(workspace: Path, raw: object) -> Path:
    """Resolve one delegated writable file; reject escapes, symlinks and protected paths."""
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise PolicyError("Invalid delegated file path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / candidate
    normalized = Path(os.path.normpath(candidate))
    if normalized == workspace or not _inside(normalized, workspace):
        raise PolicyError("Delegated file is outside the workspace: " + raw)
    if Path(os.path.realpath(normalized)) != normalized:
        raise PolicyError("Delegated file path traverses a symlink: " + raw)
    if is_protected(normalized.relative_to(workspace)):
        raise PolicyError("Delegated file is a protected Git/AGY/secret path: " + raw)
    if normalized.is_dir():
        raise PolicyError("Delegated path is a directory, not a file: " + raw)
    return normalized


def write_policy(workspace: Path, mode: str, files: list[str] | None = None) -> dict:
    hook_roots = [Path.home() / ".agy" / "hooks"] + [base / ".agy" / "hooks" for base in [workspace, *workspace.parents]]
    for hook_root in hook_roots:
        if hook_root.is_dir() and any(hook_root.iterdir()):
            raise PolicyError("Ambient AGY hooks may execute commands; disable or isolate them before delegation: " + str(hook_root))
    read_denied = sorted(set(secret_paths(workspace)))
    if mode == "review":
        if files:
            raise PolicyError("Review mode is read-only; writable files are only valid in implement mode")
        return {"tools": list(READ_TOOLS), "allowedPaths": [], "deniedPaths": [], "readDeniedPaths": read_denied}
    if mode != "implement":
        raise PolicyError("Unknown mode: " + str(mode))
    denied = denied_write_paths(workspace)
    if files:
        allowed = sorted({str(validate_file(workspace, item)) for item in files})
    else:
        for link in escaping_symlinks(workspace):
            denied += [str(link), str(link) + "/**"]
        allowed = [str(workspace), str(workspace) + "/**"]
    return {"tools": list(WRITE_TOOLS), "allowedPaths": allowed, "deniedPaths": sorted(set(denied)),
            "readDeniedPaths": read_denied}


def agent_config(name: str, mode: str, policy: dict, prompt: str) -> dict:
    tools = ['view_file', 'grep_search']
    if mode == 'implement': tools.append('replace_file_content')
    return {'name': name, 'description': 'File-only AGY worker', 'tools': tools,
            'mainAgent': True, 'subagent': False, 'commandExecutionPolicy': 'off',
            'mcpServers': [], 'skills': [], 'plugins': [], 'prompt': prompt}


def shadowing_configs(workspace: Path, name: str) -> list[Path]:
    return [p for base in [workspace, *workspace.parents]
            for p in [base / '.agents/agents' / (name + '.md'), base / '.agents/agents' / name / 'agent.md']
            if p.exists()]


def install_agent(name: str, config: dict) -> Path:
    directory = agents_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (name + '.md')
    metadata = {k:v for k,v in config.items() if k != 'prompt'}
    # JSON is a YAML subset, avoiding an external runtime dependency.
    data = '---\n' + '\n'.join(k + ': ' + json.dumps(v) for k,v in metadata.items()) + '\n---\n' + config['prompt'] + '\n'
    if path.exists():
        if path.read_text() != data:
            old = path.read_text().split('---\n', 2)
            try:
                equivalent = len(old) == 3 and json.loads(old[1]) == metadata and old[2] == config['prompt'] + '\n'
            except ValueError:
                equivalent = False
            if not equivalent: raise PolicyError('Existing agent differs; refusing to overwrite')
        return path
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'w') as stream: stream.write(data)
    return path


def remove_agent(path_text: str | None, name: str | None) -> None:
    # Keep the original profile for exact conversation continuation.
    pass
