"""Tests for the general-purpose secret store (mneme.secrets_store)."""
import os
import tempfile

import pytest

from mneme import secrets_store


@pytest.fixture()
def path(tmp_path):
    return str(tmp_path / "secrets.yaml")


def test_roundtrip(path):
    assert secrets_store.set_secret("webhook-key", "abc123", path) is True
    assert secrets_store.load_secrets(path) == {"webhook-key": "abc123"}


def test_missing_file_is_empty(path):
    assert secrets_store.load_secrets(path) == {}


def test_invalid_name_rejected(path):
    assert secrets_store.set_secret("bad name!", "x", path) is False
    assert secrets_store.set_secret("../escape", "x", path) is False
    assert secrets_store.set_secret("", "x", path) is False
    assert secrets_store.load_secrets(path) == {}


def test_delete(path):
    secrets_store.set_secret("a", "1", path)
    secrets_store.set_secret("b", "2", path)
    assert secrets_store.delete_secret("a", path) is True
    assert secrets_store.delete_secret("a", path) is False  # already gone
    assert secrets_store.load_secrets(path) == {"b": "2"}


def test_resolve_expands_and_preserves_unknown(path):
    secrets_store.set_secret("tok", "shh", path)
    s = secrets_store.load_secrets(path)
    assert secrets_store.resolve_secrets("X=${secret:tok}", s) == "X=shh"
    # unknown names are left in place so a typo surfaces
    assert secrets_store.resolve_secrets("${secret:nope}", s) == "${secret:nope}"
    # non-strings pass through
    assert secrets_store.resolve_secrets(123, s) == 123


def test_resolve_mcp_config(path):
    secrets_store.set_secret("gh", "ghp_xyz", path)
    cfg = secrets_store.resolve_mcp_config([{
        "name": "s",
        "command": "npx",
        "env": {"GITHUB_TOKEN": "${secret:gh}", "PLAIN": "keep"},
        "args": ["-y", "--token=${secret:gh}"],
    }], path)
    assert cfg[0]["env"]["GITHUB_TOKEN"] == "ghp_xyz"
    assert cfg[0]["env"]["PLAIN"] == "keep"
    assert cfg[0]["args"][1] == "--token=ghp_xyz"


def test_file_is_0600(path):
    secrets_store.set_secret("k", "v", path)
    assert (os.stat(path).st_mode & 0o777) == 0o600
