# Acceptance checklist

Compilation is not proof of successful game control.

1. Launch Everest and verify CelesteAgentBridge loads, localhost:32123 responds to ping.
2. Observe player coordinates/speed while manually moving. Missing player must be null.
3. Run ten right frames three times from the same manually established starting point; record before/after and deviations.
4. Verify right+jump, diagonal dash, grab, empty-button release and consecutive held jump semantics.
5. Disconnect during a long action; verify release. Test stop from another client.
6. Test pause, death, respawn, room transitions and leaving gameplay.
7. Check reset_room reloads the current room (not a snapshot restore).
8. Run a bounded model trial using either `--mode codex` with ChatGPT login or `--mode gpt` with API credentials. Inspect `logs/decisions.jsonl`.

Still unverified: live Mod loading/input timing, grab-setting interactions, real room completion and GPT API trial. The local protocol assumes trusted clients. Error codes are coarse. No screenshot/geometry observation yet. Updates continue while GPT thinks; no deterministic replay claim.

Verified 2026-11-15: real bridge loaded; ping/state succeeded; idle sequence executed 1 frame; right sequence executed 10 frames, x 58 -> 70, vx 0 -> 90. Python CI passed (12 tests). Remaining checklist items still pending.

See live-validation-2026-11-15.md: jump, climb, natural death/respawn and room transitions now observed successfully. Dash tutorial is blocked by frozen-level eligibility. This supersedes the earlier pending status for those four tested cases.

On 2026-11-16, `python -m agent.main --real --mode codex --steps 1` passed the
single-action closed loop in room 6b on Everest bridge 0.2.3. Codex planned
15 frames of right+jump and the bridge reported exactly 15 completed frames.
The player moved from (2016, -1128) to (2037, -1153), within the predicted
x range of 2035–2065. The decision log recorded `verified: true` under
execution ID `987dbe84-8b09-426d-b778-e01dae776b70`. The trial returned
`STOPPED` at the one-step limit with the room still incomplete. The direct
API mode and room completion remain unverified.

On 2026-11-16, a three-step Codex trial after resetting room 6b to its entrance
at (2016, -1128) reached x=2091 at the final execution boundary. All three
sequences completed and all three final x values passed their predicted ranges.

| Step | Input | Executed frames | Start x | End x | Predicted x |
| --- | --- | ---: | ---: | ---: | --- |
| 1 | Right + jump | 18 | 2016 | 2042 | 2016–2046 |
| 2 | Right | 12 | 2047 | 2062 | 2056–2063 |
| 3 | Right + jump | 18 | 2065 | 2091 | 2087–2095 |

The run returned `STOPPED` at its three-step limit. The game kept updating
while Codex planned: the second plan referred to the jump's descent, but its
execution started on the ground. The decision log records execution boundary
states, so it does not independently confirm the exact observation sent to
the model. No action stalled or missed its predicted x range in this trial,
so recovery from prediction errors and full room completion remain unverified.
The three entries in `logs/decisions.jsonl` start at execution frame 122715.

On 2026-11-16, the stale-plan guard passed a controlled timing trial on the
real 6b bridge. The test substituted two fixed planner replies to make the
first response arrive after landing. The first plan observed the player in
the air at frame 144005. At frame 144026 the player was grounded, so the
guard logged `STALE_PLAN` with `ground_state_changed` and skipped execution.
The next plan used the grounded observation at frame 144027 and completed
12 right frames, moving x from 2016 to 2031 with `verified: true`.
Both planning and pre-execution observations appear in the decision log.
Rejected plans consume the existing step budget. The test exercised real
physics and bridge inputs with controlled planner responses.

The 2026-11-16 repeat used three real Codex replies on the live 6b bridge.
Plan 1 observed the player airborne at frame 149168 and was rejected after
landing at frame 156351. Plan 2 used grounded frame 156352, completed all
12 right+jump frames, and reached x=2033 from x=2016 within its predicted
range of 2025–2035. Plan 3 again observed the player airborne and was rejected
after landing. Both rejections logged `ground_state_changed` and issued no
inputs. The run returned `STOPPED` after consuming its three-step budget.
The last observed grounded position was (2038, -1128), with 6b incomplete.

The 6b full-room trial on 2026-11-16 did not pass. An initial unassisted
Codex route reached x=2081 in the air and stopped when the player temporarily
disappeared. The retry waited for respawn and gave real Codex the previously
verified 120-frame entrance route as an advisory hint. Across six plans,
that route landed on the first ZipMover twice. A jump toward the next
ZipMover was interrupted by death at x=2171, followed by a confirmed
respawn. Two plans were blocked because the platform carried the player
beyond the permitted position drift while Codex planned. The final plan
executed 24 idle frames to ride the platform. The bridge reported a live
player at (2158, -1152), still in 6b with `completed: false`, after the
budget ended. The decision log entries start at planning frame 177117.

The local TAS action prior was then tested with real Codex from the 6b
entrance and a six-plan limit. The run ended `FAILED` after four executed
plans. Their action-end x coordinates were 2092, 2089, 2083 and 2106, but
each settled observation returned to (2016, -1128). The first and fourth
passed their immediate x-range checks without confirming a landing.
The bridge still reported room 6b incomplete at frame 313518. No verified
room-specific route was supplied in this run, so this does not isolate
the effect of the action prior from the earlier route-assisted trial.

The 1A TAS route replay through room 6b passed the room-exit check on
2026-11-16. After loading `data/1A-through-6b.tas` in Celeste Studio, the live
bridge observed the player alive and grounded in 6c at (2340, -1304),
frame 460264. This verifies the recorded TAS route through 6b. The Codex
planner's own full-room trial remains separate.

The execution contract and the collaboration path were both run against bridge
0.3.2 in room 7 on 2026-11-17, from a standing start at (2600, -1480).

Execution contract:

- A contract whose `pre.x` was 100px away from the player returned `REJECTED`
  with `reason=position_changed`, `executed_frames=0`, no `get_execution`
  record, and the player still at (2600, -1480). No input reached the game.
- `right:60` with `Abort(timeout_frames=8)` returned `ABORTED`, `reason=timeout`,
  `at_frame=8`, `executed_frames=9` in 0.187s of wall time. The extra frame is
  the stop request itself: it is dequeued on the next `Engine.Update`, so one
  frame always lands after the decision. Expect `executed = at + 1`.
- `right:40` into the wall at x≈2624 returned `ABORTED`, `reason=stalled`,
  `executed_frames=36` (24 frames of travel, then 12 still frames).

That run also exposed a defect that the stubs could not: `get_execution`
returns no `executed_frames` while a job is running -- the field is written only
by `Finish` -- so a watchdog reading it saw zero forever and neither
`timeout_frames` nor `stall_frames` could ever trip. The executor now derives
the count from the room frame counter (`state.frame - start.frame`), which
advances on the same `Level.Update` that applies one input set.
`tests/test_executor.py::FrameOnly` reproduces the bridge's shape.

Bridge 0.3.3 closes the hole at the source instead: the running record carries
`executed_frames` and is rewritten on every tick, so the count is exact and the
room clock becomes a fallback for older bridges. The client prefers the reported
value whenever the key exists (`tests/test_executor.py::ClockAhead` pins that
precedence). Loaded 2026-11-17 and confirmed by `ping` reporting
`mod_version: 0.3.3`:

- A raw 30-frame run polled `executed_frames` at 0 → 9 → 14 → 19 → 24 → 29 while
  the job was still running, so the field is live rather than post-mortem.
- The same run showed why the clock fallback was loose: the start observation was
  frame 1014 and the job was accepted at frame 1017, three ticks later (`save_state`
  and `execute_frames` each take one). Derived counts were therefore biased high
  by however many calls preceded execution.
- `right:60` with `Abort(timeout_frames=8)` returned `ABORTED`, `at_frame=9`,
  `executed_frames=10`. The count is exact, but the decision lands on a sample:
  each poll costs a `get_state` and a `get_execution`, and both are serviced on
  frame boundaries, so the executor sees every other frame and reports the first
  one at or over the budget. Expect `at <= budget + 2` and `executed = at + 1`.
- The stall case reports the same way: `at_frame=37`, `executed_frames=38`
  against `stall_frames=12` after 24 frames of travel.
- The collaboration path is unchanged by the upgrade: three steps, all `LANDED`
  at 14 frames, `rejected=0`, `superseded=0`, zero pause calls.

Collaboration path, four steps with a 1.2s scripted planner:

- Every `VERIFY` carried the id of the `ANCHOR` before it; all four were
  `LANDED` at 14 frames.
- `pause_game`/`resume_game` were never called, and the room frame counter
  advanced from 72017 to 72486 across the run: the game kept running while the
  planner was thinking.
- `SESSION` reported `rejected=0`, `superseded=0`, `freezes=0`.

Unstable-world branches, forced with `still_for=0.8 --anchor-timeout 0.08`:

- Default `--on-unstable wait` ended on `NO_ANCHOR` with zero pause calls.
- `--on-unstable freeze` paused once, resumed before the first frame, and ran
  both steps to `LANDED` with 0.25s frozen. Pause and resume each happened
  exactly once.

A real DeepSeek run (`--collab --steps 2`, no exit room) reported `anchors=3`,
`steps=2`, `superseded=2`, `rejected=0`, `freezes=0`. The log reads as designed:
a 68-frame plan missed its band, the model was still thinking when she drifted
off and died, its plan arrived after the respawn anchor and was dropped unrun,
and the next plan died mid-step. `SUPERSEDED` cost frames but no rejection; a
plan that arrives after a death is wasted work, not a hazard.

With that established, the collaboration promise is checkable from the decision
log without watching the screen:

1. Every executed step is preceded by an `ANCHOR` event and followed by a
   `VERIFY` event carrying the same `anchor` id.
2. `STALE_PLAN` never appears, and `SESSION.rejected` is zero. A non-zero value
   means the anchor promise broke and should be treated as a defect, not a
   retry.
3. No `FREEZE` event appears in a room with static floors: the game must keep
   running while the model thinks.
4. `SUPERSEDED` is the only expected accounting entry, and `SESSION.standby_seconds`
   stays small for a player standing still.
5. On a moving platform, the run ends with `NO_ANCHOR` (default) or falls back to
   `FREEZE` with `--on-unstable freeze`; it must not spin.

Route replay was exercised live in room 7 on 2026-11-17 with a scripted planner
standing in for the model (1.0s per call):

- Step 1 came from the planner. Steps 2 and 3 reported `goal: replay learned-1`
  with `latency: 0.0`, all three `LANDED` at 14 frames, and the model was called
  exactly once.
- The promoted route recorded `start: [2600, -1480] -> end: [2600, -1480]`,
  `hits: 2`, `misses: 0`, `origin: session`.
- The first run showed `hits: 1`: the last step's outcome had no next plan
  waiting for it, so it was never drained into the book. `TeamSession.run` now
  drains once on the way out, which is why the second run counts both replays.

A replay is not a shortcut past the contract. It is sent as a normal plan with
the precondition pinned to the anchor, the success band at "last landing +- 6px"
and a frame budget of "route frames + slack", so a route that no longer fits its
ledge reports MISSED and loses a hit instead of quietly drifting.

Room 7 was cleared live on 2026-11-17, segment by segment, against the same
savestate:

- Steps 1-2 (spawn to `e3`): `restore_and_execute` ends at `(2738, -1592)` with
  `on_ground: true`, `dashes: 1`, `dead: false`. The savestate was written from
  that position so step 3 started where step 2 finished.
- Step 3 (`e3` to the exit): the run ends in room `8` at `(2719, -1704)`, resting
  on ground. The execution reports `interrupted` rather than `completed` because
  the room transition cancels the job mid-sequence -- expected, not a fault.

Two numbers from this room are worth keeping because they contradict the
reference sheet: a jump is 27px (`varJump` clamps `vy` at -105 for eleven
frames), and an up dash adds 42px. Together they are 69px, which is why the
80px between the two falling blocks cannot be crossed without the wall bounce.

### Deviation stop and traced replay, live (2026-11-19)

Run with the 0.3.4 bridge loaded (`ping` reports `expect: true`, `positions: true`,
`mod_version: 0.3.4`), standing on the floor of room 8 at `(2720, -1704)`,
`on_ground: true`, `dashes: 1`. Every step starts from the same Speedrun Tool
save slot, so the arcs are compared from an identical start rather than from
"roughly the same spot".

1. **The arc is real.** `execute_frames` with `right x20`,
   `capture_positions: true` returns `completed` after 20 frames and 20
   positions, ending at `(2747, -1704)`.
2. **A frame-7 lie stops on frame 7.** The same 20 frames are sent again with
   the captured arc, except that row 7 is moved 40px in x. The result is
   `status: deviated`, `executed_frames: 7`, and
   `deviation: {frame: 7, index: 7, expected: [2767, -1704], actual: [2727, -1704],
   offset: [40, 0], tolerance: 1.5}`. The remaining 13 frames are never sent.
3. **The check does not fire on its own.** The untouched arc, same tolerance,
   same start, completes all 20 frames with `deviation: null`. The room is
   translation-exact at this scale, so a stop here means the arc stopped
   fitting, not that 1.5px was too tight.
4. **Traced replay closes the loop.** The 20 captured frames were written as
   `logs/verify-routes.json` (`start: [2720, -1704]`, `end: [2747, -1704]`,
   20 `trace` rows) and run through `--collab --steps 1 --route-book`:
   `goal: replay verify-right20`, `latency: 0.0`, `expect: 20`, verdict
   `LANDED`, 20 frames, `planner_replays: 1`, `plan_failures: 0`,
   `kb_sections: 0` -- the model was never called.
5. **The guard protects the replay too.** Corrupting row 7 of the stored trace
   by 40px and replaying from the same start gives verdict `ABORTED`,
   `detail: deviated`, `frames: 7`, settled at `(2727, -1704)`: she stops on the
   floor instead of carrying the rest of the macro into the room. The book
   records `misses: 1`, `streak: 1`, and `end` moves to the settled
   `(2750, -1704)` of the earlier landed replay.

One gap this leaves: the replay's contract still only asks for "within 6px of
the recorded landing". The frame-by-frame promise is the `expect` array, and a
route recorded without one can still run all its frames past a changed room.

### The dash aim frame, live (2026-11-19)

Same room, same savestate, same start `(2720, -1704)` on the floor of room 8.
Each run is one `execute_frames` call with the trajectory recorder on, so the
speeds below are per-tick readings from the game, not arithmetic.

1. **The aim is read after the freeze, not on the press.** `[right+up+dash] x1`
   followed by `[right+up] x14` gives `vx, vy = (169.70, -169.70)` for nine
   consecutive `StDash` ticks -- a true up-right dash.
2. **Dropping the direction for one frame flattens it.** The same press
   followed by `[right + grab]` gives `(240, 0)`: horizontal. Nothing was
   broken; the chunk that covers the aim frame simply did not say up. This is
   the shape `agent/aims.py` now repairs, and the repaired version
   (`[right+up+dash]`, `[right+up] x1`, `[right] x14`) reproduces `(169.70,
   -169.70)` exactly.
3. **Super fires, in both halves of the window.** A jump on the first tick
   after the freeze and a jump eight ticks in both give `(260, -105)` in
   `StNormal` -- `SuperJumpH` and `SuperJumpSpeed`. The late one also has a
   dash in hand: the in-dash refill happened while she was still grounded.
4. **Hyper fires.** Crouch dash (`down+right+dash`, then `down` held into the
   jump) gives `(325, -52.5)` -- `SuperJumpH * DuckSuperJumpXMult` and
   `SuperJumpSpeed * DuckSuperJumpYMult`.
5. **The earlier "grab only climbs 3px" was geometry.** Holding grab on the
   left wall enters `StClimb`, waits out `ClimbNoMoveTime` (six ticks of no
   vertical movement), climbs 3.5 px to the top of the 8 px base and hops off
   it with `vy -120` (`ClimbHopY`). The grab channel was never the problem.

With super and an air dash both available, the right side of the pocket was
re-tried: horizontal dash, super at `2748`, jump held, up-right dash at
`-1712` reaches `(2804, -1741)` and stops against the falling block's left
face without entering `StClimb`, then falls back through the spike field. The
pocket is still not solved from here; that needs the 7 -> 7a route, not a
better input.

### Stance in the landing check (built 2026-11-19, in-game check pending)

The landing check had two postures, grounded and not, so hanging on a wall was
filed under "not" -- the same bucket as falling past the drop. `Success.stance`
adds the third one (`ground` / `climb` / `air`), and bridge 0.3.5 reports the
state machine name on `get_state` so the executor can read it.

Verified off-line (167 tests, ruff clean):

- `PlayerState.stance()` returns `climb` for `StClimb`, otherwise falls back to
  `on_ground`. A snapshot without a `state` key -- every bridge before 0.3.5 --
  parses with `state: ""` and degrades to the old two-way reading.
- A contract with `stance: "climb"` accepts a `StClimb` snapshot at the target
  coordinates and returns `not_climbing` for `StNormal` at the same coordinates.
  `stance: "ground"` returns `not_grounded` for the wall grab.
- A blind snapshot asked for `climb` returns `stance_unknown` rather than a
  guess.
- `stance: None` (the default, and what every existing route carries) changes
  nothing: the x/y bands decide, as before.
- `stance: "ground"` with `on_ground: false`, and `"air"` with `true`, are
  refused when the contract is built. `"climb"` is refused with neither: see
  the floor-level grab below.

### Stance, live (2026-11-19)

The 0.3.5 bridge was already loaded when this ran (`ping` reports
`mod_version: 0.3.5`, `player_state: true`), so no restart was needed. Same
savestate each time, room 8 on the floor at `(2716, -1704)`.

1. **The state machine now arrives.** `get_state` carries `state: "StNormal"`
   standing, and the same coordinates read `StClimb` while `left+grab` is held
   -- 60 of 78 trajectory rows, position `(2716, -1704)`, `v = (0, 0)`.
2. **A wall grab at floor level is grounded.** That `StClimb` row reports
   `on_ground: true`: the wall she is holding starts at the floor. So `climb`
   does not imply airborne, and the contract validator no longer pretends it
   does. This is the one place the live data overruled the off-line design.
3. **The landing snapshot keeps the grip.** `Finish` releases the input before
   it snapshots, so the question was whether the judged `after` still says
   `StClimb`. It does: all three runs below end with `state: StClimb` and
   `stamina 109.8` (drained by the hold), at `(2716, -1704)`.
4. **The executor judges by stance, not by the bands.** Running the same 52
   frames (`left x12`, `left+grab x40`) through `ExecutorAgent`:

   | contract | status | reason |
   |---|---|---|
   | `stance="climb"` | `SUCCEEDED` | `None` |
   | `stance="ground"` | `UNSATISFIED` | `not_grounded` |
   | `stance="air"` | `UNSATISFIED` | `not_airborne` |

5. **Holding a wall used to abort the step.** The first live run of those same
   three steps came back `ABORTED / stalled` at frame 13-14, not at 52: the
   still clock counted a grip as a plan that stopped moving, because `left`
   counted as intent to move and she was not moving. `grab` now holds the
   clock, so a step that ends on a wall reaches its landing at all. Walking
   into a wall without grab still stalls, and both shapes are pinned by tests.

STILL OPEN: `Standby.resting`, `SkillBook.match` and `LocalRetry.offer` still
require `on_ground`, so a step that ends on a wall publishes no anchor, is
never matched or retried, and the session waits out `anchor_timeout` instead of
continuing. The stance answers "was this a landing"; it does not yet answer
"can the next step be planned from up there". (Closed by 0.3.6 below: the
gates accept a climb, and the grip is kept so there is a wall to plan from.)

### Planning from a wall (built and confirmed in game 2026-11-19)

A climb landing has to survive the tick that ends the job. It does not: `Finish`
releases the virtual input, and the next `Update` drops her off the wall -- the
live check below shows `StClimb` becoming `StNormal` within 30ms of the job
ending. So relaxing the three grounded-only gates alone would have produced an
anchor that can never fire where it matters.

Bridge 0.3.6 adds `hold_after`: the buttons a finished job keeps pressed. The
executor derives it from the contract, `["grab"]` when the landing promises
`stance: "climb"`, and the route book records `end_stance` so a replayed wall
route carries the same promise.

Verified off-line (178 tests, ruff clean):

- `Standby.resting` accepts a wall grab and still rejects a slow fall. A
  `StClimb` snapshot anchors after the quiet window; the same snapshot with no
  state name (a bridge that cannot see walls) stays `SETTLING`.
- `SkillBook.match` and `LocalRetry.offer` accept a climb anchor and still
  refuse an airborne one, so a route can start on a wall instead of being a hit
  that is never used.
- `note()` promotes a step that ended on a wall, records `end_stance`, and
  still refuses one that ended mid-fall. A landing updates the stance; the
  field round-trips through the file, and a row written before it existed loads
  with `end_stance: None` and is judged by the bands.
- `ExecutorAgent._payload` asks for the grip only when the contract's stance is
  `climb`; a `ground` or unset stance sends an empty list, which the bridge
  reads as "release everything".
- End to end through the team session with a stub executor that keeps returning
  a `StClimb` snapshot: the second anchor is published, the model is asked a
  second time, and its context carries `footing.stance == "climb"`.

The client also ran a climb step against the still-loaded 0.3.5 bridge: no
error, the extra parameter is ignored, and the step behaves exactly as before
(`SUCCEEDED / climb`, `UNSATISFIED / not_grounded`, `UNSATISFIED /
not_airborne`). So the two sides deploy in either order and only the restart
turns the holding on.

To check after restarting Celeste:

1. `ping` reports `mod_version: 0.3.6` and `hold_after: true`.
2. A step ending in `left+grab` with `Success(stance="climb")` leaves her in
   `StClimb` after the job, not `StNormal` -- and stays there for a second.
3. The session publishes a second anchor from that state instead of waiting out
   `anchor_timeout`, which is what step three was for.

#### In game, after the restart

Celeste was restarted onto 0.3.6 and `ping` answered
`mod_version: "0.3.6"`, `hold_after: true`, `player_state: true`. All three
checks ran against the same save, room 8, in the pocket at the bottom left
(the one that has no exit; the geometry is a 16px wall stub at `x=2712` and a
taller wall at `x=2704` above it).

1. **The grip survives the job.** `execute_frames` with
   `left+grab x2, left+up+grab x10` and `hold_after: ["grab"]` reported
   `completed / 12`, and `after` was `StClimb`, `on_ground: false`, at
   `(2716, -1705)`. Sampled every 100ms for 1.2s she stayed `StClimb` at
   `(2716, -1705)` throughout, stamina draining 108.5 -> 94.3. The same wall
   with `hold_after: []` read `StClimb` in `after` and `StNormal` 30ms later --
   the exact 30ms drop this feature exists to remove.
2. **The session plans from the wall.** A 20-frame route
   (`left+grab x2, left+up+grab x18`, recorded live from `(2709, -1712)`,
   ending `StClimb` at `(2708, -1722)`) was put in the book with
   `end_stance: "climb"` and replayed by the real `ExecutorAgent` through
   `TeamSession`, `anchor_timeout` left at 8.0s:

   | t (s) | event |
   |---|---|
   | 0.33 | `ANCHOR 1` at `(2708, -1712)`, `still_for 0.23` |
   | 1.09 | `VERIFY 1` `LANDED`, `verified: true`, 20 frames, no deviation |
   | 1.11 | `ANCHOR 2` at `(2708, -1723)`, `still_for 0.23` |
   | 1.30 | `VERIFY 2` `LANDED` |

   The second anchor was published 20ms after the landing was verified. The
   session reported `anchors 2, standby_seconds 0.2, longest_standby 0.1` --
   not 8.0 -- and `planner_replays 1`, so the wall route was chosen by the book,
   not by the model. The one context the model was given (`ANCHOR 2`) carried
   `state: StClimb`, `on_ground: false` and `footing.stance: "climb"`.
3. **Release still drops her.** The step planned from that anchor
   (`grab x6`, no stance in its contract) sent an empty `hold_after`, and she
   was back on the pedestal at `(2708, -1712)`, `StNormal`, stamina refilled to
   110, when the session ended. The grip is a property of the promise, not of
   the button.

STILL OPEN: only a replayed route can promise `climb`. `Plan` has no stance
field, so a fresh model plan builds its contract through
`StepContract.for_plan`, whose `Success` carries an x band and no stance --
`_payload` then sends an empty `hold_after` and a plan that happens to end on a
wall drops her. Closing that needs a way for a plan to declare its landing
posture, which is a planner question, not a bridge one.

## Native CelesteTAS playback: room 8 is passable (2026-11-19)

`CelesteTAS 3.47.1` was already loaded -- it and `SpeedrunTool 3.29.2` sit in
`Mods` as zips and Everest unpacks them into `Mods/Cache`, so nothing had to be
installed. The mod takes `--tas <file>`, which settles the room from the other
side: let the mod play the reference run and watch the bridge.

Two launching details worth keeping:

- Direct `Celeste.exe` exits in under a second unless `steam_appid.txt`
  (`504230`) sits next to it -- `SteamAPI.RestartAppIfNecessary` hands off to
  Steam and Steam does not relaunch it.
- `console load 1` in a TAS file loads chapter 1 at its **start**, not at the
  saved checkpoint. A file trimmed to `#lvl_8` therefore plays its room-8
  inputs in room 1; the whole file has to be played.
- Playback starts on its own on the `--tas` path; no Studio needed.

Result: rooms 1 -> 8b in 1759 frames, no death, chapter finished (frame 2995,
no level loaded). Raw samples in `logs/tas-native.json`; 135 of them land in
room 8, entered at `(2717, -1672)` and left at `(3017, -1820)`.

| frame | x, y | state | vx, vy |
|---|---|---|---|
| 1624 | 2717, -1672 | `StNormal`, rising, 36 frames of door freeze | 0, -105 |
| 1672 | 2731, -1704 | `StDash`, 11 frames, **on the floor** | 240, 0 |
| 1683 | 2777, -1705 | `StNormal`, hyper out of that dash | **325, -52.5** |
| 1710 | 2897, -1734 | `StDash`, up dash against the right wall | 0, -240 |
| 1738 | 2938, -1793 | `StNormal`, hyper again off the ledge | **325, -52.5** |
| 1759 | 3017, -1820 | leaves for 8b | 280, -105 |

So the pocket is not a dead end: dash along the floor, hyper out of it, one more
jump, up dash on the right wall, land on the ledge at `y = -1792`, dash again,
hyper, and the exit is to the right.

The replay in `agent.tas_play` died at frame 41 because of how `Z` was read.
Aligning the samples against the file (observed frame - 34 = TAS row frame, from
the 325 super and the up dash) puts the dash three frames *into* the 14-frame
`14,R,Z` block, not on its first frame: `Z` is a crouch-dash mode, so it ducks
first and dashes once the duck is on the ground. Handing `down+dash` to the
block's first frame while she is still airborne turns it into a down-diagonal
dash, and the jump that follows leaves at 183 px/s instead of 325 -- five pixels
short of clearing the spikes. The fix, if we want our own replay to pass, is a
duck lead-in that keeps the frame count.

## Duck lead-in, and what it cannot fix (2026-11-19)

`agent.tas_play` now takes `--duck-lead N`: a `Z`/`V` row is split into `N`
frames of `down` alone followed by the remaining frames of `down+dash`, so the
dash lands deeper in the row and the section keeps its frame count. Room 8 is
still 147 frames and 22 rows become 24.

The reference run says the true rule is "dash on the first frame she can
crouch", not a fixed offset. In `logs/tas-native.json` the first `14,R,Z` block
opens at f1662, she touches the floor at f1671, and `StDash` appears at f1672 --
the eleventh frame of a fourteen-frame block. `Z` holds `down` and defers the
dash; the bridge has no conditional input, so `N` remains a guess (0 restores
the old behaviour).

Live matrix from the pocket (`reset_room`, one call per lead, no trace):

| lead | status | frames | ends at | dead |
|---|---|---|---|---|
| 0 | interrupted | 41 | (2798, -1704) | yes |
| 3 | interrupted | 33 | (2793, -1704) | yes |
| 6 | interrupted | 34 | (2790, -1704) | yes |
| 9 | interrupted | 38 | (2798, -1704) | yes |
| 12 | interrupted | 38 | (2793, -1704) | yes |

All five die on the same spike bed. The lead changes the timeline but not the
outcome, because the section's opening does not fit this start: row 1 is one
step and row 2 an eight-frame jump, which is what entering through the door
looks like. Standing in the pocket, that jump is airborne, so the `Z` block
becomes a down-diagonal dash no matter how late the dash press comes.

A pocket-shaped opening does work, and it is the reference move with a different
run-up (`right x10`, `right+down+dash x2`, `right+down x8`, `right+down+jump
x2`): ground dash at x2732, hyper at **x2768 with 325/-52.5**, then a clean arc
over the spike bed to the wall at x2820 -- 2820 reached, nothing touched.

That wall is where our copy stops. The reference does a *second* 325/-105 hyper
on the frame it touches the wall (f1692: `vx 290 -> 0 -> 321.7`, `vy 60 -> -105`,
which is 325 decayed by one frame of air drag). Pressing jump on contact gives us
a wall jump instead (`vx -130`); scanning the press 10..16 frames after the hyper
with and without `down` held produced no 325 in any of the fourteen runs. An
`up+dash` at that wall does climb it (y -1697 -> -1733), so the remaining problem
is the exit from the wall, not the approach.

## Taking over a `--tas` session (bridge 0.3.10)

Reaching room 8 for these tests needs `--tas`, and a finished `.tas` file leaves
CelesteTAS holding the level: `Level.Update` is never called (the bridge's frame
counter freezes), `resume_game` reports `paused: false` without unfreezing it,
and every input call is refused. `TAS.Module.CelesteTasExports.IsTasActive`
compiles to `return Manager.Running`, so `set_tas_enabled` invokes
`TAS.Manager.DisableRun()` by reflection:

```
stop {"applied": true, "detail": "TAS.Manager.DisableRun", "tas_active": false}
frame 1756 ... frame 1775 ...   # advancing again
```

Setting `CelesteTasSettings.Instance.Enabled = false` (the earlier attempt) does
nothing to `IsTasActive`; the run has to be stopped, not the settings flipped.

## The second hyper is a wall jump with `grab` held (2026-11-19)

The "325/-105" the reference does on the frame it reaches the wall at x2820 is
not a dash and not a plain jump. Pressing `jump` on contact gives a wall jump
(`vx -130`, she is thrown back onto the spike bed). Pressing **`right+jump+grab`**
on that exact frame reproduces the reference:

```
f2991  (2820,-1704) v(   0.0,-105.0) StNormal  d1   <- corner clip, speed zeroed
f2992  (2825,-1706) v( 313.0, -90.0) StNormal  d1   <- reference f1693: 321.7/-105
f2993  (2830,-1707) v( 308.7, -75.0)
```

`dashes` stays at 1: the move costs no dash, which is what lets the reference
keep one for the `up+dash` two seconds later. She clips the top-left corner of
the solid at `2824,-1704 16x16` (top covered by spikes) for a single frame, and
the grab-held jump keeps her horizontal speed instead of replacing it. The 12px
difference from 325 is the slower approach into the corner (281.7 vs 290.3).

From there the climb chain of the reference reproduces frame for frame with the
bridge's seven buttons:

| step | ours | reference |
|---|---|---|
| corner jump | (2820,-1704) -> 313/-90 | f1692 0/-105, f1693 321.7/-105 |
| hold `right+jump+grab` x17 | rise to (2895,-1728) | f1708 (2897,-1730) |
| `right+up+dash` | (2895,-1728) -> (2900,-1768), vy -240 | f1709-f1718, y -1730 -> -1766 |
| `up+jump` on the last dash frame | **(2903,-1765) v(170,-160)** (wallbounce) | f1719 (2903,-1769) v(170,-160) |

## What still separates us from the exit

The wallbounce lands the reference on the jump-through at `2904..2912, y-1792`
(f1731 x2914, 2px of hitbox overlap). We land at x2920..2923 and fall past it
into the spikes. The surface at -1792 is solid from 2880 to 2904 and one-way
from 2904 to 2912, so the landing needs a hitbox centre in `(2900, 2916)`.

Every lever we have is measured against that:

- **air braking does not exist above run speed.** `left` held for 2, 6 and 20
  frames after the bounce leaves the decay at exactly -10.8/frame, identical to
  no input. `right` is the only direction that changes anything (it holds 165.7
  instead of 159.2).
- **the bounce x is pinned by the wall.** The block face is at 2896, so the
  earliest possible bounce is x2900 (`up` for 7 frames instead of 9); that lands
  at 2920. Bouncing on the last dash frame lands at 2923.
- **the jump cannot be downgraded.** Pressing it 1..5 frames later, or with
  `left`/`down`/`grab`/`right` held, always yields 170/-160. A plain 130 wall
  jump would drift ~13px and land on the pad, but she is inside the wallbounce
  window for all of it.

The reference shaves ~9px with a horizontal drop we cannot reproduce: f1722 and
f1723 lose **50.8 px/s each** (`148.3 -> 97.5 -> 46.7`) while `vy` is clamped to
-105, and those are the two frames where its input is `L,K,G`. None of the seven
buttons the bridge exposes produces that drop, and it is not a collision -- the
tiles at (2910,-1776) are empty and the pads nearby are one-way. The row after it
is `L,J,G`, so `K` is a key CelesteTAS knows and the bridge does not.

What is left is 5px on one landing, so the next things worth measuring are the
pad itself and the exits above it. The crumble at `2904..2928, y-1736` is the
only refill inside this climb; it sits 56px below the pad and an `up+dash` lifts
44, so it cannot substitute for landing there. The chimney at the top (`9z`,
x2904) is 8px wide, so climbing through it needs the hitbox centred at exactly
2908 -- tighter than anything this chain has attempted.

## x2920..2923 is a crumble, and it refills the dash (2026-11-19)

The "we land 5px past the pad and fall" reading was wrong about what is under
her. `get_collision` reports a `CrumblePlatform` at `x2904..2928, y-1736`, and
the wallbounce arc lands on it dead centre:

```
f72467 (2923,-1736) v(0, 160)  g1 d0   <- touches down
f72468 (2923,-1736) v(0,   0)  g1 d1   <- dash refilled by the landing
```

Twenty frames of standing on it change nothing: it only crumbles once she
leaves. That makes the pocket's right-hand route finishable without the pixel:

| step | input | result |
|---|---|---|
| wallbounce, then neutral | `_:34` | lands on the crumble, `(2923,-1736)`, `dashes 1` |
| jump, then up-dash, drift left | `jump:10` `up+dash:1` `up:12` `left:36` | lands on the `2904..2912` jump-through at **`(2908,-1792)`, `dashes 1`** |
| dash, then jump with `down` held | `right+dash:1` `right+down:2` `right+down+jump:24` | **`(2920,-1793) v(325,-52.5)`**, the reference's hyper |

The jump between the two platforms is 56px up, which no single jump reaches --
it is a jump (18px of it, with the key held) plus an up-dash (44px). The crumble
is what pays for the dash, so the sequence costs nothing the route did not
already have.

## The last 4px: the arc, not the landing

From `(2908,-1792)` the hyper reproduces exactly and still stops at the wall:

```
f111026 (2920,-1793) v(325,-52.5)   <- reference f1737 (2932,-1792) v(325,-52.5)
f111040 (2988,-1779) v(264, 157)    <- reference f1749 (2988,-1804) v(0,-105)
```

The wall at `x2992` is solid from `y-1808` down, so the exit at
`x3016, y-1840..-1816` has to be crossed above `-1808`. We peak at `-1804`
(4px short) and arrive at the wall already falling; the reference arrives at
`-1804` still *rising* at `-105` and slides up the face into the doorway.

Its `vy` stays at `-52.5` for twelve frames regardless of how long the jump is
held (10, 12, 24 and 26 frames all peak at `-1804`), so the difference is not
"hold jump longer". Holding the jump from the pad does produce twelve
gravity-free frames (`f113593`..`f113601`: `vy -105` unchanged), which is the
same window; the reference simply enters the hyper 7px further right (it lands
at 2915, we land at 2908) and spends the window clearing the corner instead of
reaching it.

## After a death, plan from the last frame she stood on

A candidate that dies used to cost the whole branch: the save slot stayed at
the entrance, so every later attempt replayed the same opening. The search now
rolls the root forward instead (`agent/checkpoint.py`, wired into
`agent/live_search.py`):

- On a failed attempt, take the deepest sample whose stance is `ground` or
  `climb` -- a wall grip counts, an airborne frame and a death do not.
- Replay only root -> that frame, confirm it reproduces the frame and ends
  standing, then `save_state`: the slot *is* the checkpoint.
- Restart the beam from there. `--no-checkpoint` restores the old behaviour,
  `--max-checkpoints` caps how far one route may walk.

The target is frozen in world coordinates before the first rollback, so `dx/dy`
keep meaning "from where the search began". `best` is dropped on a rollback
because the recorded prefixes are root-relative and can no longer be replayed.

Measured cost in room 8: the pocket-to-pad route is 153 frames; with the slot
parked on the pad each ending costs 40-55 frames, and `logs/lab.py` /
`logs/lab_room.py` run them without touching the base at all.

Deterministic coverage: `tests/test_live_search.py` drives a one-dimensional
fake bridge with a death line, and asserts that the second attempt starts at
the checkpoint (x=5) rather than at the entrance (x=0), that `--no-checkpoint`
keeps every start at 0, and that a trace with no landing is never promoted.

## Room 8 is behind us: the pocket exits to 8b (2026-11-19)

The last 4px of the reference hyper turned out not to be needed. The exit is
reached from the jump-through at `2904..2912, y-1792`, and the run that finally
crossed `x3016` ended in room 8b:

```
f131041 x3008 y-1808  d1      <- standing on the far side of the pit
right+dash:1 -> x3015, still StDash
settled: room 8b, (3020,-1808)
```

Room 8's respawn point is the pad itself (`reset_room` -> `(2908,-1792)`,
`dashes 1`), so every attempt after that costs no opening at all.

## Room 8b, solved end to end (2026-11-19)

8b is two zip movers and a 16px shaft. The map (`get_tiles`, runs in world x):

```
-1848..-1840  #3088..3184   a beam with two legs, gap 3120..3152
-1832..-1816  #3096..3120   #3152..3184   #3232..3264
-1808         #3016..3048   #3096..3104
-1792         #3016..3072                 <- the ledge you spawn on
-1760/-1752   Z3152..3184 (32x16)  Z3240..3288 (48x16)
-1816..-1856  ^3264..3272  ^3288..3296     the shaft between them
-1864..-1888  #3288..3336   solid wall, climbable
-1880..-1904  shaft widens to 3272..3304    up exit at 3272..3296
```

Both movers zip 48-56px **up** when touched and then return, on their own,
about every second, so the ride can be re-taken without leaving the block.

### Getting onto the second mover

```
right:36                 walk off the ledge at 3076
right+dash:1  right:24   dash into the pit, land on mover 1 at (3155,-1760)
_:22                     let it start rising
right+dash:1  right:2    dash, then on the dash's tail...
right+jump:2             ...jump -> super, v(260,-140)
right:34                 fly right, land on mover 2 near (3274,-1752)
```

Landing anywhere left of `3264` is fatal: the column `3232..3264` (bottom
`-1816`) crushes her when the mover tops out at `-1808`. `3274` is the spot:
the shaft is 16px wide and her hitbox is 8, so the centre has to sit in
`3276..3284`; the spike hitboxes are only 3px wide (`3264..3267` and
`3293..3296`), so 3274 clears them.

### The shaft

Waiting for the mover to reach `-1808` and then:

```
up+jump:19            apex (3274,-1835), 27px above the ride height
right+up+dash:1       diagonal up-dash through the widened part
up:10                 drift
right+grab:20  up+grab  grab the solid wall face at 3304, climb
```

That lands her on the step at `(3300,-1872)` (`#3288..3336` top `-1872`).
From there the exit is 32px up and the room has no refill, so:

```
left:8               to 3290, inside the 3272..3296 exit band
up+jump:1  up:8      jump
up+dash:1  up:14     up-dash through the ceiling gap
```

-> `settled room 9 (3287,-1941)`, alive, `dashes 1`.

Two lessons the probes paid for:

- The mover phase has to be **observed**, not counted: it re-triggers by itself
  while she stands on it, so a fixed wait drifts. `logs/climb.py` polls
  `get_entities` until the tall mover reads `y <= -1806` and only then sends the
  jump.
- `logs/lab_pad.py` (reset + ending) and `logs/lab_slot.py` (SpeedrunTool slot +
  ending) make the two iteration styles cheap: 8b took ~30 trials because the
  slot could sit on the mover.

## Room 9 is a new problem

Entered at `(3287,-1928)` on a jump-through. The exit is the right edge at
`y-2064..-2048`, 136px up. The obvious line -- grab the left wall at 3272 and
climb -- **runs out of stamina at `-2014`**: climbing costs ~45/s against 110
stamina, so ~86px is the ceiling and she slides back to the floor. The wall is
a red herring above that height; the reference spends its stamina in short
bursts and dashes between `3312..3336` and the right-hand ledges instead.
