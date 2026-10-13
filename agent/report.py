"""The report of an investigation, for a person: runs/<id>.md, made from the trace.

Written by code, not by the model. Every line comes from a field of the trace: what the checks
returned, the diagnosis whose quotes were verified, the runbook's own text. The one piece of the
model's prose it carries, the summary, is marked as the model's. Because it is made from the
trace alone, the report of any past investigation can be produced again at any time.
"""

UNKNOWN = "unknown"
SAGA_LIST_TOOL = "list_sagas_needing_attention"


def render(document: dict) -> str:
    """The trace document (agent.trace) as Markdown."""
    if document["status"] != "completed":
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
    text = ("This agent takes no action. From the runbook:\n\n"
            + "\n".join(f"> {line}".rstrip() for line in document["runbook"]["resolution"].splitlines()))
    waiting = [finding for name, finding in _saga_findings(document)]
    if waiting:
        text += "\n\nSagas waiting for an operator, according to the checks:\n\n" + "\n".join(f"- {line}" for line in waiting)
    return text


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
    if document["status"] == "completed":
        diagnosis = document["result"]["diagnosis"]
        outcome = "cause unknown" if diagnosis["root_cause"] == UNKNOWN else f"{diagnosis['root_cause']} ({diagnosis['confidence']})"
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
