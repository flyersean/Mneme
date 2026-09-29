# Gateways — reach Mneme from a terminal, Telegram, or anything else

A gateway maps an interface onto the **same harness API**. Mneme's core never
knows where a message came from. Like the swarm, gateways are extensions: they
talk to a proxy **over HTTP only**, using three endpoints:

- `POST /harness/command` for commands;
- `POST /v1/chat/completions` for chat;
- `GET /runs/<id>` for watching runs.

```
Telegram / CLI / …  →  Gateway (authenticate → identify_user → authorize)  →  HTTP  →  Mneme harness
```

- Lines starting with `/` are harness commands (`/help`, `/run <goal>`,
  `/status`, `/approve <run>`, …).
- Other text is a chat turn (`--plain chat`, the default), or starts a new run
  (`--plain run`).
- Runs you start from a gateway are **watched**. You get a message when a run
  completes, fails, or is waiting for your approval.

## CLI

```bash
python3 extensions/gateways/cli.py --url http://localhost:8080
python3 extensions/gateways/cli.py --once "/runs"
```

## Telegram

```bash
export MNEME_TELEGRAM_TOKEN=123456:ABC...        # from @BotFather
export MNEME_TELEGRAM_ALLOWED=11111111           # your numeric Telegram user id(s) — REQUIRED
python3 extensions/gateways/telegram.py --url http://localhost:8080 --plain run
```

Runs can execute `bash` and `write` with the proxy's privileges, so the Telegram
gateway has three safeguards:

- it refuses to start without an allow-list;
- it answers private chats only;
- it ignores every user who is not on the list.

## Writing a new gateway

Subclass `base.Gateway` and implement two methods:

- `receive()`: yield `Message` objects;
- `send(msg, text)`: deliver a reply.

Optionally override `authenticate`, `identify_user` and `authorize`. Then call
`serve()`. The gateway identifies the user as `<gateway>:<user_id>`, and that
string is recorded as the actor on everything they do. Dependency: `requests`.
