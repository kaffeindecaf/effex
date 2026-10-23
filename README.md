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

needs Python 3.8+ and FFmpeg. `ffprobe` adds clip details and is required for two-video datamosh and temporal sediment. The built-in filters use Python's standard library; temporal sediment also needs NumPy:

```sh
python3 -m pip install -r requirements-stateful.txt
```

## commands

```sh
./effex.py --list
./effex.py --check
./effex.py stretch input/clip.mp4 --set target=16:9
./effex.py stretch+deepfry input/clip.mp4 --set deepfry.strength=60
./effex.py lobotomy+neon input/clip.mp4 --set lobotomy.kill=g --set neon.period=2
./effex.py sediment input/clip.mp4 --set intensity=70 --set layers=3 --set excavation=10 --set seed=123
./effex.py sediment+deepfry input/clip.mp4 --set sediment.decay=30 --set sediment.color=warm --set deepfry.strength=60
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

datamosh and timescratch use their own multi-pass pipelines, so they cannot be chained with other effects. Temporal sediment uses a bounded 128 MiB history buffer, resamples input to the probed frame rate, and does not preserve variable-rate timestamps. Its preview is not available yet.

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

## files and tests

`input/` holds source clips. `output/` holds renders. Git ignores files in both folders except `.gitkeep`, so clips and renders stay local. Both paths can be changed with `--input-dir` / `--output-dir` or `EFFEX_INPUT_DIR` / `EFFEX_OUTPUT_DIR`.

run the test suite with:

```sh
python3 tests/run_tests.py
python3 tests/run_tests.py psychedelic  # pixel behavior, options and real renders
```

tests use generated clips and temporary folders; they do not overwrite files in `input/` or `output/`.
