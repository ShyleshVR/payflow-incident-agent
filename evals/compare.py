"""One table for several models:  python -m evals.compare <results .json files>

Each file is an eval's results (evals/results/*.json): the tuning set or the held-out set, of
one model. Files of the same model are put on one row. Rows are ordered the way the agent's
model is chosen (decided 2026-10-14): correctness first (scenarios passed, then root causes right,
then citations verified), then safety (planted instructions obeyed count against), then speed
(seconds per investigation), only between equals.

The table is printed and written next to the results it was made from.
"""
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from evals.run_eval import RESULTS_DIR

# The sets an eval can run: tuned on, held out (seen while tuning the rules), fresh (made for the comparison).
PARTS = ("tuning", "held_out", "fresh")


def rows(results: list[dict]) -> list[dict]:
    by_model: dict[str, dict] = {}
    for result in results:
        model = by_model.setdefault(result["model"], {"model": result["model"], "runs": []})
        part = next((name for prefix, name in (("held-out-", "held_out"), ("fresh-", "fresh"))
                     if all(row["scenario"].startswith(prefix) for row in result["scenarios"])), "tuning")
        if part in model:
            raise ValueError(f"two {part.replace('_', '-')} results for {result['model']}: {model[part]['id']} and {result['id']}")
        model[part] = result
        model["runs"].append(result["id"])
    return sorted(by_model.values(), key=_rank)


def _rank(model: dict) -> tuple:
    totals = [model[part]["totals"] for part in PARTS if part in model]
    obeyed = sum(total["instruction_read"] - total["instruction_ignored"] for total in totals)
    seconds = _per_investigation(totals)
    return (-sum(total["passed"] for total in totals), -sum(total["cause_right"] for total in totals),
            -sum(total["citations_verified"] for total in totals), obeyed, seconds if seconds is not None else float("inf"))


def _per_investigation(totals: list[dict]) -> float | None:
    finished = sum(total["scenarios"] - total["errors"] for total in totals)
    return round(sum(total["seconds"] for total in totals) / finished, 1) if finished else None


def render(models: list[dict], made_at: datetime) -> str:
    def part(model: dict, name: str, field: str) -> str:
        if name not in model:
            return "not run"
        totals = model[name]["totals"]
        return f"{totals[field]} of {totals['scenarios']}"

    lines = [f"# Model comparison {made_at:%Y-%m-%d %H:%M} UTC", "",
             ("Ordered by correctness (scenarios passed, root cause right, citations verified), then safety (planted "
              "instructions obeyed), then speed. Same prompts and rules for every model; the prompts were tuned on "
              "qwen3.5-9b, which favours it."), "",
             ("| Model | Tuning set passed | Root cause right | Citations verified | Planted instructions ignored | "
             "Held-out passed | Held-out cause right | Fresh passed | Fresh cause right | Seconds per investigation | "
             "Output tokens per investigation | Repeatable |"),
             "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for model in models:
        totals = [model[name]["totals"] for name in PARTS if name in model]
        read = sum(total["instruction_read"] for total in totals)
        ignored = sum(total["instruction_ignored"] for total in totals)
        finished = sum(total["scenarios"] - total["errors"] for total in totals)
        tokens = round(sum(total["output_tokens"] for total in totals) / finished) if finished else None
        repeatable = {model[name].get("prompt_cache") for name in PARTS if name in model} == {False}
        lines.append(f"| {model['model']} | {part(model, 'tuning', 'passed')} | {part(model, 'tuning', 'cause_right')} | "
                     f"{part(model, 'tuning', 'citations_verified')} | {ignored} of the {read} read | "
                     f"{part(model, 'held_out', 'passed')} | {part(model, 'held_out', 'cause_right')} | "
                     f"{part(model, 'fresh', 'passed')} | {part(model, 'fresh', 'cause_right')} | "
                     f"{_per_investigation(totals)} | {tokens} | {'yes' if repeatable else 'no'} |")
    lines += ["", "Made from: " + ", ".join(f"`{run}`" for model in models for run in model["runs"])]
    return "\n".join(lines) + "\n"


def main(paths: list[str], results_dir: Path = RESULTS_DIR, now=lambda: datetime.now(UTC)) -> Path:
    results = [json.loads(Path(path).read_text(encoding="utf-8")) for path in paths]
    made_at = now()
    table = render(rows(results), made_at)
    out = results_dir / f"{made_at:%Y%m%dT%H%M%SZ}-comparison.md"
    out.write_text(table, encoding="utf-8")
    print(table)
    print(f"written to {out}")
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("usage: python -m evals.compare <results .json files>")
    main(sys.argv[1:])
