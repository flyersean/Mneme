"""Decision layer — client for System One / decision models (Jev, gpt-6-luna-decisions).

These models do NOT generate text. They answer *typed* questions about a
``state`` (structured JSON or text) and return calibrated probabilities:

    noul     yes/no probability (0.0–1.0)
    choice   one option from a set + a probability for every option
    score    probability-weighted position on an ordered rubric

Requests go to OpenRouter's Decisions API (``/api/alpha/decisions``), NOT the
chat-completions endpoint. Output tokens are free; input is billed. A decision
round-trips in ~130–160 ms.

This is the foundation of the "decision layer": the harness judge, the tool
safety gate, routing, and grading all become typed decisions instead of ad-hoc
LLM calls with PASS/FAIL regexes. The label model stays a *generative* LLM — a
fixed option set can't produce the open-vocabulary descriptive labels that make
Mneme retrieval work, so labeling is explicitly out of scope here.
"""

import json
import os
import urllib.error
import urllib.request

DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = "typesafe/jev-1.13"

# Threshold bands that turn a calibrated probability into a control-flow
# decision. ACCEPT = act as if yes, REJECT = act as if no, ESCALATE = borderline
# (hand off / ask / retry). Tune these against the cost of each mistake, not a
# round number — confidence is the distribution of alternatives, not safety.
ACCEPT_AT = 0.70
REJECT_AT = 0.30


def _api_key(explicit=None):
    key = explicit or os.environ.get("MNEME_DECISION_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise RuntimeError("no API key for the Decisions API "
                           "(set MNEME_DECISION_API_KEY or OPENROUTER_API_KEY)")
    return key


def decide(state, questions, model=DEFAULT_MODEL, api_key=None, timeout=30):
    """Evaluate typed ``questions`` against ``state`` in a single request.

    state:     dict or str — the unstructured context to judge.
    questions: {name: {"type": "noul"|"choice"|"score", "instructions": str,
                       "criteria": <see builders below>}}

    Returns the full response dict: {id, model, provider, answers, usage}.
    """
    payload = {"model": model, "state": state, "questions": questions}
    req = urllib.request.Request(
        DECISIONS_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + _api_key(api_key),
                 "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


# ── question builders ────────────────────────────────────────────────────────
def noul(instructions, true_when, false_when):
    """A yes/no question. `true_when` / `false_when` describe each answer."""
    return {"type": "noul", "instructions": instructions,
            "criteria": {"true": true_when, "false": false_when}}


def choice(instructions, options):
    """Pick one option from a set. `options` is {name: description}."""
    return {"type": "choice", "instructions": instructions, "criteria": options}


def score(instructions, levels):
    """Place the state on an ordered rubric. `levels` is lowest-first."""
    return {"type": "score", "instructions": instructions, "criteria": levels}


# ── answer parsing ───────────────────────────────────────────────────────────
def answers(result):
    return (result or {}).get("answers", {})


def noul_prob(result, name):
    a = answers(result).get(name) or {}
    return float(a.get("noul", 0.0))


def verdict(result, name, accept_at=ACCEPT_AT, reject_at=REJECT_AT):
    """Map a noul probability to ('accept'|'escalate'|'reject', probability)."""
    p = noul_prob(result, name)
    if p >= accept_at:
        return "accept", p
    if p <= reject_at:
        return "reject", p
    return "escalate", p


def choice_winner(result, name):
    a = answers(result).get(name) or {}
    return a.get("choice"), float(a.get("confidence", 0.0)), a.get("probabilities", {})


def score_value(result, name):
    a = answers(result).get(name) or {}
    return a.get("score"), float(a.get("confidence", 0.0)), a.get("legend", {})


class DecisionEngine:
    """Caching, threshold-aware wrapper around :func:`decide`."""

    def __init__(self, model=DEFAULT_MODEL, api_key=None, cache=None,
                 accept_at=ACCEPT_AT, reject_at=REJECT_AT):
        self.model = model
        self.api_key = api_key
        self.cache = cache if cache is not None else {}
        self.accept_at = accept_at
        self.reject_at = reject_at

    def decide(self, state, questions):
        key = json.dumps({"model": self.model, "state": state, "questions": questions},
                         sort_keys=True)
        if key in self.cache:
            return self.cache[key]
        result = decide(state, questions, model=self.model, api_key=self.api_key)
        self.cache[key] = result
        return result

    def ask(self, state, name, question):
        """Single-question convenience: return the parsed answer dict."""
        result = self.decide(state, {name: question})
        return answers(result).get(name)

    def noul_verdict(self, state, name, question):
        """Return ('accept'|'escalate'|'reject', probability) for one noul."""
        result = self.decide(state, {name: question})
        return verdict(result, name, self.accept_at, self.reject_at)
