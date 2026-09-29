#!/usr/bin/env python3
"""Telegram gateway — control Mneme runs and chat from Telegram (Bot API long polling).

    export MNEME_TELEGRAM_TOKEN=123:abc            # from @BotFather
    export MNEME_TELEGRAM_ALLOWED=11111111,2222    # numeric Telegram user ids allowed (REQUIRED)
    python3 extensions/gateways/telegram.py [--url http://localhost:8080] [--plain run|chat]

Security: Mneme runs execute tools (bash/write) with the proxy's privileges, so
the gateway refuses to start without an allow-list, only answers private chats,
and ignores everyone not on the list. Commands are the harness /commands
(/run, /status, /approve …); runs you start are watched and you get a message
when they finish or need approval.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import Gateway, HarnessClient, Message  # noqa: E402

import requests  # noqa: E402

_MAX = 4000  # Telegram hard limit is 4096 chars per message


class TelegramGateway(Gateway):
    name = "telegram"

    def __init__(self, client, token, allowed_users, plain_text="chat", session=None, poll_timeout=25):
        if not allowed_users:
            raise SystemExit("MNEME_TELEGRAM_ALLOWED is required (comma-separated Telegram user ids)")
        super().__init__(client, plain_text, allowed_users)
        self.api = f"https://api.telegram.org/bot{token}"
        self.http = session or requests.Session()
        self.offset = 0
        self.poll_timeout = poll_timeout

    def receive(self):
        try:
            r = self.http.get(f"{self.api}/getUpdates",
                              params={"offset": self.offset, "timeout": self.poll_timeout},
                              timeout=self.poll_timeout + 10)
            updates = r.json().get("result", []) if r.status_code == 200 else []
        except requests.RequestException:
            return []
        out = []
        for u in updates:
            self.offset = max(self.offset, int(u.get("update_id", 0)) + 1)
            m = u.get("message") or {}
            if not m.get("text"):
                continue
            out.append(Message(user_id=str((m.get("from") or {}).get("id", "")), text=m["text"],
                               chat_id=str((m.get("chat") or {}).get("id", "")), raw=m))
        return out

    def authenticate(self, msg):
        return (msg.raw.get("chat") or {}).get("type") == "private"

    def send(self, msg, text):
        for i in range(0, max(len(text), 1), _MAX):
            try:
                self.http.post(f"{self.api}/sendMessage",
                               json={"chat_id": msg.chat_id, "text": text[i:i + _MAX]}, timeout=30)
            except requests.RequestException:
                pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("MNEME_URL", "http://localhost:8080"))
    ap.add_argument("--plain", choices=("chat", "run"), default="chat")
    a = ap.parse_args()
    token = os.environ.get("MNEME_TELEGRAM_TOKEN") or sys.exit("set MNEME_TELEGRAM_TOKEN")
    allowed = [x.strip() for x in os.environ.get("MNEME_TELEGRAM_ALLOWED", "").split(",") if x.strip()]
    TelegramGateway(HarnessClient(a.url), token, allowed, plain_text=a.plain).serve(poll_every=5)


if __name__ == "__main__":
    main()
