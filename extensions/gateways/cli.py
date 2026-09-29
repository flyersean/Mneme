#!/usr/bin/env python3
"""CLI gateway — talk to a Mneme proxy (chat + harness commands) from a terminal.

    python3 extensions/gateways/cli.py [--url http://localhost:8080] [--plain run|chat]
    python3 extensions/gateways/cli.py --once "/status last"      # one command, then exit

Lines starting with "/" are harness commands (/help lists them). Other lines are
chat turns (default) or new runs (--plain run).
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from base import Gateway, HarnessClient, Message  # noqa: E402


class CLIGateway(Gateway):
    name = "cli"

    def __init__(self, client, plain_text="chat", stream=None, out=None):
        super().__init__(client, plain_text)
        self.stream = stream or sys.stdin
        self.out = out or sys.stdout
        self.done = False

    def receive(self):
        self.out.write("mneme> ")
        self.out.flush()
        line = self.stream.readline()
        if not line or line.strip() in ("/quit", "/exit"):
            self.done = True
            return []
        return [Message(user_id=os.environ.get("USER", "local"), text=line.rstrip("\n"))]

    def send(self, msg, text):
        self.out.write(text.rstrip() + "\n")
        self.out.flush()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("MNEME_URL", "http://localhost:8080"))
    ap.add_argument("--plain", choices=("chat", "run"), default="chat")
    ap.add_argument("--once", help="run one command/message and exit")
    a = ap.parse_args()
    gw = CLIGateway(HarnessClient(a.url), plain_text=a.plain)
    if a.once:
        print(gw.handle(Message(user_id=os.environ.get("USER", "local"), text=a.once)) or "")
        return
    gw.serve(poll_every=5, stop=lambda: gw.done)


if __name__ == "__main__":
    main()
