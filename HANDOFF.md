# lightlaland handoff (2026-10-09)

lightlaland is a Python app (v0.2.0, stdlib HTTP server + numpy + sounddevice) that listens to a
vinyl DJ set, tracks beat/phrase/breakdown/drop/record changes, runs light scenes, and drives two
beam-curtain bars over Art-Net. The control page looks like an old lighting desk (VFD, green radar
magic sheet, rubber keys) and works on the Mac, an iPad or a phone.
See README.md for running it and how the music following works.

**Status (2026-10-09):** the bars are mounted **horizontally on the walls, ~8 ft (2.44 m) up**, not
vertically as planned. Round 2 (section 1e) rebuilt the geometry for that (tilt, not pan), added
**beam safety** (no beams below level, enforced by the engine and tested), the Mount tab for tilt
calibration, ambient scenes, 5-colour palettes and the desk-style UI. Nothing
from round 2 has been seen on the real bars yet; both bars need the Mount calibration before any
beam lights. Sections 1d and earlier describe the vertical-mount era where they differ; sections
2 and 3 are history (how the channel map was found).

---

## 1. Hardware and network

| Thing | Facts | Confidence |
|---|---|---|
| Lights | 2× 250W double-sided "beam curtain / strobe" moving bars, CH84 mode, address 001 (both set and checked by the user, 2026-10-08; A had been at A036 in a compact mode before, see 1b) | Display checked |
| Light A | "Left (yellow cabinet)", universe 0 | Which bar is on which node output is **not confirmed** |
| Light B | "Right (bookshelf)", universe 1 | same |
| Node | Art-Net→DMX, short name `ARTNET-2X`, 2 DMX outputs = universes 0 and 1, plugged into the router's LAN1 | Verified by ArtPoll |
| Node IP | `2.48.37.192/255.0.0.0` (factory auto-IP). Setting a static IP in its web UI did **not** persist after Apply + power cycle. It has a front-panel display, which may be the reliable way | Verified |
| Node quirk | Sends ArtPollReply to `2.255.255.255`. macOS drops that unless it has a 2.x alias. The node **does** accept `255.255.255.255`, so lightlaland sends ArtDMX by limited broadcast (fixture `"ip": "broadcast"`) | Verified (tcpdump, and the user says it's connecting) |
| Test Mac | MacBook Pro on Wi-Fi. Has the webcam | |
| Show Mac | Mac Studio. No mic, so it needs an audio interface fed from the mixer's booth/record out. Ethernet recommended (Wi-Fi broadcast goes at the lowest rate with no retries) | |
| Room | ~5.6 × 4.6 m, ceiling height not measured (default 2.8 m is too low, see 1f). Bars mounted horizontally ~8 ft up: A on the top wall near the left corner, B at the bottom of the right wall. Disco balls in the bottom-left corner where the curtains cross. Fogger | User, 2026-10-09 |

If a 2.x alias is still on the MacBook from earlier debugging, remove it with
`sudo ifconfig en0 -alias 2.48.37.100`.

## 1b. Probe findings, light A (2026-10-08, webcam, `tools/channel_probe.py`)

Images are in `probe/A/` and `probe/snaps/`. Sniffing the broadcast confirmed the app sends the
intended bytes, so everything below is the fixture's side.

- **Light A's display reads `A036`: DMX start address 36, not 001.** The app sent from address 1,
  so its strip colours landed on the bar's position/spin/beam channels (the "colours + spinning
  but off" the user saw).
- **It is not in 84-channel mode.** It's a compact whole-face mode (bar channel numbers, raw = +35):
  1 position, 2 spin, 3 speed (assumed), 4/5 beam dimmer/strobe, 6–9 all beams R/G/B/W,
  10 beam colour macro?, 14/15 matrix dimmer/strobe, 16–18 whole matrix R/G/B, 19/24/25 matrix
  effects or white, 26+ nothing seen. No per-segment control, so the strip scenes can't work.
  Next: switch the bar to its 84-channel mode in the menu, then rescan.
- **Position range is ~540°, not 1080°.** The matrix face points at the camera at position 0 and
  168, the beam face at 84 and 252, so one turn is ~168 steps (~2.1°/step). `geometry.py` assumes 85.
- Probe lesson: a dimmer at 0 makes every single-channel test dark, and a strobe channel > 0 makes
  the bar flash, which a median of frames deletes. Find dimmers/strobes with a leave-one-out test,
  hold them with `--base` (e.g. `--base 4=255,14=255`), and keep the max-of-frames capture.
- Camera: Claude Code's shell can't get camera permission (its bundle has no camera usage
  string). Run `tools/channel_probe.py serve` from the desktop app's Terminal panel or
  Terminal.app; the other probe commands fetch frames from it.

## 1c. Map resolved (bar A in CH84 mode at A001, 2026-10-08)

Scanned with the webcam on both faces. `fixture.py` now holds it as tables:
CH1 position (540°, 170 steps/turn, checked to the step), CH2 spin, CH3 speed, CH4–27 six beam
groups (adjacent pairs) × RGBW, CH28–51 strip row 1 and CH52–75 strip row 2 (8 positions × RGB
each, same columns), CH76–83 eight whites (middle row). Group/segment 1 is the same end on every
part. The printed chart's channel numbers were right; the code was wrong to read the strip as 16
positions along the bar (it's 8 × 2 rows). Scenes now use 8 positions with both rows the same
colour; driving the rows separately (left/right edge when mounted) is a possible later feature.
Verified on the bar: test patterns strip 3 / white 5, and the Up/down scene end to end.

CH3 (measured): 0 fastest; half a turn takes 0.86 s at 0, 1.2 s at 128, 1.9 s at 192, 3.6 s at 224,
6.7 s at 255. CH2 (measured): 0-8 off; ~16-129 spins opposite to increasing CH1, slow -> fast
(0.6 s/turn at 127-129); ~132 off; ~140-255 spins like increasing CH1, slow -> fast (0.65 s/turn at
255). Config keys are now `spin_fwd` [140, 255] / `spin_rev` [16, 129] (relative to CH1), and
`spin_value` uses the fixture's `cw` to turn "clockwise on the plan" into one of them.
Bar B: user set CH84 / A001 (said identical); not camera-checked. User is testing the app next.

## 1d. UI round 1 (2026-10-08)

- **Straight out was 180 degrees off.** At CH1 = 127 the beams point into the bar's base (the wall
  when mounted); straight out is ~42 (`config.HOME`). `config._migrate` resets `home` to 42 on any
  light that isn't calibrated (an uncalibrated home was never the user's choice).
- **Scenes are data.** `scenes.py`: a spec = general (label, group, swap) + palette + `move` +
  three light layers (`beams`, `strip`, `white`), each field defined in `SCHEMA` (the Make tab's
  form is generated from it; `normalize()` sanitises anything from the network). One renderer
  (`Scene.render`) handles every spec. `PRESETS` holds the 11 original scenes rebuilt as specs
  (close to the old hand-coded look, not identical) plus 7 new ones (Ping pong, Pendulum, Scanner,
  Lighthouse, Breathe, Strobe wall, Ball storm). Saved specs live in config `scenes` and override
  presets by name; `library()` merges them. Commands: `scene_save` (as_new or overwrite; saving a
  preset unchanged drops the override), `scene_delete` (restores a preset), `preview` (plays an
  unsaved spec until a scene is picked, the Make tab is left, or 15 minutes pass).
- **Live colours** (`show.colors`, command `colors`) replace colours 1 and 2 in every scene while
  on. They replaced the old per-scene palette overrides (`palettes`, dropped by the migration).
- **Perform pads**: command `group` (groove/calm/peak, optional `name`) goes through the director
  as an event with `pick`/`scene`, so it works with Auto off; only the Drop pad itself flashes and
  restarts the phrase. Pad `phrase` restarts the phrase count without a flash.
- **Ball cluster**: one point `{"id": "balls", "kind": "cluster", x, y, w, h}` replaces ball1/ball2
  (migrated to a box around them). `Ctx.cluster_half()` gives its angular half-width from each
  light, which the "hop" movement uses. Drag/resize on the plan in Setup (edit mode), or type w/h.
- `show.jitter_steps` is gone (jitter is per scene now). The Setup sweep width is the default for
  scenes whose sweep is 0.
- UI: tabs Perform / Show / Aim / Make / Setup. Perform opens first on touch or narrow screens (the
  last Show/Perform choice is remembered per device). Bar drawings show both faces (12 lenses;
  8 positions x RGB/white/RGB) and dim the face that points at the wall.
- Checked in the built-in browser at 375x812, 1180x820 and desktop with a `--dry-run` copy;
  `tests/test_scenes.py` covers the engine and commands. Not yet seen on the real bars.

## 1e. Round 2: horizontal mounting, beam safety, ambient, palettes (2026-10-09)

- **Geometry** (`geometry.py`): each bar lies along a wall at height z, facing into the room; its
  curtain tilts in the vertical plane at right angles to the wall. Angles are beam elevations:
  0 level out, 90 up, 180 level into the wall. Calibration per fixture is `up` (CH1 with beams
  straight up) and `dir` (+1 if increasing CH1 tilts toward the room), set on the Mount tab
  (`cmd_calib` actions up / dir / go / save / forget). Targets are 3D points (ball cluster with
  w/h/z) plus named elevations (up, out, ceiling mid-room, corner = far top
  edge, crowd = dance floor, for the LED side). A curtain only physically hits points in front
  of its bar (within its length); the plane is aimed anyway.
- **Beam safety** (user: "no beams past 90 degrees", read as never below level):
  `config.safety = {on, low: 0, high: 180}` (the user's is 270, see 1g). `safe_windows()` gives each copy of the safe arc in
  CH1 0..255 (two, as CH1 is 1.5 turns); each is contiguous. `place()` keeps positions in the copy
  the head is in; beam aims are clamped into [low, high] (Ctx.place). `engine.BeamGuard` tracks
  the range of CH1 positions the head could physically be at (it closes toward the target at the
  measured speed /1.25, starts as 0..255, and is held at 0..255 after a spin for max(8 s, 3 slow
  trips)); beams light only if target and range sit in one copy, the fixture is calibrated and
  nothing spins. Test mode zeroes beam channels when unsafe; the Mount tab dims them to 6%.
  `tests/test_safety.py` simulates a faster-than-assumed head that ends spins at random places
  and fails on any beam lit below `low`, or any long-way-round move within a scene. **Unverified
  on hardware:** what the head does after a spin stops (we assume it may unwind turns).
- **Scenes**: groups groove/calm/peak/ambient; clock beats or seconds; palette is 5 colours;
  move has `face` (beams / LED side / auto); new patterns wave, shimmer, twinkle, rain, flicker;
  colours spread/scroll/random/rainbow; `fx.lightning` strikes per minute. 32 presets incl. 11
  ambient (rain, downpour, thunderstorm, distant storm, Halloween, campfire, aurora, ocean,
  starfield, sunset, fireflies), Shimmer, Rainbow waves. Spinning scenes keep
  the beams dark by design.
- **Director**: state `ambient` (pad, key A, or auto on silence when `show.ambient_on_silence`);
  the analyzer now emits `music` when sound returns after silence, which ends an auto ambient.
- **Live palette** (`show.palette = {on, colors[5]}`, command `palette`) replaces a scene's palette
  while on; the UI builds palettes from one colour (similar / accent / soft) or two (blend).
- **UI**: tabs Perform / Show / Mount / Make / Setup. Magic sheet = green radar plan with each
  curtain's footprint (to the ceiling or far wall) and the LED side's pool on the floor; tilt dials
  per bar show the safe arc, the needle, and why beams are held off.
- **Config v2 migration** clears v1 calibration (vertical model), drops old keys, turns the two
  live colours into a 5-colour palette, adds heights. v3 removes the Blåhaj target (user asked).
- **Magic sheet ROOM view** (2026-10-09, from the user's photo taken in the corner across from the
  balls, head height, ultrawide): the default; PLAN is the old top view (keys at the sheet's bottom
  right, remembered in localStorage). `roomCam` puts the camera 0.3 m out of the corner opposite the
  cluster at 1.75 m, looks at the ball corner, clips to the near plane and fits both side corners and
  the far corner to the canvas (frozen while dragging). `drawRoom` draws shaded surfaces, the floor
  radar, 12 beam shafts per bar ray-cast to the walls/ceiling (`boxExit`), the LED wash, the balls,
  and specks thrown over the room when a curtain is on them. Editing works in both views
  (`sheetGeom`: bars go where the pointer's line of sight meets a wall; the cluster moves at its own
  height). With the default 2.8 m ceiling the curtains hit the ceiling before the balls, which is
  another sign the real ceiling is higher (see 1f).
  v4 removes the cross point (see 1g).

## 1f. The user's calibration (2026-10-09, by eye in the room)

- Both bars: CH1 40 = beams straight up, 125 = straight down; increasing CH1 tilts toward the room
  (`up` 40, `dir` +1). 85 steps for 180 degrees matches the probe's 2.12 deg/step; level is ~82.5.
- Limits (first dead step toward the room): A 81, B 82 (`fixture.limit`). Deadzones are exactly
  81-125 / 82-125 as the user asked: `safety.high` = 270 keeps 126-167 (beams pointing down the
  wall under the bar) lit, user's call. Beams may light at A 1-80 and 126-250, B 1-81 and 126-251
  (the top of the second copy, A 251-255 / B 252-255, is the room side again). Tested in
  `tests/test_safety.py`. CH1 0 is excluded only by the guard's half-step margin at the clip edge.
- Disco balls: A 76-81, B 77-81 (`fixture.ball_span`, used by the ball scenes; saved aims A 78, B 79).
  With B at the bottom of the right wall both lights put the balls ~3.05-3.10 m up, which means the
  ceiling is higher than the 2.8 m default; the cluster's z is capped at the ceiling until it's measured.
- `lightlaland.json.bak-before-limits` is the user's config just before these were written.

## 1g. Smooth movement, scenes for a tilt-only rig (2026-10-09)

- **Arcs past 180**: `safety.high` goes up to 270 (straight down the wall side). Elevations are
  wrap180, so range tests use `geometry.in_arc` / `clamp_to_arc` (Ctx.facing "auto", Ctx.place,
  the safety test), never `low <= e <= high`.
- **Vector speed** (`Engine._smooth`, `show.smooth`, default on, Setup > Music): CH3 follows the
  rate the scene moves CH1 (instant rise, 0.5 s decay), aiming at 1.5x that plus 1 step/s via
  `geometry.motor_speed_for` (inverse of the measured curve), capped by the scene's own speed.
  Before, sweeps ran the motor flat out between 2.1 degree steps (jump, stop, jump). Limits: CH1
  is 8-bit only, and the slowest motor speed is ~12.6 steps/s, so sweeps slower than that still
  step a little. The guard reads the commanded CH3, and 1.5x/1.25 margin > 1 keeps it caught up
  (Pendulum is lit 100% of the time with smoothing vs 87% without, in simulation).
- **Scenes**: the horizontal curtains can only meet in the ball corner, so the cross point is gone
  (config v4 drops it and saved `targets.cross`; old specs aiming at "cross" load as "balls").
  Cross and Ping pong aim at the balls; `move.converge` now drops both curtains in from above
  (from below was clamped flat). New `move.lift`: aim N degrees above the target. Pendulum uses
  lift 15 + sweep 15, so it swings from the balls up the wall and back (it used to sweep +-25
  around the balls and sit clamped at the limit half the time).
- Scenes that use the disco range (`ball_span` / saved ball aims): Ball jitter, Ball storm, Cross,
  Ping pong, Pendulum, Sweep + jitter's resting light. Near-useless on this rig (user to decide,
  not deleted): Spiral (CH2 spin, so beams are always dark; LED/strip show only), Far edge
  ("corner" lands just above the balls from both bars).

## 2. The channel map: printed chart vs what the code assumes (historical)

The printed manual's **84 Channel** table (photo of the paper; rows elided with "…" are in the original):

```
CH1  Y Motor 1080 degree           CH24-27  Beam Curtain LED 6 R/G/B/W Dimming
CH2  Y 360 degree infinite rotation CH28-31  STROBE LED 1 R/G/B/W Dimming
CH3  Y Motor speed                 ...
CH4  Beam Curtain LED 1 R Dimming  CH73-76  STROBE LED 16 R/G/B/W Dimming
CH5  Beam Curtain LED 1 G Dimming  ...
CH6  Beam Curtain LED 1 B Dimming  CH83     LED W8
CH7  Beam Curtain LED 1 G (sic)    CH84     Reset
```

The chart contradicts itself. 16 RGBW strobe segments starting at CH28 would end at CH91. "16 R"
at CH73 only works if the segments are **RGB** (28 + 3×15 = 73). Then CH76 can't be "16 W", and
CH76–83 are probably W1–W8. The code (`lightlaland/fixture.py`) assumes that reading:

```
CH1      position, 0-255 over 1080°   (=> 85 steps per turn, 4.235° per step)  UNVERIFIED
CH2      infinite rotation: 0 off, cw 10-127, ccw 128-255                       GUESS (per-fixture config)
CH3      motor speed, 0 = fastest                                               GUESS (per-fixture config)
CH4-27   6 beam groups × R G B W   (12 beams in pairs)
CH28-75  16 strip segments × R G B
CH76-83  8 white strobe segments
CH84     reset: never send non-zero (the engine and the test API both block it)
```

Other plausible layouts to test: 12 RGBW segments on CH28–75; colour order GRB or RGBW-with-W-first;
segments numbered bottom-up (the `flip` option already handles reversed order); beams and strip
on opposite faces of the bar (believed: one face has the 12 beams, the other the strip + white
strobe, so the camera sees one face at a time).

### Where the map lives in code

- `fixture.py`: constants `N_BEAMS, N_STRIP, N_WHITE, BEAM0, STRIP0, WHITE0, RESET`, `encode()`
  (Frame → 84 bytes) and `describe()` (labels for the test page). Scenes never touch channels.
  They fill a `Frame` (`beams` list of RGBW, `strip` list of RGB, `white` list), so a new layout
  should ideally change only `encode()`/`describe()`.
- `scenes.py` and `engine.py` import `N_BEAMS/N_STRIP/N_WHITE` (list lengths). If the segment
  count changes, these follow automatically. Check the strip scenes (UpDown, MidOut, Rainbow,
  Fill, Sparkle) still look right.
- `app.py` `cmd_test` action `pattern` (beam → W die of group n, strip → R of segment n, white n).
- `web/index.html`: the Channel test chips (~line 713, 16 strip buttons) and the visualizer face
  drawing (`drawFace`, ~line 965: 6 beams / 16 strip / 8 white).
- Suggested refactor once the truth is known: make the map a data table, e.g.
  `[(channel, kind, index, colour), ...]` or per-kind lists of channel tuples. Then the probe's
  output can be pasted in, and a second fixture model only needs a new table.

## 3. Webcam channel probe plan (done; kept for reference)

The user wants to leave the MacBook pointed at a bar and let Claude Code work out the channels.
This is feasible. Write it as `tools/channel_probe.py` (new). Plan:

### Setup (with the user, once)
1. Dim the room. Put the MacBook ~1.5–3 m from **light A**, whole bar in frame, with the
   strip/strobe face roughly toward the camera.
2. Camera: `pip install opencv-python` into `.venv`, then
   `cv2.VideoCapture(0, cv2.CAP_AVFOUNDATION)`. The first capture triggers macOS's camera
   permission for the terminal app. **The user must be there to click Allow** (System Settings >
   Privacy & Security > Camera if it was denied). Continuity Camera can make an iPhone index 0, so
   save one still, look at it yourself (Read the PNG), and confirm it shows the bar before running.
3. Claude Code asks before running commands. Have the user approve the probe command with
   "don't ask again" so it can run unattended.
4. Warn the user: white segments and beams at full are painfully bright, and the probe flashes.
   Keep levels low and nobody staring at it.

### Driving the light: exactly one Art-Net sender
Two senders on one universe make the bar flicker between them. Either:
- **(preferred)** Run the app (`python -m lightlaland --no-audio`) and drive its test mode over
  HTTP. This pauses the show for that fixture and blacks out the other.
  `POST http://localhost:8080/api/cmd` with JSON:
  `{"cmd":"test","action":"start","fixture":0}` (0 = A, 1 = B),
  `{"cmd":"test","action":"set","channel":N,"value":V}` (N 1–83; channel 1 = position),
  `{"cmd":"test","action":"clear"}`, `{"cmd":"test","action":"stop"}`.
  Responses are `{"ok": true, ...}`. The engine resends all channels at 40 fps.
- or quit the app and use `lightlaland.artnet.Sender(...).send("broadcast", universe, buf, 84)`
  from a thread at ~30 fps. Don't send once and stop.

### Algorithm
1. **Dark frame:** all channels 0, wait ~0.4 s, discard ~5 buffered frames, take the median of
   ~5 frames. Re-take the dark frame every ~10 channels to absorb auto-exposure and white-balance
   drift. You can't reliably lock exposure on macOS through OpenCV.
2. **Region of interest:** light every strip/white channel at a low level, diff against dark,
   threshold, and get the mask's main axis by PCA. Positions along that axis (0..1) give the
   segment order.
3. **Per channel** 4–83: set level L (~30–60, raise if invisible, lower if colours clip to white),
   settle, capture, diff against dark. Record visible energy, centroid along the axis, blob size,
   mean diff colour → R/G/B/W (W = low chroma), or "not visible from this side". Save a small diff
   thumbnail per channel.
4. **Infer the layout:** cluster channels by axis position into segments. The colours inside each
   cluster give the order (RGB vs RGBW vs GRB). Segment order along the axis gives `flip`.
   Invisible runs are probably the other face. Use CH1 to turn the bar (step 3), or ask the user,
   then probe that face. For the beam face, beams pointed at the lens saturate. Use a very low L,
   or aim them at a wall/ceiling and track the spots instead.
5. **Motion (second pass):** light the strip face, sweep CH1 0→255 in small steps with a dwell,
   and plot ROI brightness. The peak spacing gives steps per 360°, checking the 1080° claim and
   finding which CH1 faces the camera. For CH2, measure the brightness flicker period at a few
   values to map value → speed. Direction can't be seen from one viewpoint, so ask the user. For
   CH3, time a fixed CH1 move at a few values to get its polarity.
6. **Outputs:** `probe/A/results.json` (channel → kind/segment/colour/position/visible), diff
   thumbnails, and one labelled contact sheet. **Look at the contact sheet yourself** before
   trusting the numbers, then show the user a short table of findings.
7. Repeat a spot-check on light B (universe 1). The bars should be identical, but confirm which
   one is on which output.
8. Update `fixture.py` (and the UI pieces listed above), run the tests, and have the user check
   Setup > Channel test and a few scenes by eye.

Pitfalls: LED room lights flicker at 120 Hz (median of frames helps); webcam latency (settle +
discard frames); saturation hides colour; auto white balance shifts hue when only one colour is
lit, so classify by dominant channel ratio, not absolute hue.

## 4. Other open items (after the map)

- Mount calibration of both bars (beams stay off until done), room height, bar positions and
  heights, target positions/heights. Config lives in `lightlaland.json` in the folder the app is
  started from. **That's the user's calibration, so don't overwrite or delete it.**
- Watch a spin end on camera (`tools/channel_probe.py`) to see whether the head unwinds; the guard
  assumes it might.
- Scene looks on the real bars, especially the ambient ones and the curtain angles.
- Ideas not done: drive the two RGB rows separately; brightness on the Perform screen; stairs as
  a separate no-beam zone if level beams
  still reach anyone there (raise the lowest angle meanwhile).
- Audio has not been tested on the real Macs: input device choice, mic permission, Sync (ms)
  tuning against the bars' actual latency.
- Record-change detection is a hint (spectral novelty). It may misfire on real mixes. A Setup
  toggle exists.
- Node static IP is optional. Broadcast works.

## 5. Running and testing

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt   # or double-click start.command
.venv/bin/python -m lightlaland [--no-audio] [--dry-run] [--wav mix.wav] [--port 8080] [--config path]
.venv/bin/python -m lightlaland.find        # ArtPoll scan (won't hear the 2.x node without an alias)
.venv/bin/python -m tests.test_analyzer     # synthetic vinyl mix -> beat/phase/event accuracy (~1 min)
.venv/bin/python -m tests.test_scenes       # every scene renders valid DMX; scene maker commands (~1 s)
.venv/bin/python -m tests.test_safety       # beams never lit below level against a simulated head (~5 s)
```

Last test result: beat phase error mean ≤5 ms, p95 ≤9 ms, max 13 ms. Breakdown, drop and record
change were all detected. UI checks were done earlier with Playwright in a cloud sandbox. That
script isn't in the repo. If you change the UI, run a `--dry-run --port 8090 --config <scratch>`
copy and check every tab at phone, iPad and desktop widths.

Code map: `artnet.py` (ArtDMX, ArtPoll/Reply, broadcast), `analyzer.py` (kick-band flux → ACF
tempo + comb phase + PLL, breakdown/drop, novelty), `clock.py`, `scenes.py`, `engine.py` (40 fps
loop, director, overlays, output), `fixture.py`, `geometry.py` (CH1 ↔ plan angle), `app.py`
(commands), `server.py` (HTTP + SSE), `web/index.html` (single-file UI).
