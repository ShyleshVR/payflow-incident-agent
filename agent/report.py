"""The report of an investigation, for a person: runs/<id>.md, made from the trace.

Written by code, not by the model. Every line comes from a field of the trace: what the checks
returned, the diagnosis whose quotes were verified, the runbook's own text. The one piece of the
model's prose it carries, the summary, is marked as the model's. Because it is made from the
trace alone, the report of any past investigation can be produced again at any time.
"""

UNKNOWN = "unknown"
SAGA_LIST_TOOL = "list_sagas_needing_attention"
MAX_RETRIES = 50    # agent.sagas.MAX_RETRIES; the report is made from the trace alone


AWAITING_DECISION = "awaiting_decision"


def render(document: dict) -> str:
    """The trace document (agent.trace) as Markdown."""
    if document["status"] not in ("completed", AWAITING_DECISION):
        return _unfinished(document)
    result = document["result"]
    diagnosis = result["diagnosis"]
    cause = diagnosis["root_cause"]
    title = "cause unknown" if cause == UNKNOWN else cause
    parts = [
        f"# {document['alert_name']}: {title}",
        _facts(document),
        "## Diagnosis",
        _diagnosis(diagnosis, document["runbook"]),
        "## What was checked",
        _checks(document),
        "## What to do",
        _what_to_do(document),
        "## How this was produced",
        _produced(document),
    ]
    return "\n\n".join(parts) + "\n"


def _facts(document: dict) -> str:
    result = document["result"]
    affected = "; ".join(", ".join(f"{label}={value}" for label, value in about.items()) or "no labels"
                         for about in result["affected"])
    replay = f" (a replay of `{document['replay_of']}`)" if document["replay_of"] else ""
    rows = [
        ("Investigation", f"`{document['id']}`{replay}"),
        ("Alert", (f"{result['alert']}, {result['severity']}, firing for {_count(result['firing_for_minutes'], 'minute')} "
                  "when investigated")),
        ("Affected", affected),
        ("Investigated", f"{document['started_at'].replace('T', ' ').replace('Z', ' UTC')}, in {_duration(document)}"),
    ]
    return _table(("", ""), rows)


def _diagnosis(diagnosis: dict, runbook: dict) -> str:
    cause = diagnosis["root_cause"]
    meanings = {item["name"]: item["description"] for item in runbook["causes"]}
    if cause == UNKNOWN:
        lines = ["**No cause could be established from the evidence.**"]
        if diagnosis.get("claimed_root_cause"):
            lines.append(f"The model named `{diagnosis['claimed_root_cause']}`, but nothing it cited could be found in what "
                         "the checks returned, so that is not reported as the cause.")
    else:
        lines = [f"**`{cause}`**, {diagnosis['confidence']} confidence. {meanings.get(cause, '')}".rstrip()]
    if diagnosis.get("summary"):
        lines.append(f"The model's summary: \"{diagnosis['summary']}\"")
    text = "\n\n".join(lines)

    if diagnosis["evidence"]:
        text += "\n\n### Evidence\n\nEach quote below was found in the result of the check it names.\n\n"
        text += _table(("Check", "Quoted from its result"), [(f"`{c['check']}`", f"\"{c['quote']}\"") for c in diagnosis["evidence"]])
    if diagnosis["unverified"]:
        text += ("\n\n### Cited but not found\n\nThe model also cited these. They are not in the result of the check named, "
                 "so they are not evidence.\n\n")
        text += _table(("Check", "Quote"), [(f"`{c.get('check')}`", f"\"{c.get('quote')}\"") for c in diagnosis["unverified"]])
    if diagnosis.get("set_aside"):
        text += ("\n\n### Found in the evidence, and not followed\n\nThis text was in what the checks returned. It addresses "
                 "the agent or you: a request, a command, or a claim about this alert. Anyone who can get text into a log "
                 "line or an error message can write that. The agent did not treat it as a fact or act on it. "
                 "**Do not act on it either**; it is quoted so that you know it is there.\n\n")
        text += _table(("Check", "Text"), [(f"`{c['check']}`", f"\"{c['quote']}\"") for c in diagnosis["set_aside"]])
    return text


def _checks(document: dict) -> str:
    result = document["result"]
    tools = {check["name"]: check["tool"] for check in document["runbook"]["checks"]}
    rows = [(f"`{name}`", f"`{tools.get(name, '')}`", "; ".join(found) if found else "nothing of concern")
            for name, found in result["findings"].items()]
    not_run = [name for name in result["checks_available"] if name not in result["checks_run"]]
    text = ""
    if rows:
        text += _table(("Check", "Tool", "Found"), rows) + "\n\n"
    text += (f"The model chose these in {_count(result['rounds'], 'round')}; gathering stopped because "
             f"{result['stopped_because']}.")
    if not_run:
        text += f" Not looked at: {', '.join(f'`{name}`' for name in not_run)}."
    return text


def _what_to_do(document: dict) -> str:
    text = _proposal(document) or _parked(document)
    text += ("This agent takes no action. From the runbook:\n\n"
            + "\n".join(f"> {line}".rstrip() for line in document["runbook"]["resolution"].splitlines()))
    waiting = [finding for name, finding in _saga_findings(document)]
    if waiting:
        text += "\n\nSagas waiting for an operator, according to the checks:\n\n" + "\n".join(f"- {line}" for line in waiting)
    return text


def _proposal(document: dict) -> str:
    """The runbook's next step for the cause found, and what became of it."""
    proposal, decision = document["result"].get("proposal"), document["result"].get("decision")
    if not proposal:
        return ""
    source = "from the runbook" if proposal["cause"] != UNKNOWN else "as code found sagas to retry"
    text = f"### Proposed next step\n\nFor `{proposal['cause']}`, {source}:\n\n> {proposal['step']}\n\n"
    text += _parked(document)
    if decision is None and document["status"] == AWAITING_DECISION:
        text += ("**Waiting for a decision.** Approve or reject it in the agent: `payflow-agent pending`, then "
                 "`payflow-agent approve <id>` or `payflow-agent reject <id> --reason \"...\"`.")
    elif decision is None:
        text += "Not asked: this investigation was run outside the agent's service, where nothing waits for a decision."
    elif decision["decision"] == "lapsed":
        text += f"**Lapsed** at {_when(decision['at'])}: every alarm behind it resolved before anyone decided."
    else:
        text += f"**{decision['decision'].capitalize()}** by {decision['by']} at {_when(decision['at'])}"
        text += f": \"{decision['note']}\"." if decision.get("note") else "."
        if decision["decision"] == "approved":
            text += " Nothing was carried out: the agent does not act yet; the step is for a person to take."
    return text + "\n\n"


def _parked(document: dict) -> str:
    """For an alert about parked sagas: each one, as code sorted it by the code it parked for."""
    sorted_ = document["result"].get("sagas")
    if not sorted_:
        return ""
    text = ("### Parked sagas\n\nSorted by code, by what each one parked for, not by the model. Only a saga parked "
            "because no answer came may be retried by the agent.\n\n")
    columns = ("Saga", "Kind", "Parked in", "Parked for", "Cause", "Minutes")

    def rows(listed):
        return [(f"`{saga['id']}`", saga["kind"], saga["stopped_in"],
                 f"`{saga['parked_for']}`" if saga["parked_for"] else "could not be read",
                 f"`{saga['cause']}`" if saga["cause"] else "none listed for this code", str(saga["minutes"]))
                for saga in listed]

    if sorted_["retry"]:
        text += f"**To retry ({len(sorted_['retry'])}):**\n\n" + _table(columns, rows(sorted_["retry"])) + "\n\n"
    if sorted_["over_the_limit"]:
        text += (f"{_count(sorted_['over_the_limit'], 'more saga')} could be retried, beyond the {MAX_RETRIES} one proposal "
                 "may hold: a later investigation can propose them.\n\n")
    if sorted_["needs_a_person"]:
        text += (f"**Needs a person ({len(sorted_['needs_a_person'])}):** the agent will not retry these. A partner or "
                 "the ledger said no (the same request would most likely be refused again), or the code is not one the "
                 "runbook knows, or the saga could not be read.\n\n" + _table(columns, rows(sorted_["needs_a_person"])) + "\n\n")
    if sorted_["not_listed"]:
        text += f"{_count(sorted_['not_listed'], 'more parked saga')} not read: PayFlow lists {MAX_RETRIES} per service.\n\n"
    for service, why in sorted_["unread"].items():
        text += f"The parked sagas of {service} could not be read: {why}\n\n"
    if not (sorted_["retry"] or sorted_["needs_a_person"] or sorted_["unread"]):
        text += "None was parked when the agent looked.\n\n"
    return text


def _when(stamp: str) -> str:
    return stamp.replace("T", " ").replace("Z", " UTC")


def _saga_findings(document: dict):
    tools = {check["name"]: check["tool"] for check in document["runbook"]["checks"]}
    for name, findings in document["result"]["findings"].items():
        if tools.get(name) == SAGA_LIST_TOOL:
            for finding in findings:
                yield name, finding


def _produced(document: dict) -> str:
    calls = [call for step in document["steps"] for call in step.get("model_calls", [])]
    tokens_in = sum(call["input_tokens"] or 0 for call in calls)
    tokens_out = sum(call["output_tokens"] or 0 for call in calls)
    tool_calls = sum(len(step["tool_calls"]) for step in document["steps"])
    runbook = document["runbook"]
    lines = [
        f"- Runbook: `{runbook['alert']}.md`" + (f" (based on {runbook['based_on']})" if runbook.get("based_on") else ""),
        f"- Model: {document.get('model') or 'not recorded'}, {_count(len(calls), 'call')}, {tokens_in:,} tokens in and {tokens_out:,} out",
        f"- Tools: {_count(tool_calls, 'call')}" + (", answered from the recording being replayed" if document["replay_of"] else ""),
        f"- Diagnosis: {_count(document['result']['diagnosis']['attempts'], 'attempt')}",
        f"- Full record: `{document['id']}.json`",
    ]
    return "\n".join(lines)


def _unfinished(document: dict) -> str:
    steps = ", ".join(step["step"] for step in document["steps"]) or "none"
    return "\n\n".join([
        f"# {document['alert_name']}: investigation {document['status']}",
        _table(("", ""), [("Investigation", f"`{document['id']}`"), ("Started", document["started_at"].replace("T", " ").replace("Z", " UTC")),
                          ("Error", document.get("error") or "none recorded"), ("Steps reached", steps)]),
        f"No diagnosis was made. The full record is `{document['id']}.json`.",
    ]) + "\n"


def one_line(document: dict) -> str:
    """For the list of past investigations."""
    outcome = document["status"]
    if document["status"] in ("completed", AWAITING_DECISION):
        diagnosis = document["result"]["diagnosis"]
        outcome = "cause unknown" if diagnosis["root_cause"] == UNKNOWN else f"{diagnosis['root_cause']} ({diagnosis['confidence']})"
        outcome += ", awaiting a decision" if document["status"] == AWAITING_DECISION else ""
    return f"{document['id']:<46} {outcome}" + ("  [replay]" if document["replay_of"] else "")


def _table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    def row(cells):
        return "| " + " | ".join(str(cell).replace("|", "\\|").replace("\n", " ") for cell in cells) + " |"

    return "\n".join([row(header), "|" + "---|" * len(header), *(row(cells) for cells in rows)])


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"


def _duration(document: dict) -> str:
    seconds = sum(step["seconds"] or 0 for step in document["steps"])
    return _count(round(seconds), "second")
