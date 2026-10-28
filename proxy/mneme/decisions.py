"""Decision layer — client for System One / decision models (Jev, gpt-6-luna-decisions).

Two engines, configured via the ``decision:`` config section (surfaced as env):

    engine=jev   OpenRouter Decisions API (``/api/alpha/decisions``) — dedicated
                 classifier models that do NOT generate text and return calibrated
                 probabilities. Default model ``typesafe/jev-1.13``.
    engine=llm   Prompt a *generative* model (chat / label / utility / explicit) for
                 a yes/no + confidence, mapped to the same probability shape.

Fallback when the primary engine is unavailable: ``fallback=chat`` degrades to the
chat model as judge; ``fallback=skip`` fails open. Both fall through to fail-open if
even the fallback is unavailable.

The judge interface is engine-agnostic: ``decide()`` / ``llm_decide()`` both return a
result dict shaped ``{answers: {name: {noul: p, ...}}}`` so callers (harness judges,
tool gate, routing) never know which engine produced the verdict.
"""

import json
import os
import re
import urllib.error
import urllib.request

DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
DEFAULT_MODEL = os.environ.get("MNEME_DECISION_MODEL", "typesafe/jev-1.13")

# Engine + fallback knobs (set from the `decision:` config section via _CONFIG_ENV_MAP).
DECISION_ENGINE = os.environ.get("MNEME_DECISION_ENGINE", "jev")      # "jev" | "llm"
DECISION_LLM = os.environ.get("MNEME_DECISION_LLM", "")               # "" = chat, "label"|"utility"|explicit
DECISION_FALLBACK = os.environ.get("MNEME_DECISION_FALLBACK", "chat")  # "chat" | "skip"

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


def decide(state, questions, model=None, api_key=None, timeout=30):
    """Evaluate typed ``questions`` against ``state`` via the OpenRouter Decisions API.

    state:     dict or str — the unstructured context to judge.
    questions: {name: {"type": "noul"|"choice"|"score", "instructions": str,
                       "criteria": <see builders below>}}

    Returns the full response dict: {id, model, provider, answers, usage}.
    """
    payload = {"model": model or DEFAULT_MODEL, "state": state, "questions": questions}
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


# ── LLM engine (prompt a generative model instead of a classifier) ───────────
_LLM_PROMPT = (
    "You are a strict verification judge. Reply with EXACTLY one line: 'YES' or "
    "'NO', then a space, then a confidence digit from 1 to 5 (1 = guessing, "
    "5 = certain). Example: 'YES 4'. Do not explain.\n\n"
    "Question: {instructions}\n\n"
    "Context:\n{state}\n\n"
    "Answer YES if: {true_when}\n"
    "Answer NO if: {false_when}\n"
)


def _fmt_state(state):
    if isinstance(state, dict):
        return "\n".join(f"{k}: {v}" for k, v in state.items())
    return str(state or "")


def _llm_noul(state, question, llm_call):
    crit = (question or {}).get("criteria", {}) or {}
    prompt = _LLM_PROMPT.format(
        instructions=(question or {}).get("instructions", ""),
        state=_fmt_state(state),
        true_when=crit.get("true", ""),
        false_when=crit.get("false", ""),
    )
    reply = (llm_call([{"role": "user", "content": prompt}]) or "").strip()
    if not reply:
        raise RuntimeError("empty LLM judge reply")
    first = reply.split()[0].lower().strip(".,:;!?") if reply.split() else ""
    is_yes = first in ("yes", "true", "affirmative", "y", "correct")
    m = re.search(r"([1-5])", reply)
    conf = int(m.group(1)) if m else 3
    strength = (conf - 1) / 4.0              # 0.0 (guessing) .. 1.0 (certain)
    p = 0.5 + 0.5 * strength * (1 if is_yes else -1)
    return max(0.0, min(1.0, p))


def llm_decide(state, questions, llm_call, model_name=""):
    """Evaluate noul questions against ``state`` using a generative model.

    llm_call: callable(messages: list) -> str (the model's text reply).
    Returns the same shape as :func:`decide` (``answers`` keyed by question name),
    so callers can't tell which engine produced the verdict. choice/score are
    approximated as a noul (the calibrated classifier path is the accurate one).
    """
    out = {}
    for name, q in (questions or {}).items():
        q = q or {}
        p = _llm_noul(state, q, llm_call)
        out[name] = {"type": q.get("type", "noul"), "noul": p,
                     "confidence": abs(p - 0.5) * 2}
    return {"model": model_name or "llm", "provider": "llm", "answers": out}


class DecisionEngine:
    """Caching, threshold-aware wrapper around :func:`decide`."""

    def __init__(self, model=None, api_key=None, cache=None,
                 accept_at=ACCEPT_AT, reject_at=REJECT_AT):
        self.model = model or DEFAULT_MODEL
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
