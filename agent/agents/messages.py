"""Messages exchanged between the planning and execution agents.

The contract is deliberately narrow: a few conditions the executor can check
against the live snapshot without guessing. "Is she on the ground, in this
band, with a dash left" is decidable; "did the jump feel right" is not, and a
wrong guess there costs a death rather than a re-plan.
"""

from typing import Literal

from pydantic import Field, model_validator

from agent.protocol import (
    ActionSequence,
    Deviation,
    FrameAction,
    GameState,
    Schema,
    Stance,
)

Band = tuple[float, float]


def within(value: float, band: Band | None) -> bool:
    return band is None or band[0] <= value <= band[1]


class Precondition(Schema):
    """What must hold before the first frame is sent.

    A failed precondition costs nothing: the report comes back REJECTED and no
    input reaches the game.
    """

    room: str | None = None
    on_ground: bool | None = None
    min_dashes: int | None = Field(default=None, ge=0)
    x: Band | None = None
    y: Band | None = None

    @model_validator(mode="after")
    def ordered(self):
        for band in (self.x, self.y):
            if band and band[0] > band[1]:
                raise ValueError("band minimum exceeds maximum")
        return self

    @classmethod
    def from_state(cls, state: GameState, slack: float = 2.0) -> "Precondition":
        """Pin the contract to one observed state.

        ``slack`` is not a tolerance for a stale plan; it only absorbs the
        subpixel jitter of a body that is standing still. The anchor protocol
        keeps the world still, so in practice the drift is zero.
        """
        player = state.player
        if player is None:
            return cls()
        return cls(room=state.room.name, on_ground=player.on_ground,
                   min_dashes=player.dashes,
                   x=(player.x - slack, player.x + slack),
                   y=(player.y - slack, player.y + slack))


class Success(Schema):
    """What must hold at the end for the step to count as landed.

    ``stance`` is the three-way version of ``on_ground``: hanging on a wall is
    neither standing nor falling, and a route that ends there has to say so or
    the x/y bands will call it a landing on their own. ``None`` keeps the old
    behaviour -- check the bands and nothing else -- so every route recorded
    before this field existed still replays.
    """

    on_ground: bool | None = None
    room: str | None = None
    stance: Stance | None = None
    min_dashes: int | None = Field(default=None, ge=0)
    x: Band | None = None
    y: Band | None = None

    @model_validator(mode="after")
    def ordered(self):
        for band in (self.x, self.y):
            if band and band[0] > band[1]:
                raise ValueError("band minimum exceeds maximum")
        return self

    @model_validator(mode="after")
    def consistent(self):
        """Refuse the two stances that cannot hold; ``climb`` is not one of them.

        ``climb`` says nothing about ``on_ground``: a wall grab at floor level
        is a real landing, and the 0.3.5 bridge reports exactly that shape --
        ``StClimb`` with ``on_ground`` true -- standing on the floor of room 8
        with a hand on the wall.
        """
        if self.stance is None or self.on_ground is None:
            return self
        if self.stance == "ground" and not self.on_ground:
            raise ValueError("stance and on_ground disagree")
        if self.stance == "air" and self.on_ground:
            raise ValueError("stance and on_ground disagree")
        return self


class Abort(Schema):
    """When the executor stops a step that is still running.

    These are hard limits, not tuning knobs: a plan that trips one of them is
    already wrong, and burning the remaining frames into a spike only makes the
    report less useful.

    ``diverged_px`` is off by default. Death is observed within one poll, so a
    runaway arc is already caught; aborting on a coarse model-predicted band
    would cut good plans short. Set it when a contract really needs precision,
    such as replaying a verified macro.
    """

    dead: bool = True
    timeout_frames: int | None = Field(default=None, ge=1)
    diverged_px: float | None = Field(default=None, gt=0)
    stall_frames: int = Field(default=12, ge=2)


class StepContract(Schema):
    pre: Precondition = Field(default_factory=Precondition)
    ok: Success = Field(default_factory=Success)
    abort: Abort = Field(default_factory=Abort)

    @classmethod
    def for_plan(cls, actions: list[FrameAction], expected_x: Band | None = None,
                 planned: GameState | None = None, slack: int = 30,
                 position_slack: float = 4.0, *, expected_y: Band | None = None,
                 stance: Stance | None = None, room: str | None = None,
                 min_dashes: int | None = None) -> "StepContract":
        """Build a contract for a plan that does not carry one.

        Every guard the single-loop version applied by hand is reproduced here:
        the planning state becomes the precondition within ``position_slack``,
        the plan's expected range becomes the success band, and the frame budget
        gets a slack so only a stuck execution trips it.
        """
        pre = Precondition() if planned is None else Precondition.from_state(
            planned, position_slack)
        ok = Success(x=expected_x, y=expected_y, stance=stance, room=room,
                     min_dashes=min_dashes)
        total = sum(action.frames for action in actions)
        return cls(pre=pre, ok=ok, abort=Abort(timeout_frames=total + slack))


class TraceRow(Schema):
    frame: int
    x: float
    y: float
    vx: float = 0
    vy: float = 0
    on_ground: bool = True
    dashes: int = 1


StepStatus = Literal[
    "SUCCEEDED",     # every frame ran and the success band held
    "UNSATISFIED",   # every frame ran, the game did something else
    "REJECTED",      # precondition failed before any frame was sent
    "ABORTED",       # the executor stopped it (stall, timeout, divergence)
    "DIED",          # Madeline died
    "INTERRUPTED",   # the bridge stopped for its own reasons (cutscene, pause)
]


class StepReport(Schema):
    """What the executor tells the planner.

    The distinction that matters is UNSATISFIED versus ABORTED: the first means
    the plan was wrong about the world, the second means the world moved while
    the plan was being carried out. They need different next plans.
    """

    status: StepStatus
    reason: str | None = None
    execution_id: str
    actions: list[FrameAction] = Field(default_factory=list)
    planned_frames: int = 0
    executed_frames: int = 0
    at_frame: int | None = None
    contract: StepContract = Field(default_factory=StepContract)
    start: GameState = Field(default_factory=GameState)
    end: GameState | None = None
    trace: list[TraceRow] = Field(default_factory=list)
    # Where the step left the arc it was sent with, if it was sent with one.
    deviation: Deviation | None = None
    # Per-frame positions the bridge recorded while running, which is what
    # turns a landed step into a route that can be replayed frame by frame.
    positions: list[list[float]] = Field(default_factory=list)

    @property
    def settled(self) -> bool:
        return self.status == "SUCCEEDED"


def first_action(sequence: ActionSequence, frame: int) -> list[str]:
    """Buttons held at a given frame offset, or [] past the end."""
    offset = 0
    for action in sequence.actions:
        offset += action.frames
        if frame < offset:
            return action.buttons
    return []


def drift(state: GameState, reference: GameState) -> float:
    """Largest axis distance between two observations, in pixels."""
    a, b = state.player, reference.player
    if a is None or b is None:
        return float("inf")
    return max(abs(a.x - b.x), abs(a.y - b.y))


class Anchor(Schema):
    """A snapshot the planner is allowed to plan from.

    The executor publishes one only while the world is provably still, so a plan
    built on it cannot go stale before it runs -- the state it was reasoned from
    is the state the first frame will be applied to. ``id`` is what makes the
    promise checkable: a plan carries the anchor it was built on, and the
    executor drops any plan whose anchor is no longer the current one.

    Geometry and platform samples ride along so the planner never needs the
    bridge: that is what lets it sit in its own task and think for as long as it
    likes without holding anything the executor needs.
    """

    id: int = Field(ge=1)
    state: GameState
    still_for: float = 0.0
    frozen: bool = False
    geometry: dict | None = None
    platforms: dict | None = None
    # Entity types in the room, so the planner can be told the mechanics that
    # actually apply here instead of every mechanic the game has.
    entities: list[str] | None = None


class PlannedStep(Schema):
    """A plan, bound to the anchor it was reasoned from."""

    anchor_id: int = Field(ge=1)
    actions: list[FrameAction] = Field(min_length=1, max_length=64)
    contract: StepContract = Field(default_factory=StepContract)
    goal: str = ""
    reason: str = ""
    latency: float = 0.0
    # A recorded arc to stay on. It is not a success condition: leaving it
    # aborts the step at that frame, which is cheaper than finishing an arc
    # that no longer describes this room.
    expect: list[list[float] | None] | None = None
    tolerance: float = Field(default=1.0, ge=0, le=64)

    @property
    def frames(self) -> int:
        return sum(action.frames for action in self.actions)

    @model_validator(mode="after")
    def expect_fits(self):
        if self.expect is not None and len(self.expect) > self.frames:
            raise ValueError("expect rows exceed planned frames")
        return self


class PlanFailure(Schema):
    """The planner could not produce a plan for this anchor."""

    anchor_id: int = Field(ge=1)
    kind: Literal["exception", "timeout", "invalid"] = "exception"
    detail: str = ""


StepVerdict = Literal[
    "LANDED",       # settled on the ground inside the success band
    "MISSED",       # every frame ran, the landing was somewhere else
    "ABORTED",      # the executor stopped it mid-step
    "DIED",
    "INTERRUPTED",
    "REJECTED",     # the anchor promise was broken; must stay at zero
]


class StepOutcome(Schema):
    """What the executor tells the planner about a step that just finished.

    ``settled`` is the grounded truth: a step's last input frame usually happens
    in mid-air, so the plan is judged again once gravity has finished with her.
    """

    verdict: StepVerdict
    report: StepReport | None = None
    actions: list[FrameAction] = Field(default_factory=list)
    settled: GameState | None = None
    detail: str | None = None
