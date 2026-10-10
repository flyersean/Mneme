"""General-purpose secret store for Mneme.

Provider API keys live in the env file (see ``mneme_proxy._persist_env_key``);
EVERYTHING ELSE — webhook keys, MCP server tokens, third-party service
credentials — lives here, in a chmod-600 YAML file next to the env file. Values
are never logged. Config consumers reference a secret by name with the
``${secret:NAME}`` placeholder (see :func:`resolve_secrets`).
"""
import os
import re

# A secret name is a flat, filesystem-safe identifier. Keep it restrictive so a
# name can't smuggle YAML structure or path tricks.
_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_REF_RE = re.compile(r"\$\{secret:([A-Za-z0-9_.-]+)\}")


def secrets_file_path() -> str:
    """Path to the secrets file: ``<mneme-root>/secrets.yaml``, the same
    directory that holds the env file (``<mneme-root>/env``). Mirrors the
    directory logic of ``mneme_proxy._env_file_path()`` so both files always
    land side by side."""
    db_path = os.environ.get("MNEME_DB_PATH")
    if not db_path:
        db_path = os.path.join(
            os.environ.get("MNEME_CHUNK_DIR", "/workspace/mneme_chunks"), "mneme.db")
    db_dir = os.path.dirname(os.path.expanduser(db_path)) or "."
    return os.path.join(os.path.dirname(db_dir), "secrets.yaml")


def _valid_name(name: str) -> bool:
    return bool(name) and bool(_NAME_RE.match(name))


def load_secrets(path: str = None) -> dict:
    """Return ``{name: value}``. A missing/corrupt file returns ``{}``."""
    path = path or secrets_file_path()
    if not os.path.isfile(path):
        return {}
    try:
        import yaml
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        if not isinstance(data, dict):
            return {}
        return {str(k): (v if isinstance(v, str) else str(v)) for k, v in data.items()}
    except Exception:
        return {}


def save_secrets(secrets: dict, path: str = None) -> None:
    """Atomically write ``{name: value}`` to the secrets file, chmod 600."""
    path = path or secrets_file_path()
    import yaml
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            {str(k): str(v) for k, v in (secrets or {}).items()},
            f, default_flow_style=False, allow_unicode=True)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def set_secret(name: str, value: str, path: str = None) -> bool:
    """Add/update a secret. Returns False on an invalid name."""
    if not _valid_name(name):
        return False
    s = load_secrets(path)
    s[name] = value
    save_secrets(s, path)
    return True


def delete_secret(name: str, path: str = None) -> bool:
    """Delete a secret. Returns False if it wasn't present."""
    s = load_secrets(path)
    if name in s:
        del s[name]
        save_secrets(s, path)
        return True
    return False


def resolve_secrets(value, secrets: dict = None):
    """Expand ``${secret:NAME}`` references in a string (or pass through any
    non-string). Unresolved names are left as-is so a typo surfaces instead of
    silently becoming an empty string."""
    if not isinstance(value, str):
        return value
    if secrets is None:
        secrets = load_secrets()
    return _REF_RE.sub(lambda m: secrets.get(m.group(1), m.group(0)), value)


def resolve_mcp_config(servers: list, path: str = None) -> list:
    """Resolve ``${secret:NAME}`` in each MCP server's ``env`` values and
    ``args``, returning a new list (the input list is left untouched)."""
    secrets = load_secrets(path)
    out = []
    for s in (servers or []):
        if not isinstance(s, dict):
            out.append(s)
            continue
        c = dict(s)
        env = c.get("env")
        if isinstance(env, dict):
            c["env"] = {k: resolve_secrets(v, secrets) for k, v in env.items()}
        args = c.get("args")
        if isinstance(args, list):
            c["args"] = [resolve_secrets(a, secrets) for a in args]
        out.append(c)
    return out
