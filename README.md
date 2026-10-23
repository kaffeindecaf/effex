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

## files and tests

`input/` holds source clips. `output/` holds renders. Git ignores files in both folders except `.gitkeep`, so clips and renders stay local. Both paths can be changed with `--input-dir` / `--output-dir` or `EFFEX_INPUT_DIR` / `EFFEX_OUTPUT_DIR`.

run the test suite with:

```sh
python3 tests/run_tests.py
```

tests use generated clips and temporary folders; they do not overwrite files in `input/` or `output/`.
