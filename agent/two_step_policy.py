"""Build first-step and terminal-action rankings from a two-step search grid."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from agent.local_policy import context, key
from agent.restore_probe import observed


def build(report, traces=None):
    buttons = report["buttons"]
    frames = report["frames"]
    target = report["target"]
    start = observed(report["start"])
    states = {int(i): state for i, state in report.get("first_states", {}).items()}
    if traces:
        for trial in traces["trials"]:
            result = trial["result"]
            action = trial["actions"][0]
            if (observed(result["before"]) == start and action["frames"] == frames
                    and action["buttons"] in buttons and len(result["checkpoints"]) >= frames):
                i = buttons.index(action["buttons"])
                state = observed(result["checkpoints"][frames - 1])
                if states.setdefault(i, state) != state:
                    raise ValueError("inconsistent intermediate observation")

    routes = report["routes"]
    expected = {(i, j) for i in range(len(buttons)) for j in range(len(buttons))}
    if len(routes) != len(expected) or {(r["first"], r["second"]) for r in routes} != expected:
        raise ValueError("complete two-step grid required")
    first = defaultdict(list)
    terminal = defaultdict(list)
    for row in routes:
        i, j = row["first"], row["second"]
        distance = row["distance"]
        value = -1 if distance is None else int(row["reached"]) + 1 / (1 + distance)
        first[key(context(start["player"], target), buttons[i], frames)].append(value)
        if i not in states:
            raise ValueError(f"missing intermediate observation for action {i}")
        player = states[i]["player"]
        if player and not player["dead"]:
            terminal[key(context(player, target), buttons[j], frames)].append(value)
    return {"first": {k: max(v) for k, v in first.items()},
            "terminal": {k: sum(v) / len(v) for k, v in terminal.items()}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("grid", type=Path)
    parser.add_argument("--traces", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    model = build(json.loads(args.grid.read_text()), json.loads(args.traces.read_text()) if args.traces else None)
    args.output.write_text(json.dumps(model, indent=2))
