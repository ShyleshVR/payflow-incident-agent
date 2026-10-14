"""Checking a diagnosis against the evidence it cites.

The model must name a cause from the runbook's list and back it with citations: a check that was
run, and a value or phrase quoted from that check's result. Code confirms each quote is really
there. A model can still draw a wrong conclusion from true facts; what this rules out is a
conclusion built on facts that no tool returned.

A quote is looked for in the text the model was shown for that check (the result as the prompt
prints it, or "This check failed: ..." for a check that could not be run). It holds when it is
there as a whole, or when each of its parts is: a small model often joins several fields of a
result into one "quote" (instances_up: 2, server_error_percent: 0.0), or copies an object and
leaves fields out. Every part must still be in the result, word for word; only the joining is
forgiven.
"""
import re

from agent import prompts, runbooks

# Shorter than this, a "quote" matches almost anything ("0", "up").
MIN_QUOTE_CHARS = 3
# JSON's own punctuation, and the backslash it puts before a quote inside a value: a log line
# that says role "x" is written role \"x\" in the result's text, and must still match.
_PUNCTUATION = re.compile(r'[\s"{}\[\],:\\]+')
# Per tool, the fields of its result that can carry text from outside PayFlow's operators: what a
# service logged (a log line can quote a request), the cause of an error, and what a partner
# answered, kept as a saga's error or step detail. Everything else in a result (counts, states,
# names, the tools' own "concerns", Kubernetes' messages) is written by the platform or the tools.
OUTSIDE_TEXT_FIELDS = {
    "get_recent_errors": frozenset({"message", "cause"}),
    "list_sagas_needing_attention": frozenset({"last_error"}),
    "get_saga": frozenset({"last_error", "detail"}),
}
# Where a quote made of several pieces is taken apart: at commas, and at "..." for something left out.
_JOINS = re.compile(r",|\.{3}|\u2026")


def verify(citations: list[dict], evidence: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """(verified, unverified) citations, each {"check": ..., "quote": ...}."""
    verified, unverified = [], []
    for citation in citations:
        (verified if _holds(citation, evidence) else unverified).append(citation)
    return verified, unverified


def set_aside(answer: dict, evidence: dict[str, dict]) -> list[dict]:
    """The text a diagnosis put aside as not being a fact (prompts.SET_ASIDE). This list is shown
    to the operator as a warning, so only what can be such text is kept: a quote found in one of
    the OUTSIDE_TEXT_FIELDS of a check that ran (not in a count, a state, a tool's own concern or
    a failed check's error), each once."""
    kept = []
    outside = {name: {"is_error": False, "result": " | ".join(_outside_text(item["result"], OUTSIDE_TEXT_FIELDS[item["tool"]]))}
               for name, item in evidence.items() if not item.get("is_error") and item.get("tool") in OUTSIDE_TEXT_FIELDS}
    found, _ = verify(answer.get("set_aside") or [], outside)
    for item in found:
        if not any((item["check"], _plain(item["quote"])) == (other["check"], _plain(other["quote"])) for other in kept):
            kept.append({"check": item["check"], "quote": item["quote"]})
    return kept


def _outside_text(value, fields: frozenset[str], field: str | None = None) -> list[str]:
    if isinstance(value, dict):
        return [text for key, inner in value.items() for text in _outside_text(inner, fields, key)]
    if isinstance(value, list):
        return [text for inner in value for text in _outside_text(inner, fields, field)]
    return [value] if isinstance(value, str) and field in fields else []


def _holds(citation: dict, evidence: dict[str, dict]) -> bool:
    item = evidence.get(citation.get("check"))
    quote = str(citation.get("quote", ""))
    if item is None or len(_plain(quote)) < MIN_QUOTE_CHARS:
        return False
    shown = _plain(prompts.shown(item))
    if _plain(quote) in shown:
        return True
    parts = [_plain(part) for part in _JOINS.split(quote)]
    parts = [part for part in parts if part]
    # each part on its own must be long enough to mean something, like a whole quote
    return len(parts) > 1 and all(len(part) >= MIN_QUOTE_CHARS and part in shown for part in parts)


def _plain(text: str) -> str:
    """Text reduced to its words and values, so that a quote matches however the model spaced or
    punctuated it: '"unanswered_percent": 91.5' and 'unanswered_percent 91.5' are the same quote."""
    return " ".join(_PUNCTUATION.sub(" ", text).lower().split())


def not_shown(answer: dict, verified: list[dict], evidence: dict[str, dict], runbook: dict) -> tuple[str, ...]:
    """The checks that can show the answer's cause, when none of the verified quotes comes from
    one of them that ran without failing; empty when the cause is shown (or has no such rule)."""
    shown_by = next((tuple(cause.get("shown_by", ())) for cause in runbook["causes"] if cause["name"] == answer["root_cause"]), ())
    shown = any(citation["check"] in shown_by and not evidence[citation["check"]].get("is_error") for citation in verified)
    return () if not shown_by or shown else shown_by


def conclude(answer: dict, verified: list[dict], unverified: list[dict], attempts: int, missing: tuple[str, ...] = ()) -> dict:
    """The diagnosis as the workflow reports it. A cause is not reported when no verified citation
    is behind it, or (missing) none from a check that can show it: it becomes "unknown", and what
    the model claimed is kept beside it."""
    diagnosis = {
        "root_cause": answer["root_cause"],
        "confidence": answer["confidence"],
        "summary": answer["summary"],
        "reasoning": answer["reasoning"],
        "evidence": verified,
        "unverified": unverified,
        "attempts": attempts,
    }
    if not verified and answer["root_cause"] != runbooks.UNKNOWN:
        diagnosis |= {"root_cause": runbooks.UNKNOWN, "confidence": "low", "claimed_root_cause": answer["root_cause"],
                      "summary": "No cause is reported: nothing the model cited could be found in the evidence. "
                                 f"(It claimed {answer['root_cause']}: {answer['summary']})"}
    elif missing and answer["root_cause"] != runbooks.UNKNOWN:
        diagnosis |= {"root_cause": runbooks.UNKNOWN, "confidence": "low", "claimed_root_cause": answer["root_cause"],
                      "summary": f"No cause is reported: {answer['root_cause']} can only be shown by {', '.join(missing)}, and "
                                 "nothing the model cited from those checks backs it. "
                                 f"(It claimed {answer['root_cause']}: {answer['summary']})"}
    return diagnosis
