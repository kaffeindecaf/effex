# Reverse Infection

In **CODEC & BLOCKS → datamosh → algorithm**, select **Reverse Infection**.
It propagates future visual material backward through consecutive optical-flow
fields, with held/overdriven prediction vectors and repeated image tiles. Events
can contaminate an empty scene before their subjects arrive. Infected blocks
replace pixels at full color strength; decay reduces their coverage instead of
fading a silhouette. Growth copies complete textured tiles, including displaced
background detail, rather than dilating color into a cloud.

The [five-second comparison](examples/reverse-infection/comparison.mp4) shows
the source on the left and the Effex export on the right. The red figure enters
at 2 seconds. [Source](examples/reverse-infection/source.mp4),
[effect only](examples/reverse-infection/infected.mp4), and
[contact sheet](examples/reverse-infection/contact-sheet.png) are included.

## Run it

Linux/macOS, from the repository (FFmpeg and FFprobe must be on PATH):

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-reverse-infection.txt
python effex.py datamosh input/clip.mp4 --set algorithm=reverse_infection --set lookahead_frames=24 --set seed=42 --output output/infected.mp4
```

Windows uses the same packages and command after `.venv\Scripts\Activate.ps1`.
The existing installers already install NumPy, headless OpenCV and PyAV when
optional dependencies are requested. This algorithm uses CPU packages, with no
FFglitch executable, GPU or machine-learning model. It retains the existing
standalone-datamosh restriction on chaining.

For a timestamp-based horizon, including variable frame rates:

```sh
python effex.py datamosh input/clip.mp4 --set algorithm=reverse_infection --set lookahead_mode=seconds --set lookahead_seconds=0.8
```

In the options menu, **p** exports the first three seconds through the same
processor, decoding additional future context before stopping the decoder.
The preview therefore waits for its lookahead; it is a rendered preview rather
than a live camera effect. Audio follows the preview length. Full exports retain
video presentation timestamps, durations and dimensions through the existing
FFmpeg/PyAV NUT transport. Compatible audio packets are copied; container clock
conversion can round their timestamps (for example Matroska milliseconds to MP4
audio sample time). `audio=none` removes audio; `auto` and `first` retain it.
Second-source modes are rejected for this algorithm.

## Parameters

All parameters use the existing menu and `--set key=value` handling. Values are
validated, and `serialize_config()` contains only JSON-compatible settings. Effex
keeps menu options in its existing session dictionary and reports them in CLI
JSON; this change does not introduce a separate configuration store.

| Parameter | Default | Range / meaning |
| --- | --- | --- |
| `intensity` | 0.75 | 0–1, corrupted block coverage; zero bypasses immediately |
| `lookahead_frames` | 24 | 0–128 future frames in frame mode; zero bypasses |
| `infection_strength` | 0.8 | 0–1, coverage of full-strength prediction blocks; zero bypasses |
| `backward_decay` | 0.95 | 0–1, mask retention at every backward step |
| `spread_radius` | 3 | −16–16 pixels per step; negative values erode |
| `block_size` | 16 | 1–128 pixels, fragment/displacement grid |
| `motion_alignment` | 1.0 | 0–1, consecutive flow sampling gain |
| `mutation_strength` | 0.25 | 0–1, held/overdriven vectors and block displacement |
| `window_overlap` | 8 | 0–64 reconstructed frames switched by block between windows |
| `seed` | 42 | 0–2147483647; zero retains Effex's source-derived seed convention |
| `lookahead_mode` | frames | `frames` or `seconds` |
| `lookahead_seconds` | 1.0 | 0–5 seconds of original PTS in seconds mode |
| `flow_scale` | 0.5 | 0.125–1, analysis fraction, capped at a 320-pixel longest side |

The datamosh selector now defaults its shared seed to 42. Existing algorithms
still accept `seed=0` for their previous source-derived behavior. Codec presets
remain available for the codec algorithms; they do not change infection knobs.

## Implementation

`run_datamosh` selects `ReverseInfection` as a streaming `FramePacket` processor
inside `run_stateful_chain`. Decoding, audio mapping, export, cancellation and
partial-output cleanup remain in Effex's existing renderer. Optional packages
are imported only when the processor initializes, so the standard-library-only
CLI listing continues to work.

1. Buffer owned RGB snapshots, original packet metadata, downscaled grayscale
   images and adjacent flow fields. Frame mode waits for its frame horizon.
   Time mode uses actual packet timestamps and selects no source beyond the
   horizon, including when there is a large VFR gap. Capacity also bounds time
   mode when timestamps are unusually dense.
2. Measure **forward** Farneback flow from each original frame to its successor.
   Bilinear upsampling scales horizontal and vertical vector magnitudes by their
   respective resolution ratios. Given `F(t→t+1)`, sample future material at
   `(x+F.x, y+F.y)` with `cv2.remap`. The positive sign is deliberate: remap asks
   where to sample the future for each earlier destination pixel. Tests verify
   both real translated textures and a controlled multi-step translation.
3. Seed prediction blocks from future-versus-window-start RGB changes, rejecting
   isolated ringing noise. Step backward through **every** adjacent motion field.
   Quantize and overdrive vectors by block, hold a seeded share of vectors, and
   apply stepped displacements held for three source frames. Nearest-neighbor
   sampling preserves damaged texture rather than smoothing repeated warps.
4. Grow through neighboring prediction blocks on a schedule controlled by spread
   and motion. Copy a complete tile from the strongest damaged neighbor into new
   or weakly damaged support, including its texture and background. Repeated copies
   carry already-corrupted visual content. Negative spread erodes block support.
   Mask decay and strength determine which whole blocks replace original pixels;
   there is no RGB alpha fade, additive feedback, or channel-max color dilation.
5. A full window contains at most `lookahead+1` source frames. Emit its prefix,
   retain the original overlap plus source frame, and cache overlap RGB
   reconstructions. The next window switches whole blocks from old to new with
   increasing coverage across the overlap. Each chosen pixel comes from one
   reconstruction of the **same timestamp**, avoiding translucent window joins.
   Stable seeded spatial fields and source-time displacement avoid restarting
   randomness. Fast footage can still reveal some window structure.
6. Detected RGB scene cuts or explicit `scene_cut` packets drain the previous
   scene before accepting the new one. Seeks, non-increasing PTS, index gaps or
   source changes discard stale timeline state. EOF processes only remaining
   in-video frames, shortening its future horizon and leaving the final source
   frame intact. Empty, single-frame and shorter-than-lookahead clips work.

The processor budgets queued originals, pending backward reconstructions,
cached overlap, low-resolution flow, and conservative full-resolution working
space against **512 MiB**, with at most 129 queue/overlap slots. It reduces
effective overlap and lookahead before processing and reports this in runtime
notes. A frame too large for the working budget fails with a resize hint.
The budget concerns processor arrays; interpreter/native decoder/encoder memory
and memory allocator caches are additional. Input duration does not increase
the number of retained frames. Optical flow is an approximation: occlusion,
large motion and cuts can distort silhouettes, which is part of the corruption.

## Reproduce and verify

```sh
python tests/run_tests.py reverse-infection
python benchmarks/reverse_infection.py --output-dir output/reverse-infection
python benchmarks/reverse_infection.py --width 1280 --height 720 --frames 72 --benchmark-only --output-dir output/reverse-infection-720p
```

The benchmark generates a stationary room and a walking colored figure, renders
the source through the actual Effex CLI, makes a comparison/contact sheet, and
writes JSON measurements. Timing excludes allocation tracing; a separate pass
measures Python/NumPy allocations. Process peak RSS includes imports and native
libraries. No entire clip is retained during processing.

Measured on Windows 11, Python 3.14.7, NumPy 2.5.4, OpenCV 5.0.0, one OpenCV
thread, with the defaults above. These are CPU measurements, not guarantees of
interactive playback speed; throughput varies with resolution and scene content.

| Resolution / clip | Processor fps | Peak traced allocations | Peak process RSS | Max buffered frames |
| --- | ---: | ---: | ---: | ---: |
| 320×180 / 120 frames | 89.9 | 17.6 MiB | 60.8 MiB | 25 |
| 320×180 / 480 frames | 68.6 | 17.6 MiB | 62.2 MiB | 25 |
| 1280×720 / 72 frames | 5.25 | 243.2 MiB | 288.0 MiB | 25 |
| 1920×1080 / 30 frames | 1.90 | 367.0 MiB | 409.3 MiB | 14 |

The 120-frame CLI example, including decoding/encoding, took **2.00 seconds**.
The longer small clip retained the same 8.9 MiB of processor state and had the
same traced allocation peak. At 1080p the memory calculation reduced the horizon
to 13 frames and overlap to 6. The 30-frame 1080p sample is the room-only prefix
of the generator; the smaller examples include the moving figure. Raw reports:
[example](examples/reverse-infection/measurements.json),
[long stream](examples/reverse-infection/measurements-long.json),
[720p](examples/reverse-infection/measurements-720p.json),
[1080p](examples/reverse-infection/measurements-1080p.json).

The regression suite has **16 tests** covering early future-only color,
consecutive propagation and verified flow direction/scaling, growth versus
erosion, seeds/reset/config round trips, packet ownership, VFR metadata and
timestamp horizons, short/empty/EOF clips, disabled identity, brightness bounds,
scene cuts/seeks/source changes, long-stream capacity pressure, overlap joins,
selector/menu validation, real VFR export, copied audio and preview with/without
audio. Added visual regressions require intact textured tile copies, full-strength
palette colors instead of halos, held/overdriven vectors and overlap joins without
intermediate RGB colors. The suite runs on Ubuntu and Windows CI.

Additional local validation: existing datamosh, self-mosh, pulse-mosh, bitstream,
stateful, flowmosh, CLI and platform suites pass. Thirteen existing inversion
tests pass, including timestamped renders and stage composition. Its interactive
done-screen, POSIX environment-layout and signal-cancellation tests were excluded
from that Windows subset; the interactive test blocks on Windows console input.
The POSIX terminal layout suite skips on Windows. Linux execution is delegated
to Ubuntu CI because WSL cannot start on this host (required service unavailable).

## References

This is an independent implementation, with no copied repository code. Design
references studied:

- [OpenCV Farneback convention](https://docs.opencv.org/4.x/dc/d6b/group__video__track.html)
  and [destination-to-source remap sampling](https://docs.opencv.org/4.x/da/d54/group__imgproc__transform.html).
- [FFglitch](https://ffglitch.org/), its
  [motion-vector averaging tutorial](https://ffglitch.org/2024/06/mv_avg_010.html)
  and [documentation](https://ffglitch.org/docs/0.10.2/): temporal motion state and
  block corruption informed the visual approach; FFglitch's bitstream editing
  remains distinct from this custom RGB reverse-propagation algorithm.
- [Datamosher-Pro effect descriptions](https://github.com/Akascape/Datamosher-Pro):
  motion transfer, macroblock glide/shift and buffer effects as visual references.
