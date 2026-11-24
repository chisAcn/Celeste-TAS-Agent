"""The search must restart from where she stood, not from the room entrance.

A fake bridge keeps this deterministic: one dimension, one death line, and a
save slot that can be inspected. What is being checked is the rollback rule,
not the game.
"""

import asyncio

import pytest

from agent import live_search


class World:
    """One axis, a death line, and a frame counter that never rewinds."""

    def __init__(self, x=0.0, die_at=6.0):
        self.x = float(x)
        self.die_at = die_at
        self.frames = 0

    def copy(self):
        clone = World(self.x, self.die_at)
        clone.frames = self.frames
        return clone

    @property
    def dead(self):
        return self.x >= self.die_at


def sample(world):
    return {
        "player": {
            "x": world.x,
            "y": 0.0,
            "vx": 0.0,
            "vy": 0.0,
            "on_ground": not world.dead,
            "dashes": 1,
            "stamina": 110.0,
            "dead": world.dead,
            "state": "StNormal",
        },
        "room": {"name": "fake", "completed": False},
        "platform_sample": {"total": 0, "platforms": []},
        "frame": world.frames,
    }


class FakeSlot:
    def __init__(self):
        self.world = World()  # what save_state holds
        self.live = World()  # what the game is showing
        self.starts = []


class FakeClient:
    def __init__(self, slot):
        self.slot = slot

    async def call(self, method, **params):
        if method == "ping":
            return {"restore_and_execute": True, "tas_active": False}
        if method == "get_state":
            return sample(self.slot.live)
        if method == "save_state":
            self.slot.world = self.slot.live.copy()
            return {"accepted": True}
        if method == "load_state":
            return {"accepted": True}
        if method == "clear_state":
            return {"saved": False}
        raise RuntimeError(method)


class FakeTools:
    """Restores the slot, then applies the action list to a copy of it."""

    def __init__(self, slot):
        self.slot = slot

    async def restore_and_execute(
        self, execution_id, trace_frames, trace_platforms, actions
    ):
        world = self.slot.world.copy()
        self.slot.starts.append(world.x)
        before = sample(world)
        checkpoints = []
        for action in actions:
            for _ in range(action["frames"]):
                if not world.dead:
                    world.frames += 1
                    if "right" in action["buttons"]:
                        world.x += 1.0
                checkpoints.append(sample(world))
        self.slot.live = world
        return {
            "status": "completed",
            "executed_frames": len(checkpoints),
            "before": before,
            "after": sample(world),
            "checkpoints": checkpoints,
            "planned_frames": len(checkpoints),
        }


@pytest.fixture
def slot(monkeypatch):
    slot = FakeSlot()
    monkeypatch.setattr(live_search, "ToolRegistry", lambda client: FakeTools(slot))
    return slot


def run_search(slot, **overrides):
    options = dict(
        dx=10.0,
        dy=0.0,
        radius=1.0,
        width=1,
        depth=1,
        budget=5,
        frames=6,
        own_slot=False,
    )
    options.update(overrides)
    return asyncio.run(live_search.search(FakeClient(slot), **options))


def test_a_death_moves_the_start_to_the_last_frame_she_stood_on(slot):
    report = run_search(slot)
    assert report["rollbacks"] == 1
    assert report["root_frames"] == 5  # died on the 6th frame, at x=6
    assert slot.world.x == 5.0  # the slot now holds the checkpoint
    assert slot.starts[0] == 0.0  # the first attempts ran from the entrance
    assert 0.0 in slot.starts[1:]
    assert slot.starts[-1] == 5.0  # after the rollback, nobody did


def test_without_the_rule_every_attempt_restarts_from_the_entrance(slot):
    report = run_search(slot, checkpoint=False)
    assert report["rollbacks"] == 0
    assert set(slot.starts) == {0.0}


def test_the_rule_keeps_its_budget_and_stops_after_max_checkpoints(slot):
    report = run_search(slot, budget=6, max_checkpoints=1)
    assert report["rollbacks"] <= 1
    assert report["evaluations"] <= 6


def test_a_candidate_that_never_lands_is_not_a_checkpoint(slot):
    slot.world.die_at = 0.5  # dead before the first frame ends
    report = run_search(slot, budget=2)
    assert report["rollbacks"] == 0
    assert slot.world.x == 0.0

def test_policy_prefers_rewarded_action_with_one_attempt(slot):
    options = dict(dx=3, radius=0, frames=3, checkpoint=False)
    training = run_search(slot, **options)
    from agent.local_policy import train

    report = run_search(FakeSlot(), **options, budget=1, policy=train([training]), top_k=1)
    assert report["success"]
    assert report["evaluations"] == 1


def test_exploration_preserves_top_two_and_replays_seed(slot):
    options = dict(policy={}, top_k=3, checkpoint=False, budget=1)
    ranked = run_search(slot, **options)["decisions"][0]["selected"]
    selections = []
    for seed in (0, 1, 0):
        report = run_search(slot, **options, explore_last=True, seed=seed)
        selection = report["decisions"][0]["selected"]
        assert selection[:2] == ranked[:2]
        assert len({tuple(action) for action in selection}) == 3
        selections.append(selection)
    assert selections[0] == selections[2]
    assert selections[0] != selections[1]


def test_retained_exploration_receives_a_second_step(slot):
    options = dict(policy={}, top_k=3, explore_last=True, seed=0, checkpoint=False,
                   frames=1, depth=2, width=2, budget=8)
    for retain in (False, True):
        report = run_search(slot, **options, retain_exploration=retain)
        sampled = report["decisions"][0]["selected"][-1]
        expanded = [t["actions"][0]["buttons"] for t in report["trials"] if t["segments"] == 2]
        assert (sampled in expanded) == retain
        assert report["evaluations"] == 8


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_reserved_attempt_executes_sampled_action_within_budget(slot, budget):
    report = run_search(slot, policy={}, top_k=3, checkpoint=False, budget=budget,
                        explore_last=True, reserve_last_attempt=True, seed=0)
    selected = report["decisions"][0]["selected"]
    executed = [t["actions"][0]["buttons"] for t in report["trials"]]
    assert executed == [*selected[:budget - 1], selected[-1]]


def test_continuation_value_can_keep_a_more_distant_branch(slot):
    from agent.local_policy import context, key

    state = context(sample(slot.world)["player"], (16, 0))
    policy = {key(state, [], 1): 1.0}
    for enabled, expected in ((False, ["right"]), (True, [])):
        report = run_search(slot, dx=16, frames=1, depth=2, width=1, budget=4,
                            policy=policy, top_k=3, checkpoint=False,
                            continuation_ranking=enabled)
        assert report["trials"][3]["actions"][0]["buttons"] == expected


def test_second_segment_duration_competes_within_budget(slot):
    from agent.local_policy import context, key

    state = context(sample(slot.world)["player"], (2, 0))
    policy = {key(state, [], 1): 0.5, key(state, ["right"], 2): 1.0}
    report = run_search(slot, dx=2, radius=0, frames=1, depth=2, width=1,
                        budget=2, min_segments=2, top_k=1, checkpoint=False,
                        policy=policy, second_frames=[1, 2, 2])
    assert report["success"] and report["evaluations"] == 2
    assert report["winner"]["actions"][-1] == {"buttons": ["right"], "frames": 2}
    second = report["decisions"][1]
    assert (second["hits"], second["candidates"], second["selected_frames"]) == (2, 14, [2])


def test_repeated_unscored_states_count_distinct_failed_tasks(slot):
    from agent.local_policy import repeated_unscored_states, train

    report = run_search(slot, policy={}, checkpoint=False, budget=1)
    state = tuple(report["decisions"][0]["state"])
    assert not repeated_unscored_states([report, report])
    other = dict(report, target=[11, 0])
    assert repeated_unscored_states([report, other]) == {state}
    assert not repeated_unscored_states([report, dict(other, success=True)])
    assert train([report], states_to_fit=set()) == {}


def test_reserved_third_action_fits_in_eight_evaluations(slot):
    report = run_search(slot, dx=80, frames=1, depth=3, width=2, budget=8,
                        policy={}, top_k=3, checkpoint=False, reserve_third_attempt=True)
    assert [t["segments"] for t in report["trials"]] == [1, 1, 1, 2, 2, 2, 2, 3]
