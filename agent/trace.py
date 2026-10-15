"""The record of one investigation: runs/<id>.json.

The file is rewritten after every step, so it is always one complete, valid JSON document. If a
run crashes or is interrupted, everything up to the last finished step is still there. The same
file is what a replay reads its tool results from.
"""
import json
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def utc_now() -> datetime:
    return datetime.now(UTC)


class Trace:
    def __init__(self, alert_name: str, runs_dir: Path, replay_of: str | None = None, now=utc_now):
        started = now()
        self._now = now
        self.id = f"{started:%Y%m%dT%H%M%SZ}-{alert_name}"
        self.path = runs_dir / f"{self.id}.json"
        self.report_path = runs_dir / f"{self.id}.md"
        self.document: dict[str, Any] = {
            "id": self.id,
            "alert_name": alert_name,
            "replay_of": replay_of,
            "model": None,
            "started_at": _stamp(started),
            "finished_at": None,
            "status": "running",
            "error": None,
            "alerts": [],
            "runbook": None,
            "steps": [],
            "result": None,
        }
        self._open_step: dict[str, Any] | None = None
        # Nothing is written until the alert is known to be firing: asking about an alert that is
        # not firing is not an investigation and leaves no file.
        self._confirmed = False

    @classmethod
    def reopen(cls, path: Path, now=utc_now) -> "Trace":
        """A written trace, to add steps to: a paused investigation, resumed (perhaps by another process)."""
        document = load(path)
        trace = cls.__new__(cls)
        trace._now = now
        trace.id = document["id"]
        trace.path = Path(path)
        trace.report_path = Path(path).with_suffix(".md")
        trace.document = document
        trace._open_step = None
        trace._confirmed = True
        return trace

    @asynccontextmanager
    async def step(self, name: str):
        """Records one step of the workflow: when it ran, how long it took, and (through the
        yielded dict) what it produced. Tool calls made inside it are attached to it."""
        started = self._now()
        clock = time.perf_counter()
        entry: dict[str, Any] = {"step": name, "started_at": _stamp(started), "seconds": None, "model_calls": [],
                                 "tool_calls": [], "output": None}
        self._open_step = entry
        try:
            yield entry
        except BaseException as e:
            entry["error"] = f"{type(e).__name__}: {e}"
            raise
        finally:
            entry["seconds"] = round(time.perf_counter() - clock, 3)
            self.document["steps"].append(entry)
            self._open_step = None
            self.save()

    def tool_call(self, tool: str, arguments: dict, seconds: float, is_error: bool, result: Any) -> None:
        call = {"tool": tool, "arguments": arguments, "seconds": round(seconds, 3), "is_error": is_error, "result": result}
        (self._open_step["tool_calls"] if self._open_step else self.document.setdefault("tool_calls_outside_steps", [])).append(call)

    def model_call(self, messages: list[tuple[str, str]], answer: dict, seconds: float,
                   input_tokens: int | None, output_tokens: int | None) -> None:
        """Everything the model was shown and what it answered, under the step that asked."""
        call = {"messages": [{"role": role, "content": content} for role, content in messages], "answer": answer,
                "seconds": round(seconds, 3), "input_tokens": input_tokens, "output_tokens": output_tokens}
        (self._open_step["model_calls"] if self._open_step else self.document.setdefault("model_calls_outside_steps", [])).append(call)

    def confirm(self, alerts: list[dict]) -> None:
        """The alert is firing: from here on the investigation is written to disk."""
        self.document["alerts"] = alerts
        self._confirmed = True

    def finish(self, status: str, result: dict | None = None, error: str | None = None) -> None:
        self.document |= {"status": status, "result": result, "error": error, "finished_at": _stamp(self._now())}
        self.save()

    def save(self) -> None:
        if not self._confirmed:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # written beside the file and moved over it, so a reader never sees half a document
        partial = self.path.with_suffix(".json.partial")
        partial.write_text(json.dumps(self.document, indent=2, ensure_ascii=False), encoding="utf-8")
        partial.replace(self.path)


    def write_report(self, text: str) -> None:
        """The readable report, beside the trace (and, like it, only for a confirmed investigation)."""
        if self._confirmed:
            self.report_path.write_text(text, encoding="utf-8")


def load(path: Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _stamp(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
