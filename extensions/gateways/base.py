"""Gateway interface — every way a user reaches Mneme maps onto the same harness API.

    Telegram / CLI / future gateways
        │  receive() / send()
        ▼
    Gateway (authenticate → identify_user → authorize)
        │  HTTP only: POST /harness/command, POST /v1/chat/completions, GET /runs/<id>
        ▼
    Mneme proxy (harness)

The core never knows which gateway a message came from — gateways are
extensions and talk to the proxy over HTTP, like the swarm. Messages that start
with "/" are harness commands (/run, /status, /approve …); anything else goes to
the proxy as a normal chat turn (``plain_text="chat"``) or starts a run
(``plain_text="run"``). Runs started through a gateway are watched, and the user
is notified when one finishes or needs approval.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional

import requests

_RUN_ID = re.compile(r"\b(run_[0-9a-f]{10,})\b")
_NOTIFY_STATES = {"completed", "failed", "cancelled", "awaiting_approval"}


@dataclass
class Message:
    user_id: str
    text: str
    chat_id: str = ""
    raw: Dict = field(default_factory=dict)


class HarnessClient:
    """Minimal HTTP client for the proxy. `session` is injectable for tests."""

    def __init__(self, base_url: str = "http://localhost:8080", timeout: float = 600, session=None, token: Optional[str] = None):
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.http = session or requests.Session()
        # Authenticate to the proxy (now multi-user). MNEME_TOKEN is a user's API
        # token from `python3 proxy/mneme/auth.py adduser`; without it every call
        # 401s (and .json() on the plain-text body raised a confusing JSONDecodeError).
        self.token = token or os.environ.get("MNEME_TOKEN", "")
        if self.token:
            self.http.headers["Authorization"] = f"Bearer {self.token}"

    def command(self, text: str, actor: str) -> str:
        r = self.http.post(f"{self.base}/harness/command", json={"text": text, "actor": actor}, timeout=60)
        try:
            body = r.json() if r.content else {}
        except ValueError:
            body = {}
        if r.status_code == 200:
            return body.get("reply", "")
        if r.status_code == 401:
            return "unauthorized — set MNEME_TOKEN to a user's API token (from `adduser`)"
        return body.get("error") or f"HTTP {r.status_code}"

    def chat(self, text: str, history: Optional[List[dict]] = None) -> str:
        msgs = list(history or []) + [{"role": "user", "content": text}]
        r = self.http.post(f"{self.base}/v1/chat/completions", json={"model": "default", "messages": msgs},
                           timeout=self.timeout)
        if r.status_code != 200:
            if r.status_code == 401:
                raise RuntimeError("unauthorized — set MNEME_TOKEN to a user's API token (from `adduser`)")
            raise RuntimeError(f"chat failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json()["choices"][0]["message"]["content"]

    def run_status(self, run_id: str) -> Optional[dict]:
        r = self.http.get(f"{self.base}/runs/{run_id}", timeout=30)
        try:
            return r.json().get("run") if r.status_code == 200 else None
        except ValueError:
            return None


class Gateway:
    """Subclass and implement receive() and send(); override the auth hooks."""

    name = "gateway"

    def __init__(self, client: HarnessClient, plain_text: str = "chat", allowed_users: Iterable[str] = ()):
        if plain_text not in ("chat", "run"):
            raise ValueError("plain_text must be 'chat' or 'run'")
        self.client = client
        self.plain_text = plain_text
        self.allowed = {str(u) for u in allowed_users}
        self.watched: Dict[str, tuple] = {}      # run_id -> (message, last notified status)
        self.history: Dict[str, List[dict]] = {}

    # ── transport (implement) ────────────────────────────────────────────
    def receive(self) -> Iterable[Message]:
        raise NotImplementedError

    def send(self, msg: Message, text: str) -> None:
        raise NotImplementedError

    # ── identity / access (override as needed) ───────────────────────────
    def authenticate(self, msg: Message) -> bool:
        return True

    def identify_user(self, msg: Message) -> str:
        return f"{self.name}:{msg.user_id}"

    def authorize(self, user: str, msg: Message) -> bool:
        return not self.allowed or msg.user_id in self.allowed

    # ── routing ──────────────────────────────────────────────────────────
    def handle(self, msg: Message) -> Optional[str]:
        if not self.authenticate(msg):
            return "Not authenticated."
        user = self.identify_user(msg)
        if not self.authorize(user, msg):
            return "Not authorized for this Mneme."
        text = (msg.text or "").strip()
        if not text:
            return None
        if text.startswith("/"):
            reply = self.client.command(text, actor=user)
        elif self.plain_text == "run":
            reply = self.client.command("/run " + text, actor=user)
        else:
            hist = self.history.setdefault(user, [])
            reply = self.client.chat(text, hist[-12:])
            hist += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        for rid in _RUN_ID.findall(reply or "")[:1]:
            if text.startswith("/run ") or self.plain_text == "run":
                self.watched[rid] = (msg, "")
        return reply

    def poll_watched(self) -> None:
        for rid, (msg, last) in list(self.watched.items()):
            run = self.client.run_status(rid)
            if run is None:
                continue
            st = run["status"]
            if st in _NOTIFY_STATES and st != last:
                note = f"Run {rid[-8:]} {st}"
                if st == "completed" and run.get("result"):
                    note += ":\n" + run["result"][:1500]
                elif st == "failed":
                    note += f": {run.get('error', '')[:300]}"
                elif st == "awaiting_approval":
                    note += f" — reply /approve {rid[-8:]} or /reject {rid[-8:]}"
                self.send(msg, note)
                self.watched[rid] = (msg, st)
            if st in ("completed", "failed", "cancelled"):
                self.watched.pop(rid, None)

    def serve(self, poll_every: float = 5.0, stop: Optional[Callable[[], bool]] = None) -> None:
        last_poll = 0.0
        while not (stop and stop()):
            for msg in self.receive():
                try:
                    reply = self.handle(msg)
                except Exception as e:
                    reply = f"error: {type(e).__name__}: {e}"
                if reply:
                    self.send(msg, reply)
            if time.time() - last_poll >= poll_every:
                self.poll_watched()
                last_poll = time.time()
