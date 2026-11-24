"""The planning agent, in its own task.

It never touches the bridge. Anchors arrive as plain data (state, room geometry,
platform sample), plans leave as plain data, and outcomes come back the same
way. That is what makes "one controller at a time" structural: the planner can
spend thirty seconds inside an HTTP request without blocking a frame, and the
executor can abort a step while the model is mid-sentence.

A plan is always bound to the anchor it was built on. If the anchor is gone by
the time the plan arrives, the plan is dropped -- not executed and judged
afterwards. Dropping is free; a wrong first frame is a death.
"""

import asyncio
import math
import time

from agent.agents.messages import (
    Abort,
    Anchor,
    PlanFailure,
    PlannedStep,
    Precondition,
    StepContract,
    StepOutcome,
    Success,
)
from agent.agents.retry import LocalRetry
from agent.agents.skills import Replay, SkillBook
from agent.aims import repair
from agent.kb import DEFAULT_BUDGET, GUIDANCE, terms_for
from agent.knowledge import MOVEMENT_REFERENCE, mechanisms_reference
from agent.protocol import ActionSequence, GameState
from agent.tas_training import action_key

PLANNING_GOAL = (
    "Use grounded intermediate landings. Check recent settled outcomes "
    "before repeating a route. An airborne x coordinate does not "
    "establish a landing. Choose the next reachable foothold using terrain and hazards, "
    "not just the exit direction. Specify expected_y, expected_stance, expected_room "
    "and min_dashes for that foothold. A failed route should change the next approach, "
    "using the failure position and sampled trace."
)


def skill_hint(skill, state):
    """Compact planner-facing view of a replay-verified room route."""
    start, end = skill.verified_start, skill.verified_end
    hint = {
        "name": skill.name,
        "frames": skill.frames,
        "successes": skill.successes,
        "failures": len(skill.failures),
        "actions": [action.model_dump() for action in skill.sequence.actions],
    }
    if start and start.player:
        hint["start"] = {"x": start.player.x, "y": start.player.y}
        if state.player:
            hint["start_distance"] = round(math.hypot(
                start.player.x - state.player.x, start.player.y - state.player.y), 1)
    if end and end.player:
        hint["end"] = {"x": end.player.x, "y": end.player.y,
                       "on_ground": end.player.on_ground}
    return hint


class PlannerAgent:
    """Plans from anchors; never blocks or reads the game."""

    def __init__(self, plan_fn, *, skills=None, exit_room=None, success_room=None,
                 action_prior=None, history: int = 3, timeout: float = 60.0,
                 slack: int = 30, position_slack: float = 2.0,
                 frozen_slack: float = 8.0, book: SkillBook | None = None,
                 replay: bool = True, expect_tolerance: float = 1.5,
                 kb=None, kb_budget: int = DEFAULT_BUDGET):
        self.plan_fn = plan_fn
        self.skills = list(skills or [])
        self.exit_room = exit_room
        self.success_room = success_room
        self.action_prior = action_prior
        self.history_limit = history
        self.timeout = timeout
        self.slack = slack
        self.position_slack = position_slack
        self.frozen_slack = frozen_slack
        self.replay_enabled = replay
        self.expect_tolerance = expect_tolerance
        self.replays = 0
        # The long tail behind MOVEMENT_REFERENCE: frame windows and setups,
        # quoted from kb/ on demand. None means the prompt stays as it was.
        self.kb = kb
        self.kb_budget = kb_budget
        self.kb_sections = 0
        self.book = book if book is not None else SkillBook()
        for skill in self.skills:
            entry = Replay.from_skill(skill)
            if entry is not None:
                self.book.add(entry)
        # Second attempts come before the model: a route that already worked
        # from within reach is cheaper than any prompt.
        self.retry = LocalRetry(self.book, slack=self.slack,
                                tolerance=self.expect_tolerance,
                                position_slack=self.position_slack)
        self.history: list[dict] = []
        self.previous_action: str | None = None
        self.plans = 0
        self.failures = 0
        self.last_error: str | None = None
        # Plans whose dash chunk declared a direction the aim frame would not
        # have seen. Repaired before sending, counted so the log shows it.
        self.aim_repairs = 0
        self.last_aim_note: str | None = None

    # -- context ---------------------------------------------------------

    def context(self, anchor: Anchor) -> dict:
        state = anchor.state
        context = {
            "state": state.model_dump(),
            "history": self.history[-self.history_limit:],
            "movement_reference": MOVEMENT_REFERENCE,
            "planning_goal": PLANNING_GOAL,
            "anchor": {
                "still_for": round(anchor.still_for, 2),
                "note": ("The world is holding still. This snapshot is what the "
                         "first frame of your plan will be applied to."),
            },
        }
        if anchor.frozen:
            context["anchor"]["note"] = (
                "The game is paused on this snapshot, so it is exact, but it resumes "
                "before your plan runs: anything moving in the room may drift by a few "
                "pixels. Plan a short step and land somewhere that absorbs that.")
        if state.player is not None and state.player.stance() == "climb":
            # A wall anchor is only real because the bridge keeps holding grab
            # for her; say so, or the plan's first chunk drops the grip and she
            # slides before the second one is applied.
            context["footing"] = {
                "stance": "climb",
                "state": state.player.state,
                "note": ("She is hanging on a wall and the bridge is holding grab for "
                         "her right now. That hold ends when your first frame runs: "
                         "keep grab in the first chunk or she starts sliding down the "
                         "wall. Climbing up costs stamina; a wall jump does not need "
                         "the grip released before the jump press."),
            }
        if self.exit_room:
            context["exit_room_goal"] = self.exit_room
            if self.success_room:
                context["success_room_goal"] = self.success_room
            context["planning_goal"] = PLANNING_GOAL
        room_skills = [skill for skill in self.skills if skill.room == state.room.name]
        if room_skills:
            context["verified_room_skills"] = [
                skill_hint(skill, state) for skill in room_skills]
            context["skill_guidance"] = (
                "verified_room_skills are replay-verified action sequences recorded in "
                "this room. When your grounded position matches a skill's start "
                "(small start_distance), prefer replaying its actions exactly instead "
                "of improvising. Chain skills through their endpoint positions.")
        if anchor.platforms:
            context["platforms"] = anchor.platforms
        mechanisms = mechanisms_reference(anchor.entities)
        if mechanisms:
            context["room_mechanisms"] = mechanisms
            context["mechanism_guidance"] = (
                "room_mechanisms are the rules for the entity types in this room "
                "only. They override a guess: a spike kills by direction of "
                "travel, a spring refills the dash. Where a number here conflicts "
                "with movement_reference, movement_reference wins.")
        if self.kb is not None:
            # Retrieved per anchor rather than per room: the same room asks a
            # different question once she is airborne with no dash left.
            excerpt, labels = self.kb.excerpt(
                terms_for(anchor.entities, state.player), budget=self.kb_budget)
            if excerpt:
                self.kb_sections += len(labels)
                context["room_knowledge"] = excerpt
                context["knowledge_guidance"] = GUIDANCE
        geometry = anchor.geometry or {}
        if geometry:
            context["room_geometry"] = geometry
            context["room_geometry_guidance"] = (
                "collision and tiles describe terrain and hazards. Check their completeness "
                "flags; missing or truncated geometry never establishes clear space. "
                "An 8-pixel tile grid is a coarse map; use collision bounds for clearance. "
                "room_geometry.bounds is the current room rectangle and "
                "room_geometry.exits lists boundary segments that open into a "
                "neighbouring room. Each segment gives the side, the target room "
                "and the axis range (from..to) of the doorway, measured in world "
                "coordinates. Route the player through one of those doorway "
                "ranges; a segment without a doorway on the target side does not "
                "lead out.")
        if self.action_prior is not None and state.player is not None:
            context["tas_action_hints"] = [
                hint.model_dump()
                for hint in self.action_prior.suggest(self.previous_action,
                                                      state.player.dashes)]
        replayable = self.book.describe(state.room.name)
        if replayable:
            context["replayable_routes"] = replayable
            context["replay_guidance"] = (
                "replayable_routes were already sent from their recorded start. When "
                "an anchor sits exactly on one, it is replayed automatically and you "
                "are not asked at all -- so these entries tell you what the runner "
                "will do without you, not what to copy. Do not approximate one from a "
                "different position: that is a new plan, and its arc will not match. "
                "A route with misses > 0 has already failed somewhere near here.")
        return context

    # -- contract --------------------------------------------------------

    def contract(self, anchor: Anchor, plan, actions=None) -> StepContract:
        """Bind the plan to the anchor, not to whatever the model wrote.

        The planner owns the success band and the frame budget; the precondition
        is always the anchor state, because that is the only thing the executor
        can actually promise. ``actions`` overrides the plan's own list when the
        dash aim had to be held into the frame the game reads it on -- the
        contract has to promise what will actually be sent.
        """
        low = getattr(plan, "expected_x_min", None)
        high = getattr(plan, "expected_x_max", None)
        expected = None if low is None or high is None else (low, high)
        return StepContract.for_plan(
            plan.actions if actions is None else actions, expected_x=expected,
            expected_y=getattr(plan, "expected_y", None),
            stance=getattr(plan, "expected_stance", None),
            room=getattr(plan, "expected_room", None),
            min_dashes=getattr(plan, "min_dashes", None),
            planned=anchor.state, slack=self.slack,
            position_slack=self.frozen_slack if anchor.frozen else self.position_slack)

    # -- bookkeeping -----------------------------------------------------

    def record(self, outcome: StepOutcome):
        """Keep a compact trace for the next prompt.

        The verdict alone is not actionable: ``ABORTED`` says the world moved
        while the plan ran, ``UNSATISFIED`` says the plan was wrong about it, and
        the frame number says where. The trace is thinned to the shape of the
        arc rather than every sample, because the prompt is the scarce resource.
        """
        report = outcome.report
        row = {
            "actions": [action.model_dump() for action in outcome.actions],
            "verdict": outcome.verdict,
            "status": None if report is None else report.status,
            "reason": None if report is None else report.reason,
            "from": None if report is None else _point(report.start),
            "to": None if report is None else _point(report.end),
            "settled": _point(outcome.settled),
            "planned": None if report is None else report.planned_frames,
            "executed": None if report is None else report.executed_frames,
            "at_frame": None if report is None else report.at_frame,
            "detail": outcome.detail,
        }
        if report is not None and report.trace:
            row["trace"] = _thin(report.trace)
        if report is not None and report.deviation is not None:
            # The frame where the arc stopped fitting is the most actionable
            # number in the whole report: it says how far the room has moved
            # away from what the plan assumed.
            row["deviation"] = {"frame": report.deviation.frame,
                                "expected": report.deviation.expected,
                                "actual": report.deviation.actual}
        self.retry.note(outcome)
        self.history.append(row)
        del self.history[:-24]
        if outcome.actions:
            self.previous_action = action_key(outcome.actions[-1].buttons)
        changed = self.book.note(outcome)
        if changed is not None:
            row["route"] = changed

    def drain(self, outcomes: asyncio.Queue) -> None:
        while True:
            try:
                self.record(outcomes.get_nowait())
            except asyncio.QueueEmpty:
                return

    def _replay_step(self, anchor: Anchor, replay: Replay) -> PlannedStep:
        """Send a known route again instead of asking the model.

        The contract is the route's promise: it begins where the anchor stands,
        and it has to land back within a few pixels of where it landed last time.
        A miss is not something to hide -- it is what demotes the route.

        If the route was recorded with per-frame positions, they ride along as
        an arc: the bridge stops on the first frame that leaves it, so a route
        that no longer fits this room costs a few frames instead of a death.
        """
        ok = Success()
        if replay.end_x is not None:
            ok = Success(
                x=(replay.end_x - 6, replay.end_x + 6),
                y=None if replay.end_y is None else (replay.end_y - 6, replay.end_y + 6),
                stance=replay.end_stance)
        contract = StepContract(
            pre=Precondition.from_state(anchor.state, self.position_slack),
            ok=ok, abort=Abort(timeout_frames=replay.frames + self.slack))
        return PlannedStep(anchor_id=anchor.id, actions=list(replay.actions),
                           contract=contract, goal=f"replay {replay.name}",
                           reason="a route that already ran starts exactly here",
                           expect=replay.expected_from(anchor.state),
                           tolerance=self.expect_tolerance)

    # -- the task --------------------------------------------------------

    async def serve(self, anchors: asyncio.Queue, plans: asyncio.Queue,
                    outcomes: asyncio.Queue, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                anchor = await asyncio.wait_for(anchors.get(), 0.2)
            except asyncio.TimeoutError:
                self.drain(outcomes)
                continue
            self.drain(outcomes)
            if self.replay_enabled:
                retry = self.retry.offer(anchor)
                if retry is not None:
                    await _latest(plans, retry)
                    continue
            # While recovering, only the retrier picks routes: replaying the one
            # that just missed from the same anchor is how a failure becomes a
            # loop.
            replay = (self.book.match(anchor.state)
                      if self.replay_enabled and not self.retry.recovering else None)
            if replay is not None:
                self.replays += 1
                await _latest(plans, self._replay_step(anchor, replay))
                continue
            started = time.monotonic()
            try:
                plan = await self._plan_latest(anchor, anchors)
                if plan is None:
                    continue
            except asyncio.TimeoutError:
                await _latest(plans, PlanFailure(anchor_id=anchor.id, kind="timeout",
                                                detail="model call exceeded the deadline"))
                continue
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - a bad plan is data, not a crash
                self.failures += 1
                self.last_error = str(error)[:200]
                await _latest(plans, PlanFailure(anchor_id=anchor.id,
                                                detail=self.last_error))
                continue
            latency = time.monotonic() - started
            try:
                actions, aim_notes = repair(plan.actions)
                if aim_notes:
                    self.aim_repairs += len(aim_notes)
                    self.last_aim_note = aim_notes[-1]
                step = PlannedStep(anchor_id=anchor.id, actions=actions,
                                   goal=getattr(plan, "goal", ""),
                                   reason=getattr(plan, "reason", ""),
                                   latency=round(latency, 3),
                                   contract=self.contract(anchor, plan, actions))
                ActionSequence(execution_id=f"validate-{anchor.id}",
                               actions=step.actions)
            except ValueError as error:
                self.failures += 1
                self.last_error = f"invalid plan: {error}"
                await _latest(plans, PlanFailure(anchor_id=anchor.id, kind="invalid",
                                                detail=self.last_error))
                continue
            self.plans += 1
            await _latest(plans, step)

    async def _plan_latest(self, anchor, anchors):
        request = asyncio.create_task(self.plan_fn(self.context(anchor)))
        replacement = asyncio.create_task(anchors.get())
        try:
            done, _ = await asyncio.wait(
                {request, replacement}, timeout=self.timeout + 15,
                return_when=asyncio.FIRST_COMPLETED)
            if replacement in done:
                await _latest(anchors, replacement.result())
                return None
            if request in done:
                return request.result()
            raise asyncio.TimeoutError
        finally:
            request.cancel()
            replacement.cancel()
            await asyncio.gather(request, replacement, return_exceptions=True)


def _thin(trace, limit: int = 6) -> list[list]:
    """First, last and an even spread of the samples in between."""
    rows = trace if len(trace) <= limit else (
        [trace[0]] + [trace[round(i * (len(trace) - 1) / (limit - 1))]
                      for i in range(1, limit - 1)] + [trace[-1]])
    return [[row.frame, round(row.x, 1), round(row.y, 1), round(row.vy, 1), row.on_ground]
            for row in rows]


def _point(state: GameState | None) -> list[float] | None:
    if state is None or state.player is None:
        return None
    return [round(state.player.x, 1), round(state.player.y, 1)]


async def _latest(queue: asyncio.Queue, item) -> None:
    """Put, dropping whatever was waiting: only the newest plan is worth running."""
    if queue.full():
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
    await queue.put(item)
