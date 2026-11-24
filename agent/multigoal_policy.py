"""Relabel measured one- and two-action endpoints for multiple relative goals."""
import math
from collections import defaultdict

from agent.local_policy import context, key


def expand(grid, offsets, prior, radius=4):
    buttons = grid["buttons"]
    frames = grid["frames"]
    player = grid["start"]["player"]
    endpoints = {tuple(e["actions"]): e["xy"] for e in grid["ends"]}
    samples = {stage: defaultdict(list) for stage in ("first", "terminal")}
    for dx, dy in offsets:
        target = player["x"] + dx, player["y"] + dy

        def value(actions):
            xy = endpoints.get(actions)
            if xy is None:
                return -1
            distance = math.dist(xy, target)
            return int(distance <= radius) + 1 / (1 + distance)

        for i, action in enumerate(buttons):
            options = [value((i, j)) for j in range(len(buttons))]
            best = max(value((i,)), max(options))
            first_key = key(context(player, target), action, frames)
            samples["first"][first_key].append(best)
            if (i,) in endpoints:
                midpoint = grid["first_states"][str(i)]["player"]
                state = context(midpoint, target)
                for other, score in zip(buttons, options):
                    samples["terminal"][key(state, other, frames)].append(score)
    model = {stage: dict(table) for stage, table in prior.items()}
    for stage, table in samples.items():
        for action_key, scores in table.items():
            model[stage][action_key] = sum(scores) / len(scores)
    return model
