"""Configurable candidate routes for timed upper exits."""
import argparse
import asyncio
import itertools
import json
import math
import uuid
from pathlib import Path

from agent.client import BridgeClient
from agent.protocol import FrameAction
from agent.restore_probe import observed
from agent.tools import ToolRegistry


def key(action):
    return json.dumps([sorted(action.buttons), action.frames])


def calibrate(report):
    counts = {}
    for t in report["trials"]:
        pair = counts.setdefault(key(FrameAction.model_validate(t["actions"][-1])), [0, 0])
        pair[0] += int(t["crossed"])
        pair[1] += 1
    return {k: (w+1)/(n+2) for k,(w,n) in counts.items()}


def order_routes(stages, mode, x, center, priors):
    routes = list(itertools.product(*stages))
    if mode == "round-robin":
        return [tuple([first,*tail]) for tail in itertools.product(*stages[1:]) for first in stages[0]]
    if mode == "enumeration":
        return routes
    def error(route):
        travel = sum((int("right" in a.buttons)-int("left" in a.buttons))*min(a.frames,10)
                     *(3 if "dash" in a.buttons else 1) for a in route)
        return abs(x+travel-center)
    geometry = sorted(routes,key=error)
    if mode == "alignment":
        return geometry
    preferred = sorted(geometry,key=lambda r:-priors.get(key(r[-1]),0.5))
    result = []
    while preferred:
        route = geometry[0] if len(result)%3==2 else preferred[0]
        result.append(route)
        preferred.remove(route)
        geometry.remove(route)
    return result


async def evaluate(c, stages, dest, mode, budget, priors, exhaustive=False):
    from agent.live_search import crossed_exit
    info = await c.call("get_room_info")
    gate = next(g for g in info["exits"] if g["target"]==dest and g["side"]=="up")
    x = (await c.call("get_state"))["player"]["x"]
    routes = order_routes(stages,mode,x,(gate["from"]+gate["to"])/2,priors)
    start = None
    async def attempt(actions):
        nonlocal start
        r = await ToolRegistry(c).restore_and_execute(execution_id=uuid.uuid4().hex,trace_frames=True,trace_platforms=True,actions=actions)
        before = observed(r["before"])
        if start is None:
            start = before
        if before != start:
            raise RuntimeError("restored start changed")
        return await crossed_exit(c,r,dest)
    trials = []
    first = None
    for route in routes[:budget]:
        actions = [a.model_dump() for a in route]
        crossed = await attempt(actions)
        trials.append(dict(actions=actions,crossed=crossed))
        if crossed and first is None:
            first = len(trials)
            if not exhaustive:
                break
    verified = bool(first and await attempt(trials[first-1]["actions"]))
    return dict(start=start,target=gate,trials=trials,evaluations=len(trials),first_reached=first,verified=verified,success=verified)


def load_stages(path):
    stages = [[FrameAction.model_validate(a) for a in s] for s in json.loads(path.read_text())]
    if not stages or any(not s for s in stages) or len(stages)>64 or sum(max(a.frames for a in s) for s in stages)>120 or math.prod(map(len,stages))>4096:
        raise ValueError("invalid stage count, frame budget or candidate count")
    return stages


async def run(args):
    from agent.live_search import cleanup_slot
    stages = load_stages(args.exit_stages)
    priors = json.loads(args.exit_priors.read_text()) if args.exit_priors else {}
    async with BridgeClient() as c:
        if not (await c.call("save_state"))["accepted"]:
            raise RuntimeError("save rejected")
        try:
            return await evaluate(c,stages,args.exit_room,args.exit_order,args.budget,priors,args.exhaustive)
        finally:
            await cleanup_slot()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description="Calibrate terminal actions on real room crossings")
    parser.add_argument("report",type=Path)
    parser.add_argument("output",type=Path)
    args=parser.parse_args()
    args.output.write_text(json.dumps(calibrate(json.loads(args.report.read_text())),indent=2))
