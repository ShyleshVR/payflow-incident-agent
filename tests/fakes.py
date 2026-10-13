"""Stand-ins for the model and the tools, for tests of the workflow."""
from agent.llm import Reply
from agent.tools import ToolResult


class Answer(dict):
    """A model's answer as plain data, with the two things the workflow uses of a real one:
    attribute access and model_dump(). Not validated: a real model server may not hold the
    model to the shape either, and tests need to give answers outside it."""

    __getattr__ = dict.__getitem__

    def model_dump(self):
        return dict(self)


class ScriptedModel:
    """A model that answers from a script.

    Choosing checks: each round takes the next entry of `rounds` (a list of check names), and
    "enough" (no checks) once they are used up.
    Diagnosing: each call takes the next entry of `diagnoses` (a dict with root_cause, confidence,
    evidence and summary); with none left it answers "unknown" without citing anything.
    It remembers what it was shown.
    """

    def __init__(self, *rounds, diagnoses=(), reasoning="scripted"):
        self._rounds = list(rounds)
        self._diagnoses = list(diagnoses)
        self._reasoning = reasoning
        self.asked = []       # (messages, shape) of every call that chose checks
        self.diagnosing = []  # (messages, shape, thinking) of every call that diagnosed

    async def ask(self, messages, shape, max_tokens, thinking=False):
        if shape.__name__ == "Diagnosis":
            self.diagnosing.append((messages, shape, thinking))
            answer = {"reasoning": self._reasoning, "root_cause": "unknown", "confidence": "low", "evidence": [],
                      "summary": "Nothing was diagnosed."} | (self._diagnoses.pop(0) if self._diagnoses else {})
            return Reply(Answer(answer), input_tokens=1800, output_tokens=200)
        self.asked.append((messages, shape))
        picks = self._rounds.pop(0) if self._rounds else []
        return Reply(Answer(reasoning=self._reasoning, next_checks=list(picks)), input_tokens=900, output_tokens=40)

    def prompt(self, call=-1) -> str:
        """The human message of a call that chose checks."""
        return self.asked[call][0][1][1]


class FakeTools:
    """Tools that answer from a table (a result, or a ToolResult for an error), with "nothing of
    concern" for any tool not in it, and remember what was asked."""

    def __init__(self, alerts=(), **answers):
        self.answers = {"list_active_alerts": {"count": len(alerts), "alerts": list(alerts)}} | answers
        self.asked = []

    async def call(self, tool, arguments=None):
        self.asked.append((tool, arguments or {}))
        answer = self.answers.get(tool, {"concerns": []})
        return answer if isinstance(answer, ToolResult) else ToolResult(False, answer)


def alert(name, severity="critical", minutes=3, **about):
    return {"alert": name, "severity": severity, "about": about, "firing_for_minutes": minutes, "summary": f"{name} summary"}


CIRCUIT_OPEN = alert("ProcessorCircuitOpen", application="payment-service")
