"""Mneme proxy — modularized components.

The 4.9k-line monolith (``mneme_proxy.py``) is being decomposed into this
package via strangler-fig extraction. ``mneme_proxy.py`` stays the orchestrator
+ facade: it re-exports symbols from these modules so ``import mneme_proxy as
mp`` keeps working for existing callers (the offline tests, the Flask entry
point).

Modules:
  util        — shared helpers with no heavy dependencies (import-safe anywhere)
  tool_trail  — deterministic tool-outcome observation + the failure nudge
  auth        — gateway multi-user auth (mneme_users.yaml, token/password
                check, adduser CLI); imported by the gateway, not the proxy
"""
