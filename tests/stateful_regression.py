#!/usr/bin/env python3
"""Temporal Sediment processor and FFmpeg integration tests."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import (Checker, EFFEX, audio_codec, env_for, make_clip,  # noqa: E402
                     run, temp_folders, video_dims)

try:
    import numpy as np
except ImportError:
    print("  SKIP NumPy not installed; install requirements-stateful.txt")
    sys.exit(0)

try:
    from stateful_effects import TemporalSediment  # noqa: E402
except ImportError as exc:
    print(f"  FAIL Temporal Sediment processor is missing: {exc}")
    sys.exit(1)


DEFAULTS = {
    "intensity": 100,
    "deposit": 100,
    "decay": 25,
    "layers": "3",
    "motion": 50,
    "excavation": 0,
    "excavation_size": 20,
    "color": "keep",
    "seed": 17,
}


def make_processor(config=None, shape=(32, 32, 3), seed=17, fps=10.0):
    processor = TemporalSediment()
    processor.initialize(dict(config or DEFAULTS), shape, fps, seed)
    return processor


def sequence(processor, count=8):
    first = np.zeros((32, 32, 3), dtype=np.uint8)
    first[10:16, 8:14] = 255
    output = [processor.process(first, 0, 0.0, {})]
    blank = np.zeros_like(first)
    for index in range(1, count + 1):
        output.append(processor.process(blank, index, index / 10.0, {}))
    return output


def main() -> int:
    c = Checker("temporal sediment")

    frame = np.zeros((32, 32, 3), dtype=np.uint8)
    frame[8:16, 9:17] = (255, 80, 20)
    original = frame.copy()
    processor = make_processor()
    first = processor.process(frame, 0, 0.0, {})
    c.check("first frame keeps shape and uint8 format",
            first.shape == frame.shape and first.dtype == np.uint8)
    c.check("first frame passes through unchanged", np.array_equal(first, frame))
    c.check("input frame is not mutated", np.array_equal(frame, original))

    traces = sequence(make_processor())
    c.check("old pixels remain after the subject moves away",
            int(traces[-1][10:16, 8:14].max()) > 0)

    slow = sequence(make_processor({**DEFAULTS, "decay": 10}))[-1]
    fast = sequence(make_processor({**DEFAULTS, "decay": 90}))[-1]
    c.check("decay setting changes how much history remains",
            int(slow[10:16, 8:14].mean()) >
            int(fast[10:16, 8:14].mean()))

    blank = np.zeros_like(frame)
    moving = np.zeros_like(frame)
    moving[12:18, 16:22] = 200
    passthrough = make_processor({**DEFAULTS, "intensity": 0})
    outputs = [passthrough.process(item, index, index / 10.0, {})
               for index, item in enumerate((frame, moving))]
    c.check("zero intensity returns each input frame exactly",
            all(np.array_equal(out, item)
                for out, item in zip(outputs, (frame, moving))))

    reset = make_processor()
    reset.process(frame, 0, 0.0, {})
    reset.process(blank, 1, 0.1, {})
    reset.reset()
    cleared = reset.process(blank, 0, 0.0, {})
    c.check("reset clears accumulated history", np.array_equal(cleared, blank))
    c.check("serialized config contains settings, not frame buffers",
            reset.serialize_config() == DEFAULTS)

    config = {**DEFAULTS, "excavation": 100}
    a, b = make_processor(config, seed=123), make_processor(config, seed=123)
    moving_frames = []
    for x in (2, 8, 14, 20):
        item = np.zeros_like(frame)
        item[10:18, x:x + 6] = 220
        moving_frames.append(item)
    out_a = [a.process(item, i, i / 10.0, {})
             for i, item in enumerate(moving_frames)]
    out_b = [b.process(item, i, i / 10.0, {})
             for i, item in enumerate(moving_frames)]
    c.check("excavation is deterministic for a fixed seed",
            all(np.array_equal(x, y) for x, y in zip(out_a, out_b)))
    no_excavation = make_processor({**DEFAULTS, "excavation": 0}, seed=123)
    out_plain = [no_excavation.process(item, i, i / 10.0, {})
                 for i, item in enumerate(moving_frames)]
    c.check("excavation reveals a different stored layer",
            any(not np.array_equal(x, y) for x, y in zip(out_a, out_plain)))
    warm_excavation = make_processor({**DEFAULTS, "color": "warm",
                                      "excavation": 100,
                                      "excavation_size": 100})
    white = np.full_like(frame, 255)
    gray = np.full_like(frame, 153)
    warm_excavation.process(white, 0, 0.0, {})
    excavated = warm_excavation.process(gray, 1, 0.1, {})
    c.check("color memory tint also applies to excavated layers",
            int(excavated[:, :, 0].mean()) > int(excavated[:, :, 2].mean()))

    cut = make_processor()
    cut.process(frame, 0, 0.0, {})
    new_scene = np.full_like(frame, 255)
    cut_output = cut.process(new_scene, 1, 0.1, {})
    c.check("scene cuts reset temporal history",
            np.array_equal(cut_output, new_scene))
    color_cut = make_processor()
    red_scene = np.zeros_like(frame)
    red_scene[:, :] = (255, 0, 0)
    blue_scene = np.zeros_like(frame)
    blue_scene[:, :] = (0, 0, 255)
    color_cut.process(red_scene, 0, 0.0, {})
    blue_output = color_cut.process(blue_scene, 1, 0.1, {})
    c.check("scene cuts compare color as well as luminance",
            np.array_equal(blue_output, blue_scene))
    rewind = make_processor()
    rewind.process(frame, 0, 1.0, {})
    rewound_output = rewind.process(blank, 1, 0.5, {})
    c.check("backward timestamps reset temporal history",
            np.array_equal(rewound_output, blank))
    slow_t = make_processor({**DEFAULTS, "decay": 90})
    fast_t = make_processor({**DEFAULTS, "decay": 90})
    bright = np.zeros_like(frame)
    bright[12:16, 12:16] = 220
    slow_t.process(bright, 0, 0.0, {})
    fast_t.process(bright, 0, 0.0, {})
    after_long_gap = slow_t.process(blank, 1, 1.0, {})
    after_short_gap = fast_t.process(blank, 1, 0.1, {})
    c.check("decay follows elapsed timestamps",
            int(after_long_gap[12:16, 12:16].mean()) <
            int(after_short_gap[12:16, 12:16].mean()))
    memory = make_processor()
    for i in range(12):
        memory.process(frame if i == 0 else blank, i, i / 10.0, {})
    bounded_bytes = memory.state_nbytes
    for i in range(12, 40):
        memory.process(blank, i, i / 10.0, {})
    c.check("history memory stays constant during a long clip",
            memory.state_nbytes == bounded_bytes)

    old_budget = TemporalSediment.MAX_STATE_BYTES
    try:
        TemporalSediment.MAX_STATE_BYTES = 1024 * 1024
        bounded = make_processor(shape=(4096, 4096, 3))
        c.check("history buffers obey the memory budget",
                bounded.state_nbytes <= TemporalSediment.MAX_STATE_BYTES)
        c.check("large sources reduce history resolution",
                bounded.work_shape[0] < 4096 and bounded.work_shape[1] < 4096)
    finally:
        TemporalSediment.MAX_STATE_BYTES = old_budget

    in_dir, out_dir = temp_folders(prefix="effex-sediment-")
    clip = in_dir / "clip.mp4"
    c.check("render fixture builds",
            make_clip(clip, seconds=2, size="160x90", audio=True))
    env = env_for(in_dir, out_dir)
    dry_run = run([EFFEX, "sediment", clip, "--set", "seed=17",
                   "--dry-run"], env_extra=env)
    c.check("dry-run describes the stateful pass and its settings",
            dry_run.returncode == 0 and
            "decode the source through any earlier FFmpeg filters" in dry_run.stdout.decode() and
            "sediment.seed=17" in dry_run.stdout.decode())
    invalid = run([EFFEX, "sediment", clip, "--set", "decay=101"],
                  env_extra=env)
    c.check("sediment parameter bounds are enforced", invalid.returncode == 2)

    proc = run([EFFEX, "sediment", clip, "--set", "intensity=70",
                "--set", "deposit=80", "--set", "decay=30",
                "--set", "layers=2", "--set", "motion=65",
                "--set", "excavation=25", "--set", "excavation_size=20",
                "--set", "color=warm", "--set", "seed=17", "--json"],
               env_extra=env)
    c.check("Temporal Sediment renders", proc.returncode == 0,
            proc.stderr.decode()[:300])
    if proc.returncode == 0:
        data = json.loads(proc.stdout.decode())
        output = Path(data["output"])
        c.check("all sediment controls reach the render config",
                data["options"]["deposit"] == 80 and
                data["options"]["layers"] == "2" and
                data["options"]["motion"] == 65 and
                data["options"]["excavation"] == 25 and
                data["options"]["excavation_size"] == 20 and
                data["options"]["color"] == "warm" and
                data["options"]["seed"] == 17)
        c.check("render keeps dimensions and AAC audio",
                video_dims(output) == (160, 90) and
                audio_codec(output) == "aac")

    silent_clip = in_dir / "silent.mp4"
    c.check("silent render fixture builds",
            make_clip(silent_clip, seconds=1, size="160x90", audio=False))
    silent = run([EFFEX, "sediment", silent_clip, "--set", "seed=17", "--json"],
                 env_extra=env)
    c.check("Temporal Sediment handles sources without audio",
            silent.returncode == 0, silent.stderr.decode()[:300])
    if silent.returncode == 0:
        data = json.loads(silent.stdout.decode())
        c.check("silent render does not invent an audio stream",
                audio_codec(data["output"]) is None)

    chained = run([EFFEX, "sediment+deepfry", clip,
                   "--set", "sediment.seed=17",
                   "--set", "deepfry.strength=60", "--json"],
                  env_extra=env)
    c.check("stateful effect chains with a single-pass effect",
            chained.returncode == 0, chained.stderr.decode()[:300])
    if chained.returncode == 0:
        data = json.loads(chained.stdout.decode())
        c.check("chained render keeps dimensions and audio",
                video_dims(data["output"]) == (160, 90) and
                audio_codec(data["output"]) == "aac")

    prefixed = run([EFFEX, "stretch+sediment", clip, "--size", "320x180",
                    "--set", "sediment.seed=17", "--json"],
                   env_extra=env)
    c.check("stateful effect accepts FFmpeg filters before it",
            prefixed.returncode == 0, prefixed.stderr.decode()[:300])
    if prefixed.returncode == 0:
        data = json.loads(prefixed.stdout.decode())
        c.check("prefix sizing and audio survive the stateful pass",
                video_dims(data["output"]) == (320, 180) and
                audio_codec(data["output"]) == "aac")

    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
