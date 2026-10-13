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
# Where a quote made of several pieces is taken apart: at commas, and at "..." for something left out.
_JOINS = re.compile(r",|\.{3}|\u2026")


def verify(citations: list[dict], evidence: dict[str, dict]) -> tuple[list[dict], list[dict]]:
    """(verified, unverified) citations, each {"check": ..., "quote": ...}."""
    verified, unverified = [], []
    for citation in citations:
        (verified if _holds(citation, evidence) else unverified).append(citation)
    return verified, unverified


def set_aside(answer: dict, evidence: dict[str, dict]) -> list[dict]:
    """The text a diagnosis put aside as not being a fact (prompts.SET_ASIDE). Only real quotes
    are kept, each once: this list is shown to the operator."""
    kept = []
    found, _ = verify(answer.get("set_aside") or [], evidence)
    for item in found:
        if not any((item["check"], _plain(item["quote"])) == (other["check"], _plain(other["quote"])) for other in kept):
            kept.append({"check": item["check"], "quote": item["quote"]})
    return kept


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


def conclude(answer: dict, verified: list[dict], unverified: list[dict], attempts: int) -> dict:
    """The diagnosis as the workflow reports it. A cause with no verified citation behind it is
    not reported as the cause: it becomes "unknown", and what the model claimed is kept beside it."""
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
    return diagnosis
