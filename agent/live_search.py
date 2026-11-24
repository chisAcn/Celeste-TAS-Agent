"""Bounded beam search against a live Speedrun Tool save slot.

A candidate that dies used to cost the whole branch: the slot stayed at the
entrance and every later attempt paid for the same opening again. The slot now
walks forward. When an attempt fails, the deepest frame in its trace where she
was standing (or holding a wall) becomes the new root -- the prefix up to it is
replayed once, the slot is saved there, and the beam restarts from that frame.
``--no-checkpoint`` restores the old behaviour for comparison.
"""

import argparse
import asyncio
import json
import math
import random
import uuid
from pathlib import Path

from agent.checkpoint import SAFE_STANCES, deepest_safe, sample_stance, truncate
from agent.client import BridgeClient
from agent.local_policy import context, key
from agent.memory import Memory, assess_route, compose_skills, landing_skill
from agent.protocol import ExecutionResult, FrameAction, GameState
from agent.restore_probe import trace
from agent.tools import ToolRegistry

BUTTONS = (
    (),
    ("left",),
    ("right",),
    ("left", "jump"),
    ("right", "jump"),
    ("up", "left", "dash"),
    ("up", "right", "dash"),
)


async def search(
    client,
    dx,
    dy,
    radius,
    width,
    depth,
    budget,
    frames,
    *,
    settle=0,
    grounded_frames=4,
    min_segments=1,
    strategy="beam",
    seed=0,
    verification_runs=1,
    own_slot=True,
    absolute_target=None,
    exit_room=None,
    memory=None,
    game_version=None,
    use_skills=False,
    checkpoint=True,
    max_checkpoints=4,
    policy=None,
    stage_policy=None,
    top_k=None,
    explore_last=False,
    retain_exploration=False,
    reserve_last_attempt=False,
    continuation_ranking=False,
    second_frames=None,
    reserve_third_attempt=False,
    mirror_first=False,
    interleave_last=False,
    directional_fallback=False,
    terminal_jump_fallback=False,
    first_jump_fallback=False,
    first_dash_fallback=False,
):
    if reserve_third_attempt and (depth != 3 or strategy not in ("beam", "policy")):
        raise ValueError("reserve_third_attempt requires a three-segment beam search")
    if second_frames is not None:
        if depth < 2 or not second_frames or any(type(f) is not int or f < 1 for f in second_frames):
            raise ValueError("second_frames requires depth >= 2 and positive integer durations")
        if (depth - 1) * frames + max(second_frames) + settle > 120 or strategy not in ("beam", "policy"):
            raise ValueError("second_frames requires beam search and at most 120 total frames")
    if explore_last and (strategy != "beam" or policy is None or top_k != 3):
        raise ValueError("explore_last requires beam search, a policy and top_k=3")
    if continuation_ranking and (strategy not in ("beam", "policy") or policy is None):
        raise ValueError("continuation_ranking requires beam search and a policy")
    if retain_exploration and (not explore_last or width < 2):
        raise ValueError("retain_exploration requires explore_last and width >= 2")
    if reserve_last_attempt and (not explore_last or interleave_last):
        raise ValueError("reserve_last_attempt requires non-interleaved exploration")
    capabilities = await client.call("ping")
    if not capabilities.get("restore_and_execute") or capabilities.get("tas_active"):
        raise RuntimeError(
            "bridge 0.2.2 with Speedrun Tool and inactive Studio playback required"
        )
    if own_slot and not (await client.call("save_state"))["accepted"]:
        raise RuntimeError("could not save the current state")

    tools = ToolRegistry(client)
    fixed_direction = dx
    candidates = [
        FrameAction(buttons=list(buttons), frames=frames) for buttons in BUTTONS
    ]
    durations = list(dict.fromkeys(second_frames or [frames]))
    second_candidates = [FrameAction(buttons=list(b), frames=f) for f in durations for b in BUTTONS]

    def path_key(actions):
        return tuple((tuple(a.buttons), a.frames) for a in actions)

    frontier = [[]]
    baseline = target = best = None
    decisions = []
    trials = []
    policy_hits = policy_candidates = 0
    rng = random.Random(seed)
    settle_action = [FrameAction(buttons=[], frames=settle)] if settle else []
    root_frames = rollbacks = 0

    async def run(actions):
        result = await tools.restore_and_execute(
            execution_id=uuid.uuid4().hex,
            trace_frames=True,
            trace_platforms=True,
            actions=[action.model_dump() for action in actions],
        )

        if exit_room:
            result["crossed_exit"] = await crossed_exit(client, result, exit_room)
        return result

    async def promote(frame, actions):
        """Move the save slot forward to a frame this trace already reached.

        Only the root-to-frame prefix is replayed, never the room: that part of
        the route has already been paid for. The slot then holds the standing
        (or gripping) frame, so every later candidate starts there instead of
        at the entrance. A prefix that does not reproduce the frame is rejected
        and costs nothing -- the slot is only overwritten after the run stands.
        """
        nonlocal baseline, best, root_frames, rollbacks
        if rollbacks >= max_checkpoints or frame < 1:
            return False
        try:
            prefix = truncate(actions, frame)
        except ValueError:
            return False
        result = await run(prefix)
        ending = trace(result)[-1]
        if (
            result["status"] != "completed"
            or result["executed_frames"] != frame
            or sample_stance(ending) not in SAFE_STANCES
        ):
            return False
        if not (await client.call("save_state"))["accepted"]:
            return False
        root_frames += frame
        rollbacks += 1
        baseline = None  # the next candidate starts where she is standing
        best = None  # prefixes are root-relative; the old best cannot be replayed
        return True

    def dead_end(result):
        """The deepest frame we could restart from after this attempt failed."""
        return deepest_safe(trace(result), after=0)

    async def evaluate(actions, segments, skills=()):
        nonlocal baseline, target, best
        result = await run(actions)
        start = trace(result)[0]
        if baseline is None:
            baseline = start
            player = start["player"]
            if not player or player["dead"]:
                raise RuntimeError("restored start has no living player")
        # The goal is fixed in world coordinates once, before any rollback: dx
        # and dy mean "from where the search began", not "from the new root".
        if target is None:
            target = absolute_target or (
                baseline["player"]["x"] + dx,
                baseline["player"]["y"] + dy,
            )
        if start != baseline:
            raise RuntimeError("restored starting state changed across candidates")
        checkpoints = trace(result)
        for state in checkpoints:
            sample = state["platform_sample"]
            if sample is None or sample["total"] != len(sample["platforms"]):
                raise RuntimeError("missing or truncated platform samples")
        final = result["after"]
        player = final["player"]
        valid = (
            result["status"] == "completed"
            and player is not None
            and not player["dead"]
            and final["room"]["name"] == baseline["room"]["name"]
            and result["executed_frames"] == sum(a.frames for a in actions)
            and len(result["checkpoints"]) == result["executed_frames"]
        )
        distance = (
            math.hypot(player["x"] - target[0], player["y"] - target[1])
            if player
            else None
        )
        landing = result["checkpoints"][-grounded_frames:]
        airborne = any(
            s["player"] and not s["player"]["on_ground"] for s in checkpoints
        )
        stable = not settle or (
            airborne
            and len(landing) == grounded_frames
            and all(
                s["player"]
                and not s["player"]["dead"]
                and s["player"]["on_ground"]
                and s["room"]["name"] == baseline["room"]["name"]
                and math.hypot(
                    s["player"]["x"] - target[0], s["player"]["y"] - target[1]
                )
                <= radius
                for s in landing
            )
        )
        reached = valid and distance <= radius and stable and segments >= min_segments
        if exit_room:
            reached = result["crossed_exit"]
            valid = valid or reached
        score = (
            not valid,
            not reached,
            distance if distance is not None else math.inf,
            result["executed_frames"],
        )
        trial = {
            "actions": [a.model_dump() for a in actions],
            "segments": segments,
            "stable_landing": stable,
            "result": result,
            "valid": valid,
            "reached": reached,
            "distance": distance,
        }
        if skills:
            attempts = list(
                assess_route(
                    skills, ExecutionResult.model_validate(result), game_version
                )
            )
            trial["skills"] = [
                {"name": skill.name, "version": skill.version, "failure": reason}
                for skill, reason in attempts
            ]
            for skill, reason in attempts:
                memory.record_attempt(skill, reason)
            valid = (
                valid
                and len(attempts) == len(skills)
                and all(reason is None for _, reason in attempts)
            )
            trial["valid"] = valid
            trial["reached"] = reached = valid and reached
            score = (not valid, not reached, score[2], score[3])
        trials.append(trial)
        if not valid:
            return None
        entry = (score, len(trials) - 1, actions)
        if best is None or entry < best:
            best = entry
        return entry

    async def ranked(prefix):
        nonlocal policy_hits, policy_candidates
        choices = (second_candidates if len(prefix) == 1 else candidates).copy()
        if policy is not None:
            if prefix:
                player = policy_states[path_key(prefix)]
            else:
                player = (await client.call("get_state"))["player"]
            goal = target or absolute_target or (player["x"] + dx, player["y"] + dy)
            state = context(player, goal)
            table = policy
            stage = "terminal" if prefix else "first"
            override = (stage_policy or {}).get(stage, {})
            if override and all(key(state, a.buttons, a.frames) in override for a in choices):
                table = override
            scored = [(a, table.get(key(state, a.buttons, a.frames))) for a in choices]
            hits = sum(value is not None for _, value in scored)
            policy_hits += hits
            policy_candidates += len(choices)
            choices = [a for a, _ in sorted(scored, key=lambda pair: pair[1] or 0, reverse=True)]
            source = stage if table is not policy else "base"
            if (first_jump_fallback and not prefix and goal[1] < player["y"]
                    and hits == 0):
                direction = "right" if goal[0] >= player["x"] else "left"
                jump = next(i for i, a in enumerate(choices) if "jump" in a.buttons and direction in a.buttons)
                choices[0], choices[jump] = choices[jump], choices[0]
                source = "first_jump"
                if first_dash_fallback and player["dashes"] > 0:
                    away = "left" if direction == "right" else "right"
                    walk = next(i for i, a in enumerate(choices) if a.buttons == [away])
                    dash = next(i for i, a in enumerate(choices) if "dash" in a.buttons and direction in a.buttons)
                    choices[walk], choices[dash] = choices[dash], choices[walk]
                    source = "first_jump_dash"
            if (directional_fallback and prefix and len(prefix) == depth - 1
                    and hits == 0):
                # Prefer a legal action pointing toward an elevated target.
                direction = "right" if goal[0] >= player["x"] else "left"
                if goal[1] < player["y"] and player["dashes"] > 0:
                    idle = next(i for i, a in enumerate(choices) if not a.buttons)
                    dash = next(i for i, a in enumerate(choices) if "dash" in a.buttons and direction in a.buttons)
                    choices[idle], choices[dash] = choices[dash], choices[idle]
                    source = "directional"
            if (terminal_jump_fallback and prefix and len(prefix) == depth - 1
                    and hits == 0 and player["on_ground"] and goal[1] < player["y"]):
                horizontal = goal[0] - player["x"]
                if horizontal == 0:
                    horizontal = goal[0] - baseline["player"]["x"]
                direction = "left" if horizontal < 0 else "right"
                jump = next(i for i, a in enumerate(choices) if "jump" in a.buttons and direction in a.buttons)
                choices.insert(0, choices.pop(jump))
                source = "terminal_jump"
        if strategy == "random":
            rng.shuffle(choices)
        elif strategy == "fixed":
            choices = (
                [candidates[4 if fixed_direction >= 0 else 3]]
                if not prefix
                else [candidates[2 if fixed_direction >= 0 else 1]]
            )
        if explore_last:
            choices = [*choices[:2], rng.choice(choices[2:])]
        if policy is not None:
            decisions.append(dict(prefix=[a.buttons for a in prefix],
                                  prefix_frames=[a.frames for a in prefix], state=state,
                                  hits=hits, candidates=len(scored),
                                  source=source,
                                  selected_frames=[a.frames for a in choices[:top_k]],
                                  selected=[a.buttons for a in choices[:top_k]]))
        return choices[:top_k]

    async def schedules(branches, interleave):
        if interleave and len(branches) > 1:
            groups = [await ranked(branch) for branch in branches]
            for column in zip(*groups, strict=True):
                for branch, action in zip(branches, column):
                    yield branch, [action]
        else:
            for branch in branches:
                yield branch, await ranked(branch)

    try:
        if strategy == "fixed" and absolute_target:
            player = (await client.call("get_state"))["player"]
            if not player:
                raise RuntimeError("fixed script requires a player observation")
            fixed_direction = absolute_target[0] - player["x"]
        # A death used to throw away the whole branch. Now it only throws away
        # the airborne tail: the deepest frame she was standing on in that
        # attempt becomes the new root, and the beam restarts from there.
        restart = True
        while restart:
            restart = False
            policy_states = {}
            frontier = [[]]
            for _ in range(depth):
                children = []
                layer_budget = budget
                if reserve_third_attempt and budget >= 3:
                    layer_budget -= 2 - _
                exploration_child = None
                promoted = False
                async for prefix, choices in schedules(frontier, interleave_last and _ == depth - 1):
                    remaining = layer_budget - len(trials)
                    if reserve_last_attempt and 0 < remaining < len(choices):
                        choices = [*choices[:remaining - 1], choices[-1]]
                    for action in choices:
                        if len(trials) >= layer_budget:
                            break
                        actions = [*prefix, action]
                        attempted = [*actions, *settle_action]
                        entry = await evaluate(attempted, len(actions))
                        if entry:
                            if policy is not None and not trials[-1]["reached"]:
                                endpoint = trials[-1]["result"]["checkpoints"][sum(a.frames for a in actions) - 1]
                                policy_states[path_key(actions)] = endpoint["player"]
                            children.append((*entry[:2], actions))
                            if retain_exploration and not prefix and action == choices[-1] and len(choices) == 3:
                                exploration_child = children[-1]
                        if checkpoint and not trials[-1]["valid"]:
                            spot = dead_end(trials[-1]["result"])
                            if spot is not None and await promote(
                                spot.frame, attempted
                            ):
                                promoted = True
                                break
                        if use_skills and len(trials) == 1 and not trials[0]["reached"]:
                            state = GameState.model_validate(
                                trials[0]["result"]["before"]
                            )
                            for route in compose_skills(
                                memory, state, game_version, target
                            ):
                                if len(trials) >= layer_budget:
                                    break
                                skill_actions = [
                                    action
                                    for skill in route
                                    for action in skill.sequence.actions
                                ]
                                await evaluate(skill_actions, len(skill_actions), route)
                                if best and trials[best[1]]["reached"]:
                                    break
                        if best and trials[best[1]]["reached"]:
                            break
                    if (
                        promoted
                        or len(trials) >= layer_budget
                        or (best and trials[best[1]]["reached"])
                    ):
                        break
                if promoted:
                    restart = True
                    break
                if (
                    not children
                    or len(trials) >= budget
                    or (best and trials[best[1]]["reached"])
                ):
                    break
                if strategy == "random":
                    rng.shuffle(children)
                else:
                    children.sort()
                if continuation_ranking and len(children[0][2]) == 1:
                    def continuation_value(child):
                        state = context(policy_states[path_key(child[2])], target)
                        keys = [key(state, a.buttons, a.frames) for a in second_candidates]
                        override = (stage_policy or {}).get("terminal", {})
                        table = override if all(k in override for k in keys) else policy
                        return max((table[k] for k in keys if k in table), default=0.0)

                    children.sort(key=continuation_value, reverse=True)
                if mirror_first and width == 2 and len(children[0][2]) == 1:
                    buttons = children[0][2][0].buttons
                    opposite = {"left": "right", "right": "left"}
                    if any(b in opposite for b in buttons):
                        mirrored = {opposite.get(b, b) for b in buttons}
                        for index, child in enumerate(children[1:], 1):
                            if set(child[2][0].buttons) == mirrored:
                                children.insert(1, children.pop(index))
                                break
                retained = children[:width]
                if exploration_child is not None and exploration_child not in retained:
                    retained[-1] = exploration_child
                frontier = [entry[2] for entry in retained]
        verifications = (
            [await run(best[2]) for _ in range(verification_runs)] if best else []
        )
        verification = verifications[0] if verifications else None
        winner = trials[best[1]] if best else None
        verified = bool(
            verifications
            and all(
                (run["status"] == "completed" or run.get("crossed_exit"))
                and run["executed_frames"] == winner["result"]["executed_frames"]
                and run.get("crossed_exit") == winner["result"].get("crossed_exit")
                and trace(run) == trace(winner["result"])
                for run in verifications
            )
        )
        return {
            "target": target,
            "radius": radius,
            "evaluations": len(trials),
            "trials": trials,
            "winner": winner,
            "verification": verification,
            "verifications": verifications,
            "strategy": strategy,
            "budget": budget,
            "top_k": top_k,
            "explore_last": explore_last,
            "retain_exploration": retain_exploration,
            "reserve_last_attempt": reserve_last_attempt,
            "continuation_ranking": continuation_ranking,
            "second_frames": durations,
            "reserve_third_attempt": reserve_third_attempt,
            "decisions": decisions,
            "policy_hits": policy_hits,
            "policy_candidates": policy_candidates,
            "first_reached": next((i for i, t in enumerate(trials, 1) if t["reached"]), None),
            "settle": settle,
            "grounded_frames": grounded_frames,
            "verified": verified,
            "rollbacks": rollbacks,
            "root_frames": root_frames,
            "success": bool(verified and winner["reached"]),
        }
    finally:
        if own_slot:
            await cleanup_slot()


async def crossed_exit(client, result, destination):
    state = result["after"]
    origin = result["before"]["room"]["name"]
    if origin == destination:
        return False
    for _ in range(31):
        player = state["player"]
        if player and player["dead"]:
            return False
        if state["room"]["name"] == destination and player and (await client.call("get_playability"))["playable"]:
            return True
        if result["status"] != "interrupted":
            break
        await asyncio.sleep(0.05)
        state = await client.call("get_state")
    return False


async def cleanup_slot():
    try:
        async with BridgeClient() as cleanup:
            await cleanup.call("load_state")
    finally:
        async with BridgeClient() as cleanup:
            await cleanup.call("clear_state")


async def run(args, memory=None):
    policy = json.loads(args.policy.read_text(encoding="utf-8")) if args.policy else None
    stage_policy = json.loads(args.stage_policy.read_text()) if args.stage_policy else {}
    if args.first_only:
        stage_policy.pop("terminal", None)
    async with BridgeClient() as client:

        async def evaluate(strategy, own_slot, use_skills=False):
            return await search(
                client,
                args.dx,
                args.dy,
                args.radius,
                args.width,
                args.depth,
                args.budget,
                args.frames,
                settle=args.settle,
                grounded_frames=args.grounded_frames,
                min_segments=args.min_segments,
                strategy=strategy,
                seed=args.seed,
                verification_runs=args.repeats,
                own_slot=own_slot,
                absolute_target=args.target,
                exit_room=args.exit_room,
                memory=memory,
                game_version=args.game_version,
                use_skills=use_skills,
                policy=policy if strategy == "policy" or not args.benchmark else None,
                stage_policy=stage_policy,
                top_k=args.top_k,
                explore_last=args.explore_last,
                retain_exploration=args.retain_exploration,
                reserve_last_attempt=args.reserve_last_attempt,
                continuation_ranking=args.continuation_ranking and (not args.benchmark or strategy == "policy"),
                second_frames=args.second_frames if strategy in ("beam", "policy") else None,
                reserve_third_attempt=args.reserve_third_attempt if strategy in ("beam", "policy") else False,
                mirror_first=args.mirror_first,
                interleave_last=args.interleave_last,
                directional_fallback=args.directional_fallback,
                terminal_jump_fallback=args.terminal_jump_fallback,
                first_jump_fallback=args.first_jump_fallback or args.first_dash_fallback,
                first_dash_fallback=args.first_dash_fallback,
                checkpoint=not (args.no_checkpoint or args.benchmark),
                max_checkpoints=args.max_checkpoints,
            )

        if not (args.benchmark or args.compare_skills):
            return await evaluate(args.strategy, True, args.use_skills)
        if not (await client.call("save_state"))["accepted"]:
            raise RuntimeError("could not save the common benchmark start")
        try:
            if args.compare_skills:
                results = {
                    "without_skills": await evaluate("beam", False),
                    "with_skills": await evaluate("beam", False, True),
                }
                selected = "with_skills"
            elif policy is not None:
                results = {}
                for name in ("beam", "policy"):
                    await client.call("load_state")
                    results[name] = await evaluate(name, False)
                selected = "policy"
            else:
                results = {
                    strategy: await evaluate(strategy, False)
                    for strategy in ("beam", "fixed", "random")
                }
                selected = "beam"
            starts = [
                trace(report["trials"][0]["result"])[0] for report in results.values()
            ]
            if any(start != starts[0] for start in starts[1:]):
                raise RuntimeError("benchmark strategies did not share a common start")
            return {**results[selected], "benchmark": results}
        finally:
            await cleanup_slot()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    target_group = parser.add_mutually_exclusive_group()
    target_group.add_argument("--dx", type=float)
    target_group.add_argument("--target", type=float, nargs=2, metavar=("X", "Y"))
    parser.add_argument("--dy", type=float, default=0)
    parser.add_argument("--exit-room", help="require a living player in this destination room")
    parser.add_argument("--radius", type=float, default=4)
    parser.add_argument("--width", type=int, default=2, choices=range(1, 9))
    parser.add_argument("--depth", type=int, default=2, choices=range(1, 5))
    parser.add_argument("--budget", type=int, default=21, choices=range(1, 65))
    parser.add_argument("--frames", type=int, default=8, choices=range(1, 31))
    parser.add_argument("--settle", type=int, default=0, choices=range(121))
    parser.add_argument("--grounded-frames", type=int, default=4, choices=range(1, 121))
    parser.add_argument("--min-segments", type=int, default=1, choices=range(1, 5))
    parser.add_argument(
        "--strategy", choices=["beam", "random", "fixed"], default="beam"
    )
    parser.add_argument("--first-dash-fallback", action="store_true", help="use jump and diagonal dash toward elevated goals on unscored first steps")
    parser.add_argument("--first-jump-fallback", action="store_true", help="swap idle with a jump toward an elevated target on unscored first steps")
    parser.add_argument("--directional-fallback", action="store_true",
                        help="prioritize an upward diagonal dash toward an elevated goal on unscored final steps")
    parser.add_argument("--interleave-last", action="store_true", help="alternate branches by candidate rank on the final step")
    parser.add_argument("--mirror-first", action="store_true", help="reserve the second beam slot for the horizontally mirrored first action")
    parser.add_argument("--stage-policy", type=Path)
    parser.add_argument("--first-only", action="store_true")
    parser.add_argument("--policy", type=Path)
    parser.add_argument("--top-k", type=int, choices=range(1, 8))
    parser.add_argument("--terminal-jump-fallback", action="store_true",
                        help="prefer a grounded jump for an unscored terminal step toward an elevated goal")
    parser.add_argument("--reserve-third-attempt", action="store_true",
                        help="with depth 3, reserve the last evaluation for a third action")
    parser.add_argument("--second-frames", type=int, nargs="+",
                        help="candidate action durations for segment two, sharing one evaluation budget")
    parser.add_argument("--continuation-ranking", action="store_true",
                        help="rank first-layer beam branches by their highest learned next-action value")
    parser.add_argument("--reserve-last-attempt", action="store_true",
                        help="when a candidate group exceeds the remaining budget, try its sampled action last")
    parser.add_argument("--retain-exploration", action="store_true",
                        help="reserve a first-layer beam slot for the valid sampled candidate")
    parser.add_argument("--explore-last", action="store_true",
                        help="with --policy --top-k 3, sample the third action from the remaining candidates")
    parser.add_argument("--benchmark", action="store_true")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=1, choices=range(1, 4))
    parser.add_argument("--output", type=Path, default=Path("logs/live-search.json"))
    parser.add_argument("--skill-name")
    parser.add_argument("--game-version")
    parser.add_argument("--memory", type=Path, default=Path("logs/live-skills.sqlite3"))
    parser.add_argument("--use-skills", action="store_true")
    parser.add_argument("--compare-skills", action="store_true")
    parser.add_argument("--trajectory-jsonl", type=Path)
    parser.add_argument("--skill-revision", type=int, default=1)
    parser.add_argument(
        "--no-checkpoint",
        action="store_true",
        help="restart every attempt from the saved slot instead of "
        "rolling back to the last frame she stood on",
    )
    parser.add_argument(
        "--max-checkpoints",
        type=int,
        default=4,
        choices=range(1, 17),
        help="how many times a failed attempt may move the start forward",
    )
    parser.add_argument("--exit-stages", type=Path)
    parser.add_argument("--exit-order", choices=["enumeration","round-robin","alignment","calibrated"], default="alignment")
    parser.add_argument("--exit-priors", type=Path)
    parser.add_argument("--exhaustive", action="store_true")
    args = parser.parse_args()
    if args.exit_stages:
        from agent import timed_exit
        if not args.exit_room or (args.exit_order=="calibrated" and not args.exit_priors):
            parser.error("timed profiles require --exit-room and calibrated ordering requires --exit-priors")
        report = asyncio.run(timed_exit.run(args))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2))
        print({k:report[k] for k in ("evaluations","first_reached","verified")})
        raise SystemExit(0 if report["success"] else 1)
    if args.dx is None and args.target is None:
        parser.error("provide --dx, --target or --exit-stages")
    if args.policy and (args.strategy != "beam" or args.compare_skills):
        parser.error("--policy requires beam search")
    if args.stage_policy and (not args.policy or args.depth != 2):
        parser.error("--stage-policy requires --policy and --depth 2")
    if args.first_only and not args.stage_policy:
        parser.error("--first-only requires --stage-policy")
    if args.mirror_first and (args.width != 2 or args.strategy != "beam" or args.benchmark):
        parser.error("--mirror-first requires standalone beam search with width 2")
    if args.dx is None:
        args.dx = 0
    if (
        not all(
            map(math.isfinite, (args.dx, args.dy, args.radius, *(args.target or [])))
        )
        or args.radius < 0
    ):
        parser.error("finite target offsets and a nonnegative radius required")
    if (
        (args.depth - 1) * args.frames + max(args.second_frames or [args.frames]) + args.settle > 120
        or args.min_segments > args.depth
        or (args.settle and args.grounded_frames > args.settle)
    ):
        parser.error("invalid segment or landing frame budget")
    if args.skill_name and (
        not args.game_version or args.repeats != 3 or not args.settle
    ):
        parser.error("skill export requires --game-version, --repeats 3 and --settle")
    if sum((args.benchmark, args.compare_skills, args.use_skills)) > 1:
        parser.error("choose one of --benchmark, --compare-skills and --use-skills")
    if (args.compare_skills or args.use_skills) and (
        args.strategy != "beam" or args.budget < 2
    ):
        parser.error("skill planning requires beam search and at least two evaluations")
    if (
        args.compare_skills or args.use_skills or args.trajectory_jsonl
    ) and not args.game_version:
        parser.error("skill planning and trajectory storage require --game-version")
    if args.skill_revision < 1:
        parser.error("skill revision must be positive")
    memory = None
    if args.game_version:
        args.memory.parent.mkdir(parents=True, exist_ok=True)
        memory = Memory(args.memory)
    try:
        report = asyncio.run(run(args, memory))
        if memory:
            for strategy, recorded in report.get(
                "benchmark", {args.strategy: report}
            ).items():
                memory.store_report(recorded, args.game_version, strategy)
            memory.export_trajectories(
                args.trajectory_jsonl or args.memory.with_suffix(".jsonl")
            )
        if report["success"] and args.skill_name:
            skill = landing_skill(
                report, args.skill_name, args.game_version, args.skill_revision
            )
            memory.save_skill(skill)
            print({"skill": skill.name, "memory": str(args.memory)})
    finally:
        if memory:
            memory.close()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        {key: report[key] for key in ("target", "evaluations", "verified", "success")}
    )
    if "benchmark" in report:
        print(
            {
                name: {key: result[key] for key in ("evaluations", "success")}
                for name, result in report["benchmark"].items()
            }
        )
    raise SystemExit(0 if report["success"] else 1)


if __name__ == "__main__":
    main()
