"""Compare learned rankings from one Speedrun Tool snapshot."""
import argparse
import asyncio
import json
from pathlib import Path

from agent.client import BridgeClient
from agent.live_search import cleanup_slot, search
from agent.local_policy import train


async def compare(paths):
    async with BridgeClient() as client:
        state = await client.call("get_state")
        room = state["room"]["name"]
        reports = []
        sources = []
        for path in paths:
            report = json.loads(path.read_text(encoding="utf-8-sig"))
            if all(t["result"]["before"]["room"]["name"] != room for t in report["trials"]):
                reports.append(report)
                sources.append(str(path))
        policies = {k: train(reports, shrinkage=k) for k in (0, 1, 4)}
        rows = []

        async def evaluate(split, dx, dy, label, policy):
            if not (await client.call("load_state"))["accepted"]:
                raise RuntimeError("could not restore comparison snapshot")
            report = await search(client, dx, dy, 6, 2, 2, 8, 10,
                                  min_segments=2, own_slot=False, checkpoint=False,
                                  policy=policy, top_k=3)
            trials = report["trials"]
            row = dict(split=split, dx=dx, dy=dy, policy=label, target=report["target"],
                       success=report["success"], attempts=len(trials),
                       first=report["first_reached"], hits=report["policy_hits"],
                       candidates=report["policy_candidates"],
                       deaths=sum(any(s["player"] and s["player"]["dead"]
                                      for s in t["result"]["checkpoints"]) for t in trials))
            rows.append(row)
            return row

        if not (await client.call("save_state"))["accepted"]:
            raise RuntimeError("could not save comparison snapshot")
        try:
            scores = {}
            for k in (1, 4):
                validation = [await evaluate("validation", dx, dy, str(k), policies[k])
                              for dx, dy in ((16, 0), (16, -16))]
                scores[k] = (sum(r["success"] for r in validation),
                             -sum(r["attempts"] for r in validation),
                             -sum(r["deaths"] for r in validation))
            selected = max(scores, key=scores.get)
            for split, dx, dy in (("holdout", 24, 0), ("holdout", -16, 0),
                                  ("holdout", 24, -16), ("coverage", 32, 0),
                                  ("coverage", 48, -16)):
                for label, policy in (("default", None), ("0", policies[0]),
                                      (str(selected), policies[selected])):
                    await evaluate(split, dx, dy, label, policy)
        finally:
            await cleanup_slot()
    return dict(room=room, training=sources, selected=selected, rows=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(compare(args.reports))
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
