"""The runbook of an alert: runbooks/<AlertName>.md.

A runbook has two readers. The header (between the --- lines) is for the code: which checks
this alert may use, each a tool with its arguments already filled in, and which root causes
the alert can have. The Markdown below it is for the model: what the alert means and how to
tell those causes apart.

Looked up by the alert's exact name. There is no search and no fallback: an alert without a
runbook is one nobody has thought through yet, and the agent says so instead of improvising.
"""
import re
from dataclasses import asdict, dataclass
from pathlib import Path

import yaml

_ALERT_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]*")
_CAUSE_NAME = re.compile(r"[a-z][a-z0-9_]*")
# Always a possible answer, in every runbook: none of the listed causes, or the evidence does not say which.
UNKNOWN = "unknown"
# The section of the guidance that the report quotes as what to do. Every runbook must have it.
RESOLUTION_HEADING = "## What resolves it"


@dataclass(frozen=True)
class Check:
    """One thing the investigation may look at: a tool call, complete with its arguments."""
    name: str
    tool: str
    arguments: dict
    tells: str


@dataclass(frozen=True)
class Cause:
    """One thing that can be behind the alert. The diagnosis names one of these, or "unknown"."""
    name: str
    description: str


@dataclass(frozen=True)
class Runbook:
    alert: str
    based_on: str
    causes: tuple[Cause, ...]
    checks: tuple[Check, ...]
    guidance: str
    resolution: str    # the guidance's "What resolves it" section, without its heading

    def as_dict(self) -> dict:
        """For the investigation's state and trace, which hold plain data."""
        return asdict(self) | {"causes": [asdict(cause) for cause in self.causes],
                               "checks": [asdict(check) for check in self.checks]}


class NoRunbook(Exception):
    def __init__(self, alert_name: str, available: list[str]):
        self.alert_name = alert_name
        self.available = available
        known = ", ".join(available) if available else "none"
        super().__init__(f"No runbook for {alert_name} yet. Runbooks exist for: {known}.")


class InvalidRunbook(Exception):
    """A runbook file that the workflow must not run with."""


def available(directory: Path) -> list[str]:
    return sorted(path.stem for path in directory.glob("*.md"))


def load(alert_name: str, directory: Path) -> Runbook:
    # the name comes from outside (a command line, later a webhook): it must not be a path
    if not _ALERT_NAME.fullmatch(alert_name):
        raise NoRunbook(alert_name, available(directory))
    # compared with the real file names: on Windows the file system would also find another spelling
    if alert_name not in available(directory):
        raise NoRunbook(alert_name, available(directory))
    path = directory / f"{alert_name}.md"
    return parse(path.read_text(encoding="utf-8"), path.name)


def parse(text: str, file_name: str) -> Runbook:
    def invalid(why: str) -> InvalidRunbook:
        return InvalidRunbook(f"{file_name}: {why}")

    parts = text.replace("\r\n", "\n").split("---\n", 2)   # however the file's lines end
    if len(parts) != 3 or parts[0].strip():
        raise invalid("it must begin with a header between two --- lines")
    try:
        header = yaml.safe_load(parts[1])
    except yaml.YAMLError as e:
        raise invalid(f"the header is not valid YAML ({e})") from e
    guidance = parts[2].strip()
    if not isinstance(header, dict):
        raise invalid("the header must be a set of fields")
    if header.get("alert") != Path(file_name).stem:
        raise invalid(f"the header says alert: {header.get('alert')}, the file is named for {Path(file_name).stem}")
    if not guidance:
        raise invalid("it has no guidance below the header")
    named = header.get("causes")
    if not isinstance(named, dict) or not named:
        raise invalid("the header needs at least one cause")
    causes = []
    for name, description in named.items():
        if not isinstance(name, str) or not _CAUSE_NAME.fullmatch(name) or name == UNKNOWN:
            raise invalid(f"cause {name} must be a lower-case name such as processor_outage (\"{UNKNOWN}\" is always there already)")
        if not isinstance(description, str) or not description.strip():
            raise invalid(f"cause {name} needs a line saying what it means")
        causes.append(Cause(name=name, description=" ".join(description.split())))

    listed = header.get("checks")
    if not isinstance(listed, dict) or not listed:
        raise invalid("the header needs at least one check")

    checks = []
    for name, fields in listed.items():
        if not isinstance(fields, dict) or not isinstance(fields.get("tool"), str) or not isinstance(fields.get("tells"), str):
            raise invalid(f"check {name} needs a tool and a line saying what it tells")
        arguments = fields.get("arguments") or {}
        if not isinstance(arguments, dict) or set(fields) - {"tool", "arguments", "tells"}:
            raise invalid(f"check {name} may only have tool, arguments and tells")
        checks.append(Check(name=str(name), tool=fields["tool"], arguments=arguments, tells=" ".join(fields["tells"].split())))
    resolution = _section(guidance, RESOLUTION_HEADING)
    if not resolution:
        raise invalid(f"the guidance needs a section \"{RESOLUTION_HEADING}\" (the report quotes it)")
    return Runbook(alert=header["alert"], based_on=str(header.get("based_on", "")), causes=tuple(causes),
                   checks=tuple(checks), guidance=guidance, resolution=resolution)


def _section(guidance: str, heading: str) -> str:
    """The text under a "## " heading, up to the next one."""
    lines = guidance.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == heading)
    except StopIteration:
        return ""
    body = []
    for line in lines[start + 1:]:
        if line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body).strip()
