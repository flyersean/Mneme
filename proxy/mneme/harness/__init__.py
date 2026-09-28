"""Mneme agent harness — durable runs on top of the Mneme proxy.

The harness owns execution state; the model is one component it calls. This
package is standalone (stdlib only) and never imports ``mneme_proxy``: the proxy
binds it at startup, the same way it binds ``mneme.capability`` and
``mneme.tools``. That keeps it unit-testable against a temp directory.

    ledger.py         durable Run / Task / Step / ToolCall / Event / Artifact / Checkpoint store
    engine.py         executes runs: steps, checkpoints, budgets, pause/resume/cancel/retry, recovery
    workspace.py      per-run working directory abstraction
    chat_executor.py  adapter that runs a task step through the proxy's process_chat
    http.py           /runs HTTP API (registered on the proxy's Flask app)

See docs/harness/ for the audit and architecture decisions.
"""

from mneme.harness.ledger import Ledger, LedgerError, InvalidTransition, RUN_STATES, TERMINAL_STATES
from mneme.harness.engine import RunEngine, StepResult, StepContext, BudgetExceeded
from mneme.harness.workspace import RunWorkspace

__all__ = [
    "Ledger", "LedgerError", "InvalidTransition", "RUN_STATES", "TERMINAL_STATES",
    "RunEngine", "StepResult", "StepContext", "BudgetExceeded",
    "RunWorkspace",
]
