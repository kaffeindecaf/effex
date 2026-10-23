# effex

ffmpeg effects from a terminal menu. pick a video, choose an effect, adjust its knobs, and render to `output/`.

## quick start

from the project folder, run:

```sh
./effex.py
```

drop clips in `input/`. the effect picker groups looks by type. use `[` and `]`
to jump between groups, `space` to build a chain, and `enter` to choose.
arrow keys tune settings, `p` previews single-pass effects, and `r` renders.
the render screen shows progress, frame count, elapsed time, speed and an
estimated finish.

to put `effex` on your PATH:

```sh
./install.sh
```

needs Python 3.8+ and FFmpeg. `ffprobe` adds clip details and is required for two-video datamosh and stateful effects. The built-in filters use Python's standard library; temporal sediment also needs NumPy:

```sh
python3 -m pip install -r requirements-stateful.txt
```

Elastic collapse additionally uses headless OpenCV for optimized CPU remapping,
resizing, blur and gradient analysis. Install it in your Python environment:

```sh
python3 -m pip install -r requirements-collapse.txt
```

These packages are optional and loaded only when rendering stateful effects.
Ordinary FFmpeg effects and `--list` still work without them.

## commands

```sh
./effex.py --list
./effex.py --check
./effex.py stretch input/clip.mp4 --set target=16:9
./effex.py stretch+deepfry input/clip.mp4 --set deepfry.strength=60
./effex.py lobotomy+neon input/clip.mp4 --set lobotomy.kill=g --set neon.period=2
./effex.py sediment input/clip.mp4 --set intensity=70 --set layers=3 --set excavation=10 --set seed=123
./effex.py sediment+deepfry input/clip.mp4 --set sediment.decay=30 --set sediment.color=warm --set deepfry.strength=60
./effex.py collapse input/clip.mp4 --set intensity=80 --set seed=17
./effex.py collapse input/clip.mp4 --set mode=seam --set seam_strength=80
./effex.py datamosh input/a.mp4 --set second=input/b.mp4 --set cut=dice --set survival=50 --set seed=123
./effex.py stretch input/clip.mp4 --dry-run
```

`--set key=value` changes an effect option. use `--size 1920x1080` for a shape, `--output path.mp4` for an exact path, and `--force` to overwrite. without `--force`, effex adds `_2`, `_3`, and so on instead of replacing a render.

## what it does

- effects include stretch, pixelize, deepfry, vhs/crt, dropout, trails, datamosh, timescratch, and temporal sediment.
- stack single-pass effects with `+`, such as `stretch+deepfry`; order matters.
- temporal sediment layers the current image over a bounded 1-3 layer history. Tune deposit, decay, motion sensitivity, excavation chance, color memory, and seed; it works with single-pass filters before or after it.
- datamosh can join two videos in one codec stream. pick the second clip in the menu or pass `--set second=path`; set the trim, keyframe survival, seed, fill, grit, and audio.
- single-pass previews, render progress, ctrl-c cancellation, and cleanup of partial renders.

datamosh and timescratch use their own multi-pass pipelines, so they cannot be chained with other effects. Temporal sediment uses a bounded 128 MiB history buffer. Stateful effects resample input to the probed frame rate, do not preserve original variable-rate timestamps, and have no preview yet. Multiple stateful processors must be adjacent; FFmpeg filters can go before and after them.

## channel lobotomy

`lobotomy` crushes each RGB channel independently, removes an optional source
channel, then swaps the survivors. Find it under **COLOR & TEXTURE** in the menu.

| option | values | default |
| --- | --- | --- |
| `red` | 2, 4, 8, 16, 32, 64, 128, 256 levels | 8 |
| `green` | same level choices | 4 |
| `blue` | same level choices | 8 |
| `kill` | none, r, g, b | g |
| `swap` | none, rb, rg, gb, roll | rb |

The filter order is `format=rgb24,lutrgb,colorchannelmixer,colorchannelmixer`:
quantization uses `floor(val/Q)*Q` with `Q=256/levels`; kill happens **before**
swap, so killing red and swapping red/blue leaves the output blue channel empty.
Set all three levels to 256 with `kill=none` and `swap=none` for unchanged colors.

## neon hue cycle

`neon` swings the hue back and forth, boosts saturation, and punches contrast
with an S-curve. It is also under **COLOR & TEXTURE**.

| option | range | default |
| --- | --- | --- |
| `amount` | 0-180 degrees of hue swing | 120 |
| `period` | 0.5-30 seconds per full cycle | 4 |
| `saturation` | 100-300% | 200% |
| `punch` | 0-100% curve strength | 50% |

The filter is `hue=h='A*sin(2*PI*t/P)':s=S,curves=all='...'`.
`h` uses degrees, not radians. Saturation at 100% is unchanged; punch at 0%
disables the curve. Set `amount=0`, `saturation=100` and `punch=0` to disable
the whole effect. Both effects preserve frame size and audio, support `p`
previews, and chain in either order without extra dependencies.

## content-aware elastic collapse

`collapse` lives under **SHAPE & MOTION**. The default `elastic` mode evolves
seeded, spatially nonuniform displacement fields. Gradient-rich regions resist
deformation; motion can amplify it elsewhere. Fields are analyzed at no more
than 192 pixels on the longest side, smoothed in time, then resized into
full-frame maps for CPU `cv2.remap`. This is not a uniform zoom. Reflective
sampling keeps edges filled and output dimensions never change.

| option | range / values | default |
| --- | --- | --- |
| `intensity` | 0-100%, displacement strength | 65% |
| `horizontal` | 0-100%, horizontal deformation scale | 65% |
| `vertical` | 0-100%, vertical deformation scale | 45% |
| `preserve` | 0-100%, content/edge protection | 75% |
| `mode` | elastic, seam | elastic |
| `seam_strength` | 0-100%, offline seam removal amount | 60% |
| `motion` | 0-100%, elastic motion influence | 50% |
| `smoothness` | 0-100%, spatial field smoothness | 70% |
| `speed` | 0-4 elastic cycles per second | 0.35 |
| `coherence` | 0-100%, elapsed-time field smoothing | 80% |
| `seed` | 0-2147483647; 0 derives phases from the source | 0 |

The optional `seam` mode is an offline content-aware carve, not a full-resolution
seam search on every frame. A gradient-energy dynamic program removes at most
four seams per axis from the reduced frame, at most once every 0.2 seconds of
video. Source coordinate maps are carved alongside the image, resized to the
original dimensions, and temporally smoothed before sampling the current frame.
This smooths seam decisions rather than crossfading old pixels. Seam mode uses
`seam_strength`, the two axis scales, preservation, smoothness, coherence and
intensity. Elastic motion, speed and random phases do not drive seam selection.

Zero intensity is an exact passthrough; two zero axis scales disable both modes.
Zero seam strength disables seam mode. The first frame is deformed immediately.
State resets on a new input/shape, explicit reset, rewind, frame discontinuity,
explicit scene cut, or a large RGB scene change. Persistent analysis state is
bounded independently of clip length; full-frame remap buffers are transient.
Chains such as `neon+collapse+sediment+deepfry` reuse the shared decoder,
encoder, audio handling, progress, cancellation and partial-file cleanup.

Benchmark both modes separately (requires the optional dependencies):

```sh
python3 tests/benchmark_collapse.py --frames 120 --output benchmarks/collapse.json
```

The report measures processor frame time and peak process RSS at 640x360 and
1280x720 in fresh workers. It excludes FFmpeg decode/encode and is not an
end-to-end playback guarantee. Seam mode also reports carving-update time.

Recorded on this Linux x86_64 host with one OpenCV CPU thread, 120 frames per
case, Python 3.14, NumPy 2.5.4 and OpenCV 5.0 (raw data in
`benchmarks/collapse.json`):

| mode | frame size | mean ms/frame | p95 ms/frame | peak process RSS |
| --- | --- | --- | --- | --- |
| elastic | 640x360 | 4.55 | 7.02 | 72.8 MiB |
| elastic | 1280x720 | 11.57 | 12.30 | 119.1 MiB |
| seam | 640x360 | 3.43 | 6.37 | 68.1 MiB |
| seam | 1280x720 | 9.72 | 13.20 | 119.1 MiB |

Seam averages include frames reusing a cached map; carving-update frames alone
averaged 6.68 ms at 360p and 12.87 ms at 720p. Persistent state was about
0.38 MiB for elastic and 0.53 MiB for seam mode in these cases. These are
measurements of this fixture on this host, not a promise for every input.

## files and tests

`input/` holds source clips. `output/` holds renders. Git ignores files in both folders except `.gitkeep`, so clips and renders stay local. Both paths can be changed with `--input-dir` / `--output-dir` or `EFFEX_INPUT_DIR` / `EFFEX_OUTPUT_DIR`.

run the test suite with:

```sh
python3 tests/run_tests.py
python3 tests/run_tests.py psychedelic  # pixel behavior, options and real renders
python3 tests/run_tests.py collapse seams  # deformation, carving and shared pipeline
```

tests use generated clips and temporary folders; they do not overwrite files in `input/` or `output/`.
