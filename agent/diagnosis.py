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
from collections import Counter

from agent import prompts, runbooks

# Shorter than this, a "quote" matches almost anything ("0", "up").
MIN_QUOTE_CHARS = 3
# JSON's own punctuation, and the backslash it puts before a quote inside a value: a log line
# that says role "x" is written role \"x\" in the result's text, and must still match. Single
# quotes too: a model may write message: 'Simulated outage set to 240s'.
_PUNCTUATION = re.compile("[\\s\"'{}\\[\\],:\\\\]+")
# Typography a model writes where the evidence has the plain character: gpt-oss quoted
# "ledger‑service" with a non-breaking hyphen (U+2011). Dashes become "-", curly quotes plain ones,
# non-breaking spaces spaces.
_TYPOGRAPHY = str.maketrans({**dict.fromkeys("‐‑‒–—―−", "-"),
                             **dict.fromkeys("‘’", "'"), **dict.fromkeys("“”", '"'),
                             **dict.fromkeys("  ", " ")})
# Per tool, the fields of its result that can carry text from outside PayFlow's operators: what a
# service logged (a log line can quote a request), the cause of an error, and what a partner
# answered, kept as a saga's error or step detail. Everything else in a result (counts, states,
# names, the tools' own "concerns", Kubernetes' messages) is written by the platform or the tools.
OUTSIDE_TEXT_FIELDS = {
    "get_recent_errors": frozenset({"message", "cause"}),
    "list_sagas_needing_attention": frozenset({"last_error"}),
    "get_saga": frozenset({"last_error", "detail"}),
    # a failed run's error is Java's text, which can carry a remote service's answer
    "get_reconciliation": frozenset({"last_error"}),
}
# Where a quote made of several pieces is taken apart: at commas, at "..." for something left out,
# and at the end of a sentence (a model quoting a long log line drops a sentence from its middle).
_JOINS = re.compile(r",|\.{3}|\u2026|(?<=\.)\s")


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
    outside = {name: {"is_error": False, "result": " | ".join(text for check, text in outside_texts(evidence) if check == name)}
               for name in evidence}
    found, _ = verify(answer.get("set_aside") or [], outside)
    return merge_set_aside([], found)


def outside_texts(evidence: dict[str, dict]) -> list[tuple[str, str]]:
    """(check, text) for each piece of text from outside PayFlow's operators that the checks
    returned (OUTSIDE_TEXT_FIELDS of checks that ran), each once."""
    texts = []
    for name, item in evidence.items():
        if item.get("is_error") or item.get("tool") not in OUTSIDE_TEXT_FIELDS:
            continue
        for text in _outside_text(item["result"], OUTSIDE_TEXT_FIELDS[item["tool"]]):
            if (name, text) not in texts:
                texts.append((name, text))
    return texts


def merge_set_aside(kept: list[dict], more: list[dict]) -> list[dict]:
    """kept, then each of more not already in it (the same check and the same words)."""
    merged = [dict(item) for item in kept]
    for item in more:
        if not any((item["check"], _plain(item["quote"])) == (other["check"], _plain(other["quote"])) for other in merged):
            merged.append({"check": item["check"], "quote": item["quote"]})
    return merged


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
    # A quote as long as the answer's shape allows was cut there by that limit, often in the middle
    # of a word ("/v1/repts" for "/v1/reports"): the cut word is ours, what comes before it must be there.
    if len(quote) >= prompts.MAX_QUOTE_CHARS and " " in _plain(quote):
        head = _plain(quote).rsplit(" ", 1)[0]
        if len(head) >= MIN_QUOTE_CHARS and head in shown:
            return True
    parts = [_plain(part) for part in _JOINS.split(quote)]
    parts = [part for part in parts if part]
    if len(quote) >= prompts.MAX_QUOTE_CHARS and parts and " " in parts[-1]:
        parts[-1] = parts[-1].rsplit(" ", 1)[0]      # cut by the limit, as above
    # each part on its own must be long enough to mean something, like a whole quote
    return len(parts) > 1 and all(len(part) >= MIN_QUOTE_CHARS and part in shown for part in parts)


def _plain(text: str) -> str:
    """Text reduced to its words and values, so that a quote matches however the model spaced or
    punctuated it: '"unanswered_percent": 91.5' and 'unanswered_percent 91.5' are the same quote."""
    return " ".join(_PUNCTUATION.sub(" ", text.translate(_TYPOGRAPHY)).lower().split())


def unsupported(answer: dict, verified: list[dict], evidence: dict[str, dict], runbook: dict,
                sorted_by_code: list[dict] | None = None) -> list[str]:
    """Why the answer's cause cannot stand on this evidence, one sentence per reason; empty when
    it can (or the runbook sets no rule for it). Three rules, all from the runbook:
    - shown_by: a verified quote must come from a check that can show the cause and found something;
    - requires_clear: some causes also need checks to have run and found nothing. "The processor
      cannot be reached" means something only when the processor itself looks fine; when its pod
      status or health failed, or shows a problem, payment-service's failed calls say nothing more;
    - sorted by code: where code has already sorted what the alert is about into causes (parked
      sagas by their codes, failed days by their errors: sorted_by_code, rows with a "cause"), a
      cause that none of them has cannot be the cause. Seen: a model whose own reasoning said
      partner_rejected answered processor_no_answer, while no saga had parked for lack of an answer."""
    cause = next((item for item in runbook["causes"] if item["name"] == answer["root_cause"]), None)
    if cause is None:
        return []
    reasons = []
    if sorted_by_code and cause["name"] in _causes_code_sorts(runbook):
        found = Counter(row["cause"] for row in sorted_by_code if row.get("cause"))
        if cause["name"] not in found:
            counts = ", ".join(f"{count} {name}" for name, count in found.most_common()) or "none of a known cause"
            reasons.append(f"{cause['name']} is not the cause of any of them: code sorted them by what PayFlow recorded "
                           f"(each saga's code, or each day's error) into {counts}.")
    missing = not_shown(answer, verified, evidence, runbook)
    if missing:
        reasons.append(f"{cause['name']} is shown only by {', '.join(missing)}, and only when the check found something "
                       "(a logged line, or a concern); none of the quotes comes from such a check.")
    not_clear = [f"{name} {_why_not_clear(evidence.get(name))}" for name in cause.get("requires_clear", ())
                 if _why_not_clear(evidence.get(name))]
    if not_clear:
        reasons.append(f"{cause['name']} also needs {', '.join(cause['requires_clear'])} to have run and found nothing, "
                       f"but {'; '.join(not_clear)}.")
    return reasons


def _causes_code_sorts(runbook: dict) -> set[str]:
    return set(runbook.get("parking_codes", {})) | set(runbook.get("error_sources", {}))


def _why_not_clear(item: dict | None) -> str:
    if item is None:
        return "was not run"
    if item.get("is_error"):
        return "failed"
    return "found something" if found_something(item) else ""


def not_shown(answer: dict, verified: list[dict], evidence: dict[str, dict], runbook: dict) -> tuple[str, ...]:
    """The checks that can show the answer's cause, when none of the verified quotes comes from
    one of them that ran and found something (see found_something); empty when the cause is
    shown (or has no such rule). A check that can show a cause shows nothing when it found nothing:
    "error_lines: 0" from the processor's log is no evidence of an outage."""
    shown_by = next((tuple(cause.get("shown_by", ())) for cause in runbook["causes"] if cause["name"] == answer["root_cause"]), ())
    shown = any(citation["check"] in shown_by and found_something(evidence[citation["check"]]) for citation in verified)
    return () if not shown_by or shown else shown_by


def found_something(item: dict) -> bool:
    """Whether a check's result has anything in it to show a cause: for a log check, a logged line
    (a warning is enough: an outage being switched on is one); for any other, a concern the tool
    raised. A failed check found nothing."""
    result = item.get("result")
    if item.get("is_error") or not isinstance(result, dict):
        return False
    return bool(result.get("groups")) if "groups" in result else bool(result.get("concerns"))


def conclude(answer: dict, verified: list[dict], unverified: list[dict], attempts: int, unsupported_because: list[str] = ()) -> dict:
    """The diagnosis as the workflow reports it. A cause is not reported when no verified citation
    is behind it, or the runbook's rules for it do not hold (unsupported_because, see unsupported):
    it becomes "unknown", and what the model claimed is kept beside it."""
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
    elif unsupported_because and answer["root_cause"] != runbooks.UNKNOWN:
        diagnosis |= {"root_cause": runbooks.UNKNOWN, "confidence": "low", "claimed_root_cause": answer["root_cause"],
                      "summary": f"No cause is reported: {' '.join(unsupported_because)} "
                                 f"(It claimed {answer['root_cause']}: {answer['summary']})"}
    return diagnosis
