"""Wires the two agents together and supervises the run.

The planner is a task. The executor is the loop. Everything they share is a
queue of plain messages, so neither can block the other by accident:

    executor --anchor--> planner --plan--> executor --outcome--> planner

The promise that removes stale plans is in the middle: an anchor is only
published while the world is standing still, and a plan is only accepted while
that exact anchor is still current. A plan that lost its anchor is dropped
before a single frame is sent, which costs nothing; running it and letting the
contract refuse it afterwards also costs nothing in frames but is a step of
progress lost, and the report it produces is harder to read than "the world
moved, planning again".

``on_unstable`` decides what to do when the world refuses to stand still (a lift
carrying her, ice, a hazard that keeps her sliding). ``wait`` keeps watching and
never freezes the game; ``freeze`` falls back to the old behaviour and pauses the
room for the length of the model call.
"""

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Callable

from agent.agents.messages import (
    Anchor,
    PlanFailure,
    PlannedStep,
    StepOutcome,
    drift,
)
from agent.agents.standby import Standby
from agent.protocol import ActionSequence


class _Finished(Exception):
    """Ends the run from deep inside the wait loops."""

    def __init__(self, verdict: str, reason: str):
        super().__init__(reason)
        self.verdict = verdict
        self.reason = reason


@dataclass
class Stats:
    anchors: int = 0
    steps: int = 0
    landed: int = 0
    superseded: int = 0
    rejected: int = 0
    plan_failures: int = 0
    deviations: int = 0
    standby_seconds: float = 0.0
    frozen_seconds: float = 0.0
    freezes: int = 0
    longest_standby: float = 0.0

    def summary(self) -> dict:
        data = asdict(self)
        data["standby_seconds"] = round(self.standby_seconds, 1)
        data["frozen_seconds"] = round(self.frozen_seconds, 1)
        data["longest_standby"] = round(self.longest_standby, 1)
        return data


@dataclass
class Option:
    exit_room: str | None = None
    success_room: str | None = None
    max_steps: int = 20
    max_retries: int = 3
    max_plan_failures: int = 5
    poll: float = 0.1
    still_for: float = 0.15
    drift_px: float = 1.5
    anchor_timeout: float = 8.0
    unstable: str = "wait"
    settle_timeout: float = 2.5
    respawn_timeout: float = 5.0
    think_budget: float = 90.0
    frozen_slack: float = 8.0
    telemetry: bool = True
    record: Callable[[dict], None] | None = None


class TeamSession:
    """Runs the planner task and the executor loop until the trial ends."""

    def __init__(self, executor, planner, **kwargs):
        self.option = Option(**kwargs)
        self.executor = executor
        self.planner = planner
        self.standby = Standby(still_for=self.option.still_for,
                               drift_px=self.option.drift_px)
        self.anchors: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.plans: asyncio.Queue = asyncio.Queue(maxsize=1)
        self.outcomes: asyncio.Queue = asyncio.Queue()
        self.stop = asyncio.Event()
        self.stats = Stats()
        self._counter = 0
        self._ready = None
        self._paused = False
        self._unstable_since: float | None = None
        self._frozen_since: float | None = None
        self._geometry: dict[str, dict | None] = {}
        self._entities: dict[str, list[str] | None] = {}
        self._telemetry = True
        self._log: list[dict] = []

    # -- lifecycle -------------------------------------------------------

    async def run(self) -> str:
        await self._probe()
        task = asyncio.create_task(self.planner.serve(
            self.anchors, self.plans, self.outcomes, self.stop),
            name="planner")
        try:
            verdict = await self._loop()
        except _Finished as finished:
            self.event({"state": "END", "verdict": finished.verdict,
                        "reason": finished.reason})
            verdict = finished.verdict
        finally:
            self.stop.set()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            # The last step's outcome has no next plan waiting for it, but the
            # route book should still learn from it before the session closes.
            self.planner.drain(self.outcomes)
            if self._paused:
                await self._resume()
            self.event({"state": "SESSION", **self.stats.summary(),
                        "planner_failures": self.planner.failures,
                        "planner_error": self.planner.last_error})
        return verdict

    def event(self, payload: dict) -> None:
        payload = {"collab": True, "t": round(time.monotonic(), 3), **payload}
        self._log.append(payload)
        del self._log[:-200]
        if self.option.record:
            self.option.record(payload)

    # -- the loop --------------------------------------------------------

    async def _loop(self) -> str:
        retries = 0
        while self.stats.steps < self.option.max_steps:
            state = self._ready or await self.executor.observe()
            self._ready = None
            if self._done(state):
                self.event({"state": "EXIT", "verdict": "STOPPED", "source": "observation"})
                return "STOPPED"
            if state.player is None:
                self.event({"state": "NO_PLAYER"})
                await self._wait_playable()
                self.standby.reset()
                continue

            now = time.monotonic()
            stance = self.standby.observe(state, now)
            if stance == "BLOCKED":
                retries += 1
                if retries > self.option.max_retries:
                    self.event({"state": "RESPAWN_LIMIT", "retries": retries})
                    return "FAILED"
                await self._wait_playable()
                continue

            anchor = await self._anchor(state, stance, now)
            if anchor is None:
                await asyncio.sleep(self.option.poll)
                continue

            step = await self._decide(anchor)
            if step is None:
                continue

            outcome = await self._execute(anchor, step)
            if outcome is None:
                continue
            retries = 0 if outcome.verdict == "LANDED" else retries + 1
            if outcome.verdict == "DIED":
                await self._wait_playable()
                self.standby.reset()
            if (outcome.report.executed_frames > 0 and self._done(outcome.report.end)):
                self.event({"state": "EXIT", "verdict": "SUCCESS", "source": "execution"})
                return "SUCCESS"
            if self._done(outcome.settled):
                self.event({"state": "EXIT", "verdict": "STOPPED", "source": "settling"})
                return "STOPPED"
            if retries >= self.option.max_retries:
                self.event({"state": "RETRY_LIMIT", "retries": retries})
                return "FAILED"
        self.event({"state": "STEP_LIMIT", "steps": self.stats.steps})
        return "STOPPED"

    # -- anchors ---------------------------------------------------------

    async def _anchor(self, state, stance: str, now: float):
        """Publish an anchor when the world allows it, or wait for a better one."""
        if stance == "ANCHORED":
            self._unstable_since = None
            return await self._publish(state, still_for=self.standby.stable_for(now))
        if self._unstable_since is None:
            self._unstable_since = now
        waited = now - self._unstable_since
        self.stats.standby_seconds += self.option.poll
        self.stats.longest_standby = max(self.stats.longest_standby, waited)
        if waited < self.option.anchor_timeout:
            return None
        if self.option.unstable == "freeze":
            return await self._freeze(state)
        self.event({"state": "NO_ANCHOR", "waited": round(waited, 1),
                    "on_ground": state.player.on_ground,
                    "vx": round(state.player.vx, 1), "vy": round(state.player.vy, 1)})
        raise _Finished("STOPPED", "the world never held still; try --on-unstable freeze")

    async def _publish(self, state, *, still_for=0.0, frozen=False) -> Anchor:
        self._counter += 1
        geometry = await self._room_geometry(state.room.name)
        # Moving hazards and platforms must be sampled for each anchor.
        geometry = dict(geometry or {})
        for method, key, params in (
            ("get_collision", "collision", {"limit": 128}),
            ("get_tiles", "tiles", {"whole_room": True}),
        ):
            try:
                geometry[key] = await self.executor.call(method, **params)
            except RuntimeError:
                # Older bridges and protocol fixtures may not support these probes.
                pass
        entities = await self._room_entities(state.room.name)
        platforms = await self._platforms() if self.option.telemetry and self._telemetry else None
        anchor = Anchor(id=self._counter, state=state, still_for=round(still_for, 2),
                        frozen=frozen, geometry=geometry, platforms=platforms,
                        entities=entities)
        self.stats.anchors += 1
        player = state.player
        self.event({"state": "ANCHOR", "anchor": anchor.id, "frozen": frozen,
                    "still_for": anchor.still_for,
                    "at": None if player is None else [round(player.x, 1), round(player.y, 1)]})
        await _latest(self.anchors, anchor)
        return anchor

    async def _freeze(self, state) -> Anchor:
        """Last resort: pause the room, plan on a frozen snapshot, resume to run it."""
        waited = time.monotonic() - (self._unstable_since or time.monotonic())
        await self.executor.call("pause_game")
        self._paused = True
        self._frozen_since = time.monotonic()
        frozen = await self.executor.observe()
        self.stats.freezes += 1
        self._unstable_since = None  # the next freeze gets a fresh grace window
        self.event({"state": "FREEZE", "anchor": self._counter + 1,
                    "waited": round(waited, 1)})
        return await self._publish(frozen, frozen=True)

    async def _resume(self) -> None:
        if not self._paused:
            return
        self._paused = False
        if self._frozen_since is not None:
            self.stats.frozen_seconds += time.monotonic() - self._frozen_since
            self._frozen_since = None
        await self.executor.call("resume_game")

    def _lost(self, anchor: Anchor, state) -> str | None:
        if state.room.name != anchor.state.room.name:
            return "room_changed"
        if state.player is None or state.player.dead:
            return "player_changed"
        tolerance = self.option.frozen_slack if anchor.frozen else self.option.drift_px
        if drift(state, anchor.state) > tolerance:
            return "drifted"
        return None

    # -- waiting for a plan ---------------------------------------------

    async def _decide(self, anchor: Anchor):
        deadline = time.monotonic() + self.option.think_budget
        try:
            return await self._await_plan(anchor, deadline)
        finally:
            # A frozen anchor is only good while the room is paused, so the
            # resume happens here: as late as possible, before the first frame.
            if anchor.frozen:
                await self._resume()

    async def _await_plan(self, anchor: Anchor, deadline: float):
        while True:
            try:
                message = await asyncio.wait_for(self.plans.get(), self.option.poll)
            except asyncio.TimeoutError:
                if anchor.frozen:
                    self._check_deadline(deadline)
                    continue
                state = await self.executor.observe()
                if self._done(state):
                    self.event({"state": "EXIT", "verdict": "STOPPED", "source": "while_planning"})
                    raise _Finished("STOPPED", "target reached outside agent execution")
                lost = self._lost(anchor, state)
                if lost is not None:
                    self.stats.superseded += 1
                    self.event({"state": "ANCHOR_LOST", "anchor": anchor.id,
                                "reason": lost})
                    self.standby.reset()
                    return None
                self._check_deadline(deadline)
                continue

            if message.anchor_id != anchor.id:
                self.stats.superseded += 1
                self.event({"state": "SUPERSEDED", "anchor": anchor.id,
                            "plan_anchor": message.anchor_id})
                continue
            if isinstance(message, PlanFailure):
                self.stats.plan_failures += 1
                self.event({"state": "PLAN_FAILED", "anchor": anchor.id,
                            "kind": message.kind, "detail": message.detail})
                if self.stats.plan_failures >= self.option.max_plan_failures:
                    raise _Finished("FAILED", f"planner failed: {message.detail}")
                return None

            # Pre-flight: the anchor promise is checked before any input, so the
            # executor never has to reject a step it already started.
            lost = self._lost(anchor, await self.executor.observe())
            if lost is not None:
                self.stats.superseded += 1
                self.event({"state": "ANCHOR_LOST", "anchor": anchor.id,
                            "reason": lost, "when": "pre_flight"})
                self.standby.reset()
                return None
            return message

    def _check_deadline(self, deadline: float) -> None:
        if time.monotonic() > deadline:
            raise _Finished("FAILED", "the planner stopped answering")

    # -- running a step --------------------------------------------------

    async def _execute(self, anchor: Anchor, step: PlannedStep):
        sequence = ActionSequence(
            execution_id=f"anchor{anchor.id}-{uuid.uuid4().hex[:6]}",
            actions=step.actions, expect=step.expect, tolerance=step.tolerance,
            capture_positions=True)
        try:
            report = await self.executor.run(sequence, step.contract)
        except RuntimeError as error:
            if not any(token in str(error) for token in ("not playable", "busy")):
                raise
            self.event({"state": "NOT_PLAYABLE", "anchor": anchor.id,
                        "reason": str(error)})
            await self._wait_playable()
            self.standby.reset()
            return None

        self.stats.steps += 1
        settled = report.end
        if report.status in {"SUCCEEDED", "UNSATISFIED"}:
            settled = await self._settle(report.end)

        if report.status == "REJECTED":
            # Cannot happen while the anchor promise holds; if it does, the
            # promise is what is broken, not the plan.
            self.stats.rejected += 1
            verdict, detail = "REJECTED", report.reason
        elif report.status in {"DIED", "ABORTED", "INTERRUPTED"}:
            verdict, detail = report.status, report.reason
        else:
            failure = self.executor.success_reason(step.contract, settled)
            verdict, detail = ("LANDED", None) if failure is None else ("MISSED", failure)

        if verdict == "LANDED":
            self.stats.landed += 1
        if report.deviation is not None:
            self.stats.deviations += 1
        self.event({"step": self.stats.steps, "state": "VERIFY", "anchor": anchor.id,
                    "plan": {"goal": step.goal, "reason": step.reason,
                             "actions": [a.model_dump() for a in step.actions],
                             "latency": step.latency,
                             "expect": None if step.expect is None else len(step.expect)},
                    "verdict": verdict, "verified": verdict == "LANDED",
                    "detail": detail, "frames": report.executed_frames,
                    "deviation": (None if report.deviation is None
                                  else report.deviation.model_dump()),
                    "settled": _point(settled)})
        outcome = StepOutcome(verdict=verdict, report=report, actions=step.actions,
                              settled=settled, detail=detail)
        await self.outcomes.put(outcome)
        return outcome

    async def _settle(self, end):
        """Watch until gravity is done with her, without touching the game."""
        if end is None:
            end = await self.executor.observe()
        deadline = time.monotonic() + self.option.settle_timeout
        state = end
        while time.monotonic() < deadline:
            stance = self.standby.observe(state, time.monotonic())
            if stance == "ANCHORED":
                self._ready = state
                return state
            if stance == "BLOCKED":
                return state
            await asyncio.sleep(self.option.poll)
            state = await self.executor.observe()
        return state

    # -- helpers ---------------------------------------------------------

    def _done(self, state) -> bool:
        if state is None:
            return False
        if state.room.completed:
            return True
        if not (state.player and not state.player.dead):
            return False
        if self.option.success_room:
            return state.room.name == self.option.success_room
        return bool(self.option.exit_room and state.room.name != self.option.exit_room)

    async def _wait_playable(self) -> None:
        """The room respawns on its own; reloading near a boundary has crashed it."""
        deadline = time.monotonic() + self.option.respawn_timeout
        while time.monotonic() < deadline:
            try:
                playable = (await self.executor.call("get_playability")).get("playable")
            except RuntimeError:
                playable = False
            if playable:
                state = await self.executor.observe()
                if state.player is not None and not state.player.dead:
                    self._ready = state
                    self.event({"state": "PLAYER_READY", "at": _point(state)})
                    return
            await asyncio.sleep(0.08)
        self.event({"state": "RESPAWN_TIMEOUT"})
        raise _Finished("STOPPED", "player did not become playable before the deadline")

    async def _probe(self) -> None:
        """Ask once what the bridge can do; an optional probe that fails must not
        cost the session a round trip on every step."""
        try:
            capabilities = await self.executor.call("ping")
        except RuntimeError:
            return
        self._telemetry = bool(capabilities.get("platform_telemetry", True))

    async def _room_geometry(self, room: str) -> dict | None:
        if room in self._geometry:
            return self._geometry[room]
        try:
            geometry = await self.executor.call("get_room_info")
        except Exception:  # noqa: BLE001 - geometry is a hint, not a requirement
            geometry = None
        self._geometry[room] = geometry
        return geometry

    async def _room_entities(self, room: str) -> list[str] | None:
        """Entity type names in the room, cached per room.

        Only the types are kept: they decide which mechanic hints the planner is
        sent, and the prompt is the scarce resource. A room that cannot be
        scanned simply gets no hints and the model falls back on the probes it
        can still ask for.
        """
        if room in self._entities:
            return self._entities[room]
        try:
            payload = await self.executor.call("get_entities", whole_room=True)
        except Exception:  # noqa: BLE001 - hints are optional
            payload = None
        rows = (payload or {}).get("entities") or []
        types = sorted({str(row.get("type")) for row in rows if row.get("type")})
        self._entities[room] = types[:24] or None
        return self._entities[room]

    async def _platforms(self) -> dict | None:
        try:
            return await self.executor.call("get_platforms")
        except Exception:  # noqa: BLE001
            return None


def _point(state) -> list[float] | None:
    if state is None or state.player is None:
        return None
    return [round(state.player.x, 1), round(state.player.y, 1)]


async def _latest(queue: asyncio.Queue, item) -> None:
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    await queue.put(item)
