"""Which failed reconciliation days the agent may propose to run again: decided by code, never by
the model.

Each day no run has reconciled, and whose newest run failed, is sorted into a cause by the source
its last error names (the runbook's error_sources: "ledger-service" in the URL that did not
answer, "Abandoned" for a run whose pod died). A day of a rerunnable cause goes on the rerun list.
A run reads and compares; it moves no money, so a rerun is safe, and the only risk is that it
fails again. Hence a day that has already failed MAX_FAILED_RUNS times needs a person: something
keeps breaking it (the hourly out-of-memory crash of 2026-10-11 was exactly that: the processor
came back after each crash, and each run crashed it again). So does a day whose error names no
known source.
"""
from agent.tools import Tools

# A day that has failed this many times is not rerun by the agent.
MAX_FAILED_RUNS = 3
TOOL = "list_failed_reconciliations"
# The step proposed when code found days to rerun but the diagnosis named no cause that reruns them.
RERUN_STEP = ("Run the reconciliation of the days listed below again: each failed because a source did not answer. A "
              "run reads and compares and moves no money. Just before each, code checks that the day is still not "
              "reconciled and that the source that failed is healthy again.")


async def read(tools: Tools) -> dict | str:
    result = await tools.call(TOOL)
    return result.data if not result.is_error else str(result.data)


def sort(failed: dict | str, runbook: dict) -> dict:
    """{"rerun": [...], "needs_a_person": [...], "unread": why or None}. Each day: id (the date),
    cause (None: its error names no known source), failed_runs, error."""
    if isinstance(failed, str):
        return {"rerun": [], "needs_a_person": [], "unread": failed}
    sources = runbook.get("error_sources", {})
    rerunnable = set(runbook.get("rerunnable", ()))
    rerun, needs_a_person = [], []
    for day in failed["days"]:
        error = day["last_error"] or ""
        cause = next((cause for cause, texts in sources.items() if any(text in error for text in texts)), None)
        row = {"id": day["date"], "cause": cause, "failed_runs": day["failed_runs"], "error": day["last_error"]}
        (rerun if cause in rerunnable and day["failed_runs"] < MAX_FAILED_RUNS else needs_a_person).append(row)
    return {"rerun": rerun, "needs_a_person": needs_a_person, "unread": None}
