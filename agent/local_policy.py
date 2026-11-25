"""Fit a tabular Monte Carlo action-value policy from live-search reports."""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path


def context(player, target):
    offsets = [math.floor((t - player[k]) / 16) for t, k in zip(target, ("x", "y"))]
    velocity = [round(player.get(k, 0) / 50) for k in ("vx", "vy")]
    return [*offsets, *velocity, player["on_ground"], player["dashes"]]


def key(state, buttons, frames):
    return json.dumps([state, sorted(buttons), frames])


def repeated_unscored_states(failures):
    cases = defaultdict(set)
    for report in failures:
        if report["success"]:
            continue
        start = report["trials"][0]["result"]["before"]
        case = (start["room"]["name"], start["player"]["x"], start["player"]["y"],
                tuple(report["target"]))
        for decision in report["decisions"]:
            if decision["hits"] == 0:
                cases[tuple(decision["state"])].add(case)
    return {state for state, tasks in cases.items() if len(tasks) >= 2}


def train(reports, *, progress_reward=False, shrinkage=0.0, states_to_fit=None, stats=None):
    if not math.isfinite(shrinkage) or shrinkage < 0:
        raise ValueError("shrinkage must be finite and nonnegative")
    totals = defaultdict(float)
    counts = defaultdict(int)
    origins = defaultdict(set)
    for report in reports:
        for trial in report["trials"]:
            result = trial["result"]
            states = [result["before"], *result["checkpoints"]]
            steps = []
            offset = 0
            for action in trial["actions"][:trial["segments"]]:
                if offset >= len(states) or not states[offset]["player"] or states[offset]["player"]["dead"]:
                    break
                player = states[offset]["player"]
                steps.append((context(player, report["target"]), action))
                offset += action["frames"]
            if not trial["valid"]:
                reward = -1.0
            elif trial["reached"]:
                reward = 1.0
            else:
                distance = trial["distance"]
                reward = 0.5 / (1 + distance / 16)
                if progress_reward and steps:
                    start = states[0]["player"]
                    initial = math.dist((start["x"], start["y"]), report["target"])
                    reward += 0.1 * math.tanh((initial - distance) / 16)
            for state, action in reversed(steps):
                if states_to_fit is None or tuple(state) in states_to_fit:
                    action_key = key(state, action["buttons"], action["frames"])
                    totals[action_key] += reward
                    counts[action_key] += 1
                    if stats is not None:
                        start = result["before"]
                        origins[action_key].add((start["room"]["name"], start["player"]["x"],
                                                 start["player"]["y"]))
                reward *= 0.95
    if not totals and states_to_fit is None:
        raise ValueError("no observed action transitions in reports")
    if stats is not None:
        stats.update({k: dict(observations=counts[k], distinct_starts=len(origins[k]))
                      for k in totals})
    return {k: total / (counts[k] + shrinkage) for k, total in totals.items()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--stats-output", type=Path,
                        help="write observation and distinct room/x/y start counts for this training run")
    parser.add_argument("--fill-missing-in", type=Path, help="base policy to augment without replacing learned values")
    parser.add_argument("--failure-reports", nargs="+", type=Path,
                        help="search reports used to identify repeatedly unscored states")
    parser.add_argument("--shrinkage", type=float, default=0.0)
    parser.add_argument("--progress-reward", action="store_true",
                        help="include a bounded distance-progress bonus in unsuccessful valid trials")
    args = parser.parse_args()
    if args.stats_output and args.stats_output.resolve() == args.output.resolve():
        parser.error("--stats-output and --output require different files")
    reports = (json.loads(path.read_text(encoding="utf-8-sig")) for path in args.reports)
    if bool(args.fill_missing_in) != bool(args.failure_reports):
        parser.error("use --fill-missing-in and --failure-reports together")
    states = None
    if args.failure_reports:
        states = repeated_unscored_states(json.loads(p.read_text(encoding="utf-8-sig")) for p in args.failure_reports)
    stats = {} if args.stats_output else None
    values = train(reports, progress_reward=args.progress_reward, shrinkage=args.shrinkage,
                   states_to_fit=states, stats=stats)
    if args.fill_missing_in:
        values.update(json.loads(args.fill_missing_in.read_text(encoding="utf-8-sig")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(values), encoding="utf-8")
    if args.stats_output:
        args.stats_output.parent.mkdir(parents=True, exist_ok=True)
        args.stats_output.write_text(json.dumps(stats), encoding="utf-8")


if __name__ == "__main__":
    main()
