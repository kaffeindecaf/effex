# effex roadmap

purpose: pick one item per session, finish it end-to-end (code + test + docs),
then check it off with a done note saying what actually landed.
format: `[ ]` open, `[x]` done, done notes indented under the item.
state: input/ holds source videos, output/ holds renders and previews, effex.py
is the tool (v0.12.0, 24 effects, chains of two or more), tests/ has ten
suites. verify before closing an item with:

    python3 tests/run_tests.py        # cli + datamosh + menu + layout + install + stateful + psychedelic + collapse + seams + inversion
    python3 tests/make_clip.py        # drop a lavfi test clip into input/
    ./install.sh                      # effex onto ~/.local/bin

---

## section 0 - where the next session starts

- [x] E0.4 - effect chaining (E3.3): stack two effects in one render by reusing
      each builder's chain
  _Done 2026-09-25: see E3.3. chains work on the cli (`stretch+deepfry`) and in
  the menu (space chips an effect, the chip number is the run order). the next
  session starts at E0.5._
- [ ] E0.5 - config file (E3.7): effex.conf with default shape / crf / preset,
      template written on first run, ~45min
- [x] E0.6 - crt / scanlines (E2.12) - shipped in the 2026-09-24 batch
- [x] E0.7 - slow + pitch warp (E2.8) - shipped in the 2026-09-24 batch,
      which also proved the pipeline can do audio filters
- [ ] E0.8 - psychedelic batch 1 (all single-pass, ~1 session): channel
      lobotomy (E2.29), strobe gates (E2.30), neon hue cycle (E2.31), heat
      palette (E2.37), tblend madness (E2.38), slice tears via geq (E2.42),
      then the trip stack (E2.43) chains them - the first bleood-look render
  _Partial: channel lobotomy (E2.29) and neon hue cycle (E2.31) now ship;
  the remaining pieces and trip stack are still open._
- [ ] E0.9 - the E3.1 unlock (filter_complex): feedback loop (E2.32),
      kaleidoscope (E2.19), slice shuffle (E2.20), glow (E2.22), slitscan
      (E2.35) all wait on it - decide the Plan shape first, ~1 session

---

## section 1 - menu + pipeline

- [x] M1.1 - interactive menu (video picker, effect picker, y/n confirm, render)
  _Done 2026-09-24: effex.py, stdlib only. video list from input/ with resolution,
  duration and size per row; effect list from the EFFECTS registry; arrow-key
  selector with termios cbreak + echo off, numbered prompt fallback when stdin is
  not a tty; y/n confirm with default yes; result panel with size, elapsed and
  realtime factor; unique output names (clip_STRETCHED_2.mp4) instead of
  clobbering, --force overrides._
- [x] M1.2 - ffmpeg auto-detect + install prompt
  _Done 2026-09-24: shutil.which for ffmpeg and ffprobe; --check prints both
  paths; when ffmpeg is missing the menu names the detected package manager
  (apt-get, dnf, pacman, zypper, apk, brew - first hit wins), shows the exact
  command, and asks y/n. yes runs it, then re-probes and reports. no prints the
  command to run later and exits cleanly. no package manager -> manual hint only.
  piped runs never prompt, they print the command and exit 1. proven with a fake
  PATH holding apt-get and sudo shims (tests/install_prompt.py)._
- [x] M1.3 - progress bar
  _Done 2026-09-24: ffmpeg runs with -progress pipe:1 -nostats -loglevel error;
  bar, percent and done/total read from out_time_us, stderr drained on a thread so
  a full pipe cannot deadlock. rendered only on a tty; piped runs stay plain.
  ctrl-c terminates ffmpeg instead of orphaning it. the bar now clears itself and
  the screen is redrawn after, so no leftovers._
- [x] M1.4 - cli surface next to the menu
  _Done 2026-09-24: `effex.py` (no args, tty) opens the menu; `effex.py stretch
  clip.mp4 [more...]` renders without prompts; `--list` shows effects, their
  default values and ranges; `--check` exits 1 when ffmpeg is missing; `--json`
  for scripts; `--no-color` / NO_COLOR / non-tty strips all ansi; no args and no
  tty prints help instead of blocking._
- [x] M1.5 - per-effect options menu (the "another menu" step)
  _Done 2026-09-24: params live on the effect (Param: key, label, help, kind,
  default, choices, min/max/step), the options screen opens straight after the
  effect pick and shows every option with its live value, a preview line (output
  name, frame size, filter chain) and the rows render / back. keys: arrows move,
  left/right change a value in place, enter opens the full choice list or a text
  prompt, r renders, b backs out. cli equivalent: `--set target=16:9`, with
  `--size WxH` as shorthand. bad keys, bad choices and absurd sizes are rejected
  with the real options listed._
- [x] M1.6 - menu polish + ease of use (first pass)
  _Done 2026-09-24: status line (ffmpeg, videos in input/, renders in output/,
  version), key gutters so letters and numbers jumped straight to a row, a keys
  line under every screen, a help screen (`?`), the effects screen listing each
  option's default, a boxed confirm panel, q/esc stepping back one screen at a
  time, r rescanning the input folder in place. the banner and logo from this
  pass were removed again in M1.8 (see there)._
- [x] M1.7 - pointed folders, so tests and other libraries are safe
  _Done 2026-09-24: input/ and output/ come from EFFEX_INPUT_DIR / EFFEX_OUTPUT_DIR
  when set (plus --input-dir / --output-dir / --output). the test suite runs
  entirely against temp folders because of it._
- [x] M1.8 - full-frame redraw, no ascii, easier layout (second pass)
  _Done 2026-09-24: the menu was confusing to use, so the drawing model changed.
  every screen is now built as one complete frame and written with a single
  write after homing the cursor (ESC[H + frame + ESC[J), instead of moving the
  cursor up and rewriting a tracked block. that kills the artifacts from
  nested screens and from frames that shrank. the ascii logo, the double-line
  banner and the boxed panels are gone; the header is one line
  (`effex > pick a video` on the left, a small status on the right), then a rule,
  the rows, a rule, and one keys line. the selected row is a full-width reverse
  bar, every row is padded to the same width so columns line up, and long values
  are truncated with an ellipsis instead of wrapping. list screens paginate to
  the terminal height, and everything is measured against the real terminal
  width (48 to 140 columns). tests/layout.py parses the frames out of a pty at
  60x20, 80x24 and 120x40 and fails if any line is too wide, if rows disagree on
  width, if a frame moves the cursor, or if the screen was not cleared before
  the first frame._
- [x] M1.9 - effect 2: pixelize, proving the registry extends cleanly
  _Done 2026-09-24: one new Effect entry plus its params, no menu or pipeline
  changes. block size (int, 2-128, step 2, clamped so a block can never exceed a
  quarter of the short side) and shape (keep source, or any target shape).
  filter chain: scale to the working frame, scale down to iw/chunk, scale back up
  with neighbor, setsar=1. verified by a cli check that a chunk of 96 produces a
  file at least 25% smaller than a chunk of 2 on the same clip, and by a pty test
  that the frame size and audio survive._
- [x] M1.10 - six more effects in one pass, proving the registry scales
  _Done 2026-09-24: rgbsplit, stutter, deepfry, wobble, spin and posterize - all
  registry entries with their own params, none of them touching menu or pipeline
  code. all eight effects render for real (each verified against a lavfi clip
  before the tests were written), and every effect keeps the source frame size
  unless it says otherwise. a shared _clamp_int / _source_dims helper pair keeps
  the builders honest about odd sizes and nonsense values._
- [x] M1.11 - output visibility pass (a render must never vanish silently)
  _Done 2026-09-24: n on the confirm screen now returns to the options screen
  with an amber "nothing was rendered" notice instead of dropping you at the
  main menu as if nothing happened; the done screen gained o, which opens
  output/ in the file manager (xdg-open / open, graceful when neither exists);
  a failed or cancelled render says so on the done screen. the decline path is
  covered by a pty test that also proves exactly one file lands after a decline
  and a re-render._
- [x] M1.12 - option kinds + effect refinement pass
  _Done 2026-09-24: three new kinds - float (fractional numbers, 0.5 steps),
  percent (10-100%, "55" and "55%" both accepted), and multi (pick several
  values; a checkbox screen where space toggles, enter confirms, q keeps).
  every existing effect gained knobs: stretch flip, pixelize edges
  (crisp/soft), rgb split axis + which planes drift, stutter hold mode
  (minterpolate dup - keeps the source frame rate while the picture holds),
  deepfry brightness + crisp and strength as a percent, wobble triangle
  waveform, spin direction + fractional seconds per turn, posterize grain,
  datamosh grit. all refined options are verified two ways: dry-run chain
  assertions plus real renders checked with ffprobe - which caught two real
  bugs: chromashift has no green option (dropped from the choices) and eq
  saturation is hard-capped at 3.0 (deepfry clamps instead of erroring at
  full strength). 285 checks green._

---

## section 2 - effect backlog

each item is one Plan builder in the registry. filter chains are the actual ffmpeg
expressions, so they are drop-in once the effect model supports the bits they need.

- [x] E2.1 - pixelize / chunky
  _Done 2026-09-24: see M1.9. chain is scale to frame, scale=w=iw/chunk:h=ih/chunk
  with neighbor, scale back to the frame with neighbor._
- [x] E2.2 - rgb split
  _Done 2026-09-24: chromashift=cbh=N:crh=-N, N in 2-80 chroma steps (step 2,
  default 12). the classic red/cyan 3d-glasses drift, frame size untouched._
- [x] E2.3 - wobble (frame rocks side to side)
  _Done 2026-09-24: rotate=a='DEG*(PI/180)*sin(2*PI*t/PERIOD)':fillcolor=black,
  amount 1-15 degrees plus a slow/medium/fast speed choice. thin black corners
  show at the edge of each rock, which is the look._
- [x] E2.4 - zoom drift / punch in
  _Done 2026-09-24: zoompan creep with a max zoom percent (110-300) and a
  slow/fast speed choice. zoompan is the slowest filter in the set - the
  notes warn about long renders._
- [x] E2.5 - vhs
  _Done 2026-09-24: chroma bleed, tape grain, tracking tears (crop+pad roll
  with a t expression) and a color fade - four knobs, each can go to zero
  (a stripped vhs is a pass-through). the pad bug was a real one: pad's
  iw/ih refer to the cropped frame, so the height has to come from the
  source at build time. the section 6 research (ntsc-rs, NTSC-CRT, gists)
  fed this._
- [x] E2.6 - smudge / trails
  _Done 2026-09-24: tmix with trail length 2-20 and fade (recent frames
  weigh more) or even weights. verified behaviorally: trail 16 output is
  61% the size of trail 2 - blending really does smear detail away._
- [x] E2.7 - stutter / choppy
  _Done 2026-09-24: fps=8 (or 12, 15 as a choice). audio keeps its timing, only
  the picture chops - and the file shrinks, verified at 8/1 in ffprobe._
- [x] E2.8 - slow + pitch warp
  _Done 2026-09-24: quarter/half/double/quadruple speed - setpts on the
  video, atempo chained on the audio (0.25x = atempo=0.5,atempo=0.5). this
  forced the first engine extension: Plan carries an af (audio filter
  chain) that build_command and the preview map with -af when the clip
  has audio, and skips on silent clips. verified: 0.5x duration 6.0s on a
  3s clip, 2x = 1.5s, aac audio preserved._
- [ ] E2.9 - boomerang (forward then backward): needs E3.1
- [ ] E2.10 - mirror tunnel: crop one half, `hflip`, `hstack` - needs E3.1
- [x] E2.11 - deepfry
  _Done 2026-09-24: eq=saturation=S:contrast=C,unsharp=5:5:A, strength 10-100
  (default 40 = 3.0x saturation, 1.60 contrast, sharpen 4.0). the screenshot of
  a screenshot look._
- [x] E2.12 - crt / scanlines
  _Done 2026-09-24: drawgrid scanlines, optional tube curve
  (lenscorrection), electric fuzz and a slow vertical roll - same
  crop+pad trick as vhs._
- [x] E2.13 - wide squeeze (inverse of stretch)
  _Done 2026-09-24: folded into stretch instead of a second effect - the shape
  option covers 16:9, 4:5, 1:1, 3:4 and any typed WxH, so a separate "squeeze"
  would have been the same builder with one different default._
- [x] E2.14 - spinning frame
  _Done 2026-09-24: rotate=a='2*PI*t/PERIOD':fillcolor=black, seconds per turn
  2-30 (default 6). the corners black out as the frame turns._
- [ ] E2.15 - gif export: split -> `palettegen` -> `paletteuse`, output .gif - needs E3.1
- [ ] E2.16 - audio effects as their own picker (bass, vibrato, aecho, asetrate)
      - unblocked by the af field E2.8 added: single-chain audio filters
        now work, only the picker UI is left
- [ ] E2.17 - wave / liquid: `wavefilter` via `displace` with a moving
      `nullsrc` waveform map - the classic ripple, medium effort
- [ ] E2.18 - fisheye / bulge: `lenscorrection=k1=-0.35:k2=0.1` plus an optional
      aspect stretch, cheap and very "phone camera in a bottle"
- [ ] E2.19 - kaleidoscope: crop a wedge, `hflip`/`vflip` it, `hstack`/`vstack`,
      again - needs E3.1 for the multi-branch graph
- [ ] E2.20 - slice shuffle: `crop` the frame into N horizontal bands and stack
      them back in a shuffled order (fixed permutation or seeded random)
- [x] E2.21 - posterize
  _Done 2026-09-24: format=rgb24 then lutrgb=floor(val/Q)*Q on r/g/b, levels 2/4/
  8/16 (default 8, Q=32). flat, screen-printed color bands; the encoder converts
  back to yuv420p itself._
- [ ] E2.22 - glow / bloom: split, `gblur=sigma=12`, `blend=all_mode=screen`,
      needs E3.1
- [ ] E2.23 - temporal smear: `tmix=frames=20:weights='...'` with a triangular
      weighting - the "dragged across the screen" look
- [ ] E2.24 - displacement glitch: `displace=edge=mirror` with a `noise` map and
      a moving seed - now only the cheap approximation; the real thing is E2.25
- [x] E2.25 - datamosh (real one, codec-level)
  _Done 2026-09-24: two passes - encode mpeg4 part 2 with `-g <hold> -bf 0`,
  then strip_ivops() cuts i-vops out of the bitstream in python (00 00 01 b6
  start codes, top 2 bits of the vop header = coding type) and fills each hole
  with the next p-vop, then the stream is remuxed with `-c:v copy` and the
  original audio. the decoder applies each run of motion vectors to a stale
  picture, which is the mosh. cut = heavy/medium/light (keep every 1st/2nd/3rd
  keyframe), hold 15-600 frames between keyframes. frame count and audio sync
  survive the cut (verified: heavy keeps 1 keyframe, light 2, both stay at
  90 frames / 3.0s). a custom run callback on the registry entry runs the two
  passes inside the normal bar/cancel/cleanup flow, and the preview key hides
  itself on multi-pass effects. technique and links: section 6._
  _Hardened 2026-09-24: dice (per-keyframe 1-in-4 rolls, seeded from the
  file), shuffle (first keyframe, then every motion frame replays in a
  random order - the hard mode), fill choices (next drift / prev freeze /
  chaos random p-vop) and hold down to 2. "no keyframes at all" was tried
  and removed: an mpeg4 stream without its first i-vop decodes to zero
  video frames, so that mode cannot ship - shuffle is the hard mode._
  _Extended: datamosh can append a second input (trimmed, normalized to the
  primary size/rate and joined in the same mpeg4 stream); auto audio follows
  the join and pads missing tracks with silence. dice now exposes a 0-100%
  keyframe survival control and an explicit reproducible seed. CLI and menu
  validate the second source and segment bounds before encoding; ffprobe-backed
  tests verify duration, frame size, cross-source motion, audio and keyframe
  survival._
- [x] E2.26 - circuit bending kit
  _Done 2026-09-24: channelswap (colorchannelmixer channel rewrites plus
  optional chroma bleed), blockswap (swaprect blocks traveling on sin
  curves) and dropout (bendr-style random frame drops with freeze-holds,
  audio untouched). two real ffmpeg-7 gotchas surfaced: lutrgb has no
  cross-channel variables (r=b is undefined - colorchannelmixer is the
  swap filter), and swaprect's expression context knows w/h but not iw/ih.
  behavioral proofs: a pure red frame comes out pure blue after a rb swap,
  and frames at two timestamps differ on a static clip, so the blocks
  really travel. the bendr / video-glitch-array vocabulary from section 6
  fed this._
- [x] E2.27 - crunch (bitrate + resolution down)
  _Done 2026-09-24: resolution choice (keep/75/50/25%) and a bitrate cap
  (100-5000 kbit/s). forced a small engine flag: Plan.abr drops -crf for
  the render because -b:v only bites in abr mode - with crf present the
  bitrate knob was silently ignored (verified: two bitrates produced
  byte-identical files before the fix, a 0.2x size ratio after)._
- [x] E2.28 - timescratch (timeline scrub)
  _Done 2026-09-24: the clip is cut into segments, each gets a signed
  speed from a curve (sine / random / swing / walk), backward segments run
  reverse+setpts=N/FPS/TB and areverse+atempo, then everything concats
  with -c copy. a plain setpts wobble cannot go backward - per-segment
  re-encodes are the only stock-ffmpeg way. speeds are seeded from the
  file (deterministic renders), atempo chains decompose to 0.5-2 stages
  for exact ratios, and -ss/-to frame the input so slow segments are not
  truncated. verified: 3s source renders to 7.42s, audio aac survives,
  silent clips skip the audio chain, two renders agree to <0.05s._

### the psychedelic family (the bleood look - E2.29 to E2.43)

bleood = underground florida rap whose videos look like a circuit-bent
deck melting on acid: crushed codec ruins, rgb splits, datamosh, blown-out
color, strobes, warps. the goal here is that look as a layered family, not
one filter. every item carries its feasibility tag: (now) = single-pass,
stock ffmpeg; (E3.1) = needs the filter_complex engine; (multipass) = the
timescratch/datamosh run-callback pattern; (numpy) = needs the pixel-math
decision.

- [x] E2.29 - channel lobotomy (now): bitcrush each channel
      (`lutrgb=r=floor(val/Q)*Q:g=...:b=...`), kill a channel
      (`colorchannelmixer` zero), swap them - the "lobotomized chroma" look
      from videoglitchcrazynes. chain the three knobs into one builder
  _Done: `lobotomy` has independent RGB level controls (2-256), optional
  source-channel kill, and channel swaps applied after crushing/killing.
  Reuses the existing swap matrices and single-pass pipeline. Pixel tests
  verify quantization, every kill/swap mode and unchanged-color settings;
  CLI and PTY tests cover validation, previews, real renders and chaining._
- [ ] E2.30 - strobe / invert gates (now): time-gated negative flashes,
      `lutrgb=r='if(lt(mod(t,P),D),255-val,val)'` on all channels (verify
      lutrgb accepts t at build time), plus a white-out gate via eq
- [x] E2.31 - neon hue cycle (now): `hue=H*sin(2*PI*t/P)` + saturation
      crush + a curves punch - the acid palette slowly rotating
  _Done: `neon` uses `hue=h='A*sin(2*PI*t/P)'` (degrees), saturation and an
  adjustable S-curve. Options cover 0-180 degree swing, 0.5-30 second cycle,
  100-300% saturation and 0-100% punch, including a fully disabled setting.
  Static-color tests prove time-varying hue and visible saturation/contrast;
  real renders verify both chain orders, dimensions, timing and audio/silence.
  Both new effects live in COLOR & TEXTURE with editable options and previews.
  README updated; all seven test suites pass._
- [ ] E2.32 - video feedback loop (E3.1): THE psychedelic core - split the
      frame, rotate by t degrees, scale ~1.02, blend back at ~0.55 decay,
      screen/add mode. the blob-tracker glossary has the exact math; the
      self-similar tunnel writes itself
- [ ] E2.33 - lagfun phosphor trails (medium): out = max(prev*R, curr) with
      retention R ~0.55-0.75 - highlights leave glowing trails (the
      TouchDesigner lagfun pattern). stateful decay is not one stock
      filter; try tblend=all_mode=maximum + eq fade as the approximation
- [ ] E2.34 - pixel sort melt (numpy): brightness-sorted pixel runs per
      row, banded - the Kim Asendorf look. pixelmosh.py and ASDFPixelSort
      in section 6 have the exact algorithms; blocked on the numpy
      decision, a pure-python sort on big frames is unusable
- [ ] E2.35 - slitscan / temporal stripes (multipass + E3.1): cut the
      frame into N horizontal stripes, offset every stripe in time, stack
      back - time flows sideways. philintheattic/videoslitscan and
      slit_scanner_darkly are the references
- [ ] E2.36 - datascope overlay (audio family): the audio drawn as an
      oscilloscope and blended over the picture (ARTFORGE datascope) -
      rides on the E2.16 audio picker work
- [ ] E2.37 - heat / thermal palette (now): eq + gamma + hue + curves into
      the thermal/heat palette - "the camera sees through skin" look
- [ ] E2.38 - temporal blend madness (now): `tblend=all_mode=hardmix |
      phoenix | heat | freeze | negation` - ffmpeg 7 tblend carries the
      whole psychedelic blend vocabulary, one knob = one mode
- [ ] E2.39 - generation loss (multipass): re-encode the clip through N
      damage passes (grit up each pass) or jpeg-databend frames - the
      "copied a copy of a copy" decay from datamosh-tools
- [ ] E2.40 - glitch shaders (custom ffmpeg): ffmpeg-gl-transition brings
      Shadertoy-style GLSL warps into the chain - a whole engine decision
      (custom build), noted for later
- [ ] E2.41 - audio-reactive glitch (big ticket): every effect gated by
      beats (librosa/astats detection) - videoglitchcrazynes does this
      fully; stdlib-only means a beat detector must be written or the
      numpy decision must land first
- [ ] E2.42 - slice displace / band tears (now): per-band horizontal
      offsets through geq pixel math -
      `geq=lum='p(X-OFF,Y)':cb='p(X-OFF,Y)':cr='p(X-OFF,Y)'` with
      OFF = A*sin(2*PI*t/P + Y/B) - horizontal tear bands moving
- [ ] E2.43 - the trip stack (now, the capstone): one builder that chains
      the now-pieces - channel lobotomy + neon hue cycle + strobe gates +
      tblend hardmix + grain - the default bleood look, with an intensity
      knob scaling how many pieces join the chain

### stateful glitch effects

Six effects from the requested design prompt. Temporal Sediment, Elastic Collapse and Causality Inversion are implemented; the remaining three are checklist items, not implementations.

Shared rules for the stateful effects:
- Trace how `effex.py` scans/probes clips, builds `Plan`s, invokes custom runners, and encodes output. The core stays FFmpeg-filter based; Temporal Sediment adds a narrow per-frame adapter for stateful effects.
- Reuse the stateful frame adapter added for Temporal Sediment; do not build a second decoder/export path. Keep every effect's config separate from its runtime state and reset state on a new clip or explicit reset.
- Preserve dimensions and pixel format, avoid mutating shared frames, bound memory, handle first/short clips and scene cuts, and use timestamps where available.
- Make effects independently configurable and reorderable in a chain. Keep each history buffer owned by its effect; share flow or metadata only with clear ownership.
- Keep a CPU implementation. Consider NumPy/OpenCV only when needed; no GPU or downloaded ML models.
- Register options through the existing effect model. Persist settings only, not temporal state. Add validation and concise docs. Do not mark a placeholder as done.
- For each effect, test frame shape/channels/dtype, input mutation, first/short/static/scene-cut behavior, off/zero-intensity passthrough, parameter bounds, fixed-seed repeatability, and bounded memory. Test variable timestamps where the pipeline supports them.
- Add one integration test chaining all six in user-selected order. Benchmark each at 640x360 and 1280x720 for frame time and peak memory; do not call it real-time until measured.

- [x] E2.44 - temporal sediment (prompt effect 01): accumulate visual history instead of making a simple motion trail.
  - Maintain one bounded float buffer or a small set of layers with different decay rates. Deposit each frame at a configurable rate and normalize/clamp the accumulated result so brightness cannot grow without limit.
  - Use motion, edges, brightness, or a slowly changing mask to control deposition. Add excavation that reveals or removes saved history; it must expose actual history, not just add random noise.
  - Options: intensity, deposit rate, decay rate, motion sensitivity, and excavation chance use 0-100 percent values; bound layer count and excavation size. Add color-memory and seed controls.
  - Test static-scene stability, recognizable trails from a moving shape, visible deposit/decay changes, excavation of older content, and a reset that clears state.
  _Done: registered `sediment` as a CPU stateful frame processor. It keeps 1-3 float16 decay layers within a 128 MiB state budget, downscales history for large sources, supports motion-weighted deposit, seeded excavation and color memory, resets on explicit reset/scene cut/rewind, and composes with FFmpeg filters before or after it. Tests cover bounds, repeatability, frame shape, no mutation, decay, reset, scene cuts, timestamps, actual renders, chaining and audio. VFR inputs are normalized to the probed FPS; original per-frame PTS is not retained._
- [ ] E2.45 - frame debt (prompt effect 03): let motion events borrow frames and repay them later.
  - Start with a normalized frame-difference score; add optical flow only if the simple detector is insufficient. Use a threshold with hysteresis or cooldown so small changes do not retrigger continuously.
  - On a motion event, queue a bounded frame or patch snapshot with a due timestamp. Release delayed frames or short repayment bursts without blocking the render loop. Handle clip end when queued debt remains.
  - Options: motion threshold/sensitivity, min/max delay, debt gain, release rate, burst size, freeze chance, patch mode, queue capacity, seed.
  - Test that static input creates little debt, a sudden moving shape triggers delayed output, sensitivity and release controls change behavior, fixed seeds repeat, and queue memory stays bounded.
- [x] E2.46 - content-aware scaling and elastic warp (prompt effect 05): change image composition while preserving output dimensions.
  - Default to progressive content-aware scale: linearly ramp seam-carved shrink from identity at clip start to the configured target at clip end, then remap each frame to its original canvas.
  - Analyze at reduced resolution, preserve high-energy regions, cap shrink to half per axis, and smooth cached decisions. Keep the earlier seeded elastic warp as an alternate mode.
  - Options: intensity, horizontal/vertical shrink, content preservation, seam strength/mode, elastic motion influence, field smoothness/speed, temporal coherence, seed.
  - Test linear progress against clip duration, fixed output size, content preservation, repeatability, bounded state and real CLI/menu renders.
  _Done: `collapse` defaults to a content-aware seam scale that begins at the
  source composition and grows linearly to its target over probed clip duration.
  The low-resolution energy map removes seams from low-detail regions, and the
  renderer inverts that displacement so salient content shrinks instead of
  growing. Each output is mapped to the original frame dimensions. Analysis is
  capped at 192px; shrink is limited to 50% per axis and refreshed at <=5Hz with
  temporal smoothing. The seeded elastic warp remains selectable.
  Duration flows through the shared stateful adapter. Tests cover exact start,
  linear midpoint field, end-frame deformation, output shape/audio, resets,
  preservation, optional dependencies and TUI/CLI integration. Optional headless
  OpenCV remains isolated in requirements-collapse.txt. Existing stateful VFR
  normalization, adjacent-processor and preview limits remain explicit._
- [ ] E2.47 - pixel archaeology (prompt effect 09): transplant patches from earlier frames using content matching.
  - Keep a bounded reservoir of old frames or patches. Cache inexpensive descriptors such as color mean/variance, gradient histograms, or downsampled pixels; sample candidates instead of searching every patch in every frame.
  - Match current regions to historical patches by color, texture, or edge compatibility. Include a controlled mismatch mode. Support hard copy and soft/feathered boundaries; every replacement must come from actual video history, not random rectangles.
  - Options: history capacity/seconds, patch size, candidates per frame, analysis interval, match metric/threshold, mismatch strength, replacement chance, boundary mode, displacement, seed.
  - Test patch provenance, distinct matching modes, zero-intensity passthrough, deterministic selection, and bounded history memory.
- [ ] E2.48 - anti-motion compensation (prompt effect 15): estimate motion, then amplify it instead of stabilizing it.
  - Reuse a shared flow field if the pipeline can safely provide one; otherwise estimate dense flow from previous/current grayscale frames at a configurable analysis resolution, starting with Farneback flow if OpenCV is accepted. Smooth and clamp vectors, apply a tunable gain, and remap the current frame. Verify the displacement sign with a synthetic moving shape before tuning the look.
  - Options: flow gain, nonlinear gain, motion threshold, analysis resolution, smoothing, max displacement, optional trail amount/decay, seed for randomized modulation.
  - Handle the first frame without history. Test direction/sign, stronger displacement at higher gain, relative stability on static footage, valid shape/dtype, and CPU cost.
- [x] E2.49 - causality inversion (prompt effect 20): let selected regions borrow pixels from later video frames.
  - Use a bounded look-ahead buffer of frames and timestamps. Start with future-frame blend and replacement; add region tearing or motion alignment as separate modes. Preserve output timestamps and dimensions.
  - Document the look-ahead delay. Live input cannot provide genuine future frames without buffering or prediction. Near clip end, reduce the blend or use the latest available frame.
  - Options: look-ahead seconds, blend/replacement strength, offset, region/mask mode, mask speed, motion alignment, edge feather, seed.
  - Test that a moving synthetic object appears early by the configured offset, blend and replacement differ, unavailable future frames take the documented fallback, and memory/timestamps remain bounded and valid.
  _Done: `inversion` adds bounded look-ahead with separate blend, replacement,
  band-tearing and CPU-flow-aligned modes. Region masks, speed, feathering,
  strength, horizon/offset, alignment and seed are configurable. Queues cap at
  128 frames/128 MiB, reject impossible nominal settings and clamp dense VFR
  bursts/EOF/cuts to available future content. Streaming stages retain rational
  PTS and dimensions through optional PyAV/NUT transport inside the existing
  FFmpeg render lifecycle; compatible unfiltered audio is copied. Tests verify
  early moving objects, distinct modes, ownership, resets/cuts, budget bounds,
  EOF, exact VFR/nonzero-start PTS, audio packet timing, stage ordering, short
  writes, failure/cancel cleanup and the menu. Separate 360p/720p benchmarks for
  all four modes are saved in benchmarks/inversion.json. README documents
  buffering latency, live-input limits, fallback and optional dependencies._

---

## section 3 - engine work

- [ ] E3.1 - effect model: allow `filter_complex` (and multiple inputs/outputs) so
      boomerang, mirror, kaleidoscope, glow and gif export fit the same registry
- [x] E3.2 - effect params: per-effect options with defaults, a menu step and cli flags
  _Done 2026-09-24: see M1.5. choice / bool / int / size kinds, defaults declared
  with the param, values validated in one place (coerce_param) so the menu and
  `--set` reject the same things, and the preview rebuilds the plan on every
  keypress so a bad combination shows up before the encode instead of after._
- [x] E3.3 - chaining: stack 2+ effects in one pass, reusing each builder's chain
  _Done 2026-09-25: a Chain (one effect or more, in order) is what the pipeline
  works on now; a chain of one behaves exactly as before, so nothing else had to
  change. plan_for_chain builds every effect, joins the vf chains and the af
  chains with commas, tags the notes per effect and names the output after all
  of them (clip_STRETCHED+DEEPFRIED.mp4). each effect is handed the frame the
  previous one produced (EffectContext.frame, threaded through _source_dims,
  and pixelize's "keep" branch reads the working frame instead of the clip), so
  stretch+pixelize pixelates the stretched 1080x1920 frame and not the 640x360
  source. options are keyed per effect: --set key=value still works while one
  effect in the chain has that key, two effects sharing one (zoom and blockswap
  both take speed) is refused with both qualified forms named, --set
  effect.key=value always works, and --size lands on the first effect with a
  shape option. menu: space chips a row, the chip number is the run order, c
  clears, and the options screen shows one labelled block per effect (headers
  skipped by the arrow keys) plus one filter line per effect, because a merged
  chain does not fit one line. single-pass effects only: datamosh and
  timescratch refuse to be chained (the menu says so when you try to chip one,
  the cli exits 2), because their run callbacks would have to feed the earlier
  chain into their own first pass - that is E3.16. two real bugs surfaced:
  the run callbacks read their options flat off the ctx, so nesting the option
  dict silently swallowed every --set for datamosh (heavy and light cut rendered
  identically) and timescratch (sine and swing came out the same length) - both
  went green again once process() hands the runner its own flat opts; and the
  chain picker first inserted its marks after padding the row, which would have
  tripped the layout width checks. difflib now offers a fix after a typo in an
  effect name (strecth -> did you mean stretch?), E4.12 is still open for the
  same thing on --set option names. version 0.10.0. cli 168 -> 187 checks, menu
  100 -> 121 checks (the chain pty test needed a wait_last helper: the pty
  buffer keeps every frame, so waiting for "pick an effect" on the whole buffer
  matched the first visit and let the test race ahead of the app), layout (73)
  and install (15) untouched._
- [x] E3.4 - batch mode
  _Done 2026-09-24: `effex stretch a.mp4 b.mp4 ...` queues them, asks once when a
  terminal is attached, renders each with its own result line, continues past a
  failure and exits 1 if any failed. `--json` prints one object per render._
- [x] E3.5 - preview: a 3 second taste before committing to a long encode
  _Done 2026-09-24: `p` on the options screen renders the current options from
  the middle of the clip (a 5s+ clip previews 3s starting at mid-1.5s, shorter
  clips preview whole) into output/ as name_EFFECT_PREVIEW.mp4 with the same
  encoder settings, bar and partial-file cleanup as a full render; the result or
  the failure shows up as a notice on the options screen. tested through the pty:
  the preview lands in output/ at 2.0-4.5s long and a full render straight after
  still works._
- [x] E3.6 - `--dry-run`: print the exact ffmpeg command, run nothing
  _Done 2026-09-24: prints the command (including the option-derived filter chain)
  and writes nothing; the tests use it to assert option handling without encoding._
- [ ] E3.7 - config file: effex.conf key=value (default shape, crf, preset,
      default effect, animations on/off), template written on first run
- [ ] E3.8 - hw encode: use vaapi / nvenc / videotoolbox when detected, libx264 fallback
- [ ] E3.9 - output manager: list previous renders from the menu, delete one, reveal
      the folder
- [x] E3.10 - cleanup: delete partial files when a render is cancelled or fails
  _Done 2026-09-24: cancel or a non-zero ffmpeg exit unlinks the half-written
  output before reporting, so a failed render never leaves a file that looks real.
  previews get the same treatment._
- [ ] E3.11 - naming: effect name plus date in the output filename, not just the suffix
- [x] E3.12 - number params: min/max/step with left/right stepping and typing
  _Done 2026-09-24, extended 2026-09-24 with float and percent kinds: Param
  gained min/max/step, left/right step and clamp, enter opens a typed prompt,
  and out-of-range or non-numeric input is refused with the limits named.
  first user was pixelize's block size; rgbsplit, deepfry, wobble and spin use
  it now too. floats step in 0.5s (spin's seconds per turn), percents accept
  "55" or "55%" (deepfry's strength), and a multi kind takes comma-separated
  values on the cli and a checkbox screen in the menu. see M1.12._
- [ ] E3.13 - option presets: save the current option set as a named preset per
      effect, plus a reset-to-defaults row in the options menu
- [ ] E3.14 - remember the options used last for each effect within a session
- [ ] E3.15 - frame size guards in the pipeline, not in each builder: odd
      dimensions, 1-pixel frames, and sources ffprobe cannot read
- [ ] E3.16 - a chain that ends in a multi-pass effect: datamosh / timescratch
      after one or more single-pass effects - the earlier chain has to reach
      their first pass (temp pre-encode or a filter_complex input), which is the
      same missing shape as E3.1

---

## section 4 - polish + packaging

- [x] E4.1 - install.sh symlink into ~/.local/bin (house convention)
  _Done 2026-09-24: idempotent, resolves the repo root from its own location,
  warns when ~/.local/bin is not on PATH. verified: symlink created, `effex
  --version` works from anywhere. effex.py already resolved its own realpath, so
  the symlinked entry point needs no extra code._
- [x] E4.2 - tests/: pty keypress smoke, cli assertions, layout, missing-ffmpeg path
  _Done 2026-09-24, extended 2026-09-24 with the layout suite and 2026-09-24 with
  the effect and preview checks: cli covers every effect (dry-run chains, real
  renders with ffprobe-verified dimensions and the 8/1 stutter frame rate, range
  rejection for the number params), menu_smoke drives wobble and the preview key
  through a real pty, layout checks every frame at three terminal sizes,
  install_prompt uses a fake PATH with apt-get and sudo shims. writing these
  caught four real bugs: per-keypress terminal mode toggling ate arrow escape
  sequences, the install prompt outside the cbreak session blocked on a single
  key, --check --json mixed human text into stdout, and menu Items without a
  value made the main menu dispatch fall through._
- [x] E4.3 - README: one section per effect with its filter chain
  _Done 2026-09-24: README covers the menu flow screen by screen, the key list
  (preview included), every flag, all eight effects with their option tables and
  real filter chains, install.sh, and the test commands._
- [x] E4.4 - banner art in brand color, tty only
  _Done 2026-09-24 as a logo above the banner, then removed again in M1.8: the
  user asked for the ascii art to go. colours now carry the design instead._
- [ ] E4.5 - probe with a spinner: ffprobe on a multi-gigabyte file takes a moment
- [x] E4.6 - `r` to rescan input/ without leaving the picker
  _Done 2026-09-24: rescan callback on the selector; the video picker rebuilds its
  rows in place and keeps the cursor sane. the empty state also offers r, so a
  video dropped in while the menu is open shows up without a restart._
- [ ] E4.7 - remember the last used effect and video for the session
- [ ] E4.8 - README notes for windows and macos (the original script was windows-first)
- [ ] E4.9 - optional single-file build (pyinstaller) for machines without python
- [x] E4.10 - wide-terminal layout check
  _Done 2026-09-24: tests/layout.py runs the whole flow at 60x20, 80x24 and
  120x40 and asserts nothing wraps, rows share one width and the columns line up.
  the layout is width aware from 48 to 140 columns; the note on the right of the
  header is dropped rather than truncated into the row labels when space runs out._
- [ ] E4.11 - one-line result screen while a batch runs, so a long queue shows
      progress instead of a wall of text
- [ ] E4.12 - `--set` completion hints: after a typo, suggest the closest option
      name (difflib.get_close_matches)
- [ ] E4.13 - static checks wired into the test driver: pyflakes on effex.py and
      the tests themselves, failing the suite on an undefined name

---

## section 5 - later / wild ideas

feasibility noted so nobody spends a week on them by accident.

- datamosh extras now shipped: configurable dice survival (0-100%) and
  explicit deterministic seed; append a second video into the same mpeg4
  motion stream with start/length trims and auto-concatenated audio. The menu
  selects the second input, the CLI accepts `--set second=path`, and source
  formats are normalized before encoding. still on the table: motion-vector
  transplants (ffglitch) and bloom/stutter p-vop duplication. note: "no
  keyframes at all" was tried and removed - an mpeg4 stream without its
  first i-vop decodes to zero video frames, so total mode cannot ship.
  section 6 has the sources.
- video to ascii in the terminal: ffmpeg's caca output device (needs libcaca) or a
  custom frame reader. renders live, not to a file.
- audio reactive effects: one analysis pass (astats/ebur128) then feed the values
  into expressions - possible, medium effort, needs a two-pass pipeline.
- frame interpolation mush: `minterpolate` - exists, but slow enough to need a
  preview step first.
- looped render: make a ping-pong montage with an audio crossfade at the seam.
- seeded random glitch: random() driven noise/displace with a fixed seed, so a
  render can be reproduced from the seed alone.
- overlay a second video (input/extra/) with blend modes for double exposure.
- effect on a time range only: glitch the middle 5 seconds, leave the rest clean.
- plugin folder: drop a .py into effects/ and it registers itself in the menu.
- render history as json: every render appended with settings, so results can be
  reproduced or compared.
- side-by-side compare: render the original and the effect stacked, for showing
  the difference to someone else.

---

## section 6 - research: open source techniques with links

scoured 2026-09-24. every entry names the repo, what it does, and which roadmap
item it feeds. links were live at the time of writing.

### datamosh (landed as E2.25 - these shaped it)

- https://github.com/ramiropolla/ffglitch-core - THE tool: an ffmpeg fork that
  lets scripts read and rewrite motion vectors and dct blocks mid-decode
  (ffedit/ffgac). the gold standard; build-heavy, do not vendor.
- https://github.com/ramiropolla/ffglitch-scripts - community scripts for the
  above (free-for-all directory).
- https://ffglitch.org/frontends/ - the frontends page (Live Mosher,
  Datamosher Pro, datamoshing-GUI) - a list of the knobs users expect.
- https://github.com/tiberiuiancu/datamoshing - mosh.py: i-frame removal,
  p-frame duplication and vector-motion moshing, plain python.
- https://github.com/happyhorseskull/you-can-datamosh-on-linux - the ancestor
  of the above, tutorial-style.
- https://github.com/mwolski16/datamosh - h264 cli datamosh: prep re-encode
  with a long gop, strip idr slices, remux mp4. closest in shape to effex's
  own two-pass pipeline; has mosh windows and presets.
- https://github.com/JesseRWeigel/datamosh-toolkit - bitstream edits on
  avi/mpeg4 part 2 with --propagate N to bound the corruption length - the
  idea behind effex's hold param. refuses sources it cannot mosh safely.
- https://github.com/diegochavez-io/datamosh-tools - three methods (avi,
  h264 nal, ffglitch) plus pixelmosh.py with 23 pixel effects (tear, chanshift,
  pixsort, dropout, melt, ghost...) - a catalogue of effect ideas by itself.

### vhs (feeds E2.5, E2.12 and the crt family)

- https://github.com/valadaptive/ntsc-rs - accurate ntsc/vhs artifact
  emulation in rust: luma/chroma separation, tracking wobble, dropouts. web
  demo at https://ntsc.rs - the reference for what vhs should look like.
- https://github.com/LMP88959/NTSC-CRT - c/c++ library decoding ntsc video
  the way a crt tv would. build-heavy; borrow the ideas, not the code.
- https://github.com/F33RNI/NTSC-VHS-Renderer - gui wrapper around the above
  with bloom, interlacing and brightness/contrast recipes.
- https://gist.github.com/AARomanov1985/4d52d2b803eaa06fbf83158ec6095292 -
  stock-ffmpeg vhs recipe: scale to 480, eq, noise=alls, audio bandpass and
  tape-hiss mix - directly portable into effex's single-pass model.
- https://gist.github.com/netlooker/7990869a6a5b8e6f3cf8284c48be8fde - same
  family, hiss mixed at low volume.

### circuit bending / analog glitch (new backlog family)

- https://github.com/clickysteve/bendr - emulates the circuit-bent video gear
  look (bent enhancers, dying vhs decks, unstable sync, rescan feedback) in a
  web app - names the artifacts to fake.
- https://github.com/CTXz/video-glitch-array - real analog vga signal
  manipulation; good vocabulary for what the glitches actually are.
- circuit-bending vocabulary worth an effex effect each: horizontal sync
  tear, chroma desync/bleed, rescan feedback, dropout lines, enhancer color
  crush. ffglitch (above) is circuit bending applied to motion vectors:
  amplify, invert or noise them.

### psychedelic / trippy (feeds E2.29-E2.43, the bleood look)

- the reference: bleood (jamari spencer brown, florida underground rap) -
  https://music.apple.com/us/artist/bleood/1744999836 and
  https://www.discogs.com/artist/16008147-bleood - "unorthodox and distorted".
  the look: crushed codec ruins, rgb splits, datamosh, blown-out color,
  strobes, warps - the whole family layered, never one clean filter.
- https://github.com/ekomsSavior/ARTFORGE - glitch framework with the REAL
  corruption vocabulary: snow codec corruption, x265 slice/block glitching,
  datascope overlays, grid-warp deformation, and the complete ffmpeg blend
  mode catalogue (phoenix, heat, freeze, hardmix, negation...). philosophy
  note: "avoids fake effects, leans into genuine compression artifacts".
- https://github.com/Apolotary/blob-tracker/blob/main/references/postfx-glossary.md
  - THE psychedelic math reference: video-synth lineage straight from
  Tachyons+ circuit-bent ntsc enhancers (Vortex Decoder, Psychenizer,
  Opti-Glitch), with exact formulas - feedback loop (rotate t*6deg, scale
  1.02, blend 0.55 decay), lagfun phosphor trails (out = max(prev*R, curr)),
  radial ripple warp, threshold pixel sort.
- https://github.com/willbearfruits/videoglitchcrazynes - glitch engine with
  channel lobotomy, strobe/invert, wave warp, slice displace, camera shake,
  frame shuffle - and a full audio-reactive (librosa beat sync) layer:
  every effect gates on the beat. the E2.41 template.
- slit scan (feeds E2.35): https://github.com/philintheattic/video-tools
  (videoslitscan: stripes offset in time), https://github.com/Pantsworth/slit_scanner_darkly
  (single + moving slitscans), https://github.com/zzkt/slitscan (racket, from
  the oioiiooixiii temporal slice stacking blogpost).
- https://github.com/transitive-bullshit/ffmpeg-gl-transition - GLSL shader
  transitions inside ffmpeg (E2.40): Shadertoy warps become filters, at the
  price of a custom ffmpeg build.
- https://superuser.com/questions/736388/kaleidoscope-effect-with-ffmpeg -
  the crop + hflip + hstack/vstack recipe behind E2.19 / the mirror maze.

### pixel sorting (backlog - needs numpy or a slow pure-python sort)

- https://github.com/kimasendorf/ASDFPixelSort - the original processing
  sketch that started pixel sorting.
- https://github.com/satyarth/pixelsort - python port with a cli; lightness/
  hue/saturation sort keys, interval and angle options.
- https://github.com/rkargon/pixelsorter - pixel sorting library: sort paths,
  edge detection, animated sorts across frames.
- https://github.com/chinatsu/pixelsortframes - applies the sort per frame of
  a video - the effex-shaped loop.
- https://github.com/DavidMcLaughlin208/PixelSorting - c++/openframeworks,
  ~10x faster; proves the sort itself is the bottleneck, not the video io.

### glitch engines to mine for ideas

- https://github.com/willbearfruits/videoglitchcrazynes - audio-reactive
  glitch editor: rgb shift, channel lobotomy, pixel sort, echo trails, wave
  warp, strobe, databend, vhs decay, flow smear - a whole section 2 in one
  repo.
- https://github.com/diegochavez-io/datamosh-tools (pixelmosh.py) - the 23
  pixel effects with names and parameter shapes.
