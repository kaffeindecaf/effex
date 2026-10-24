#!/usr/bin/env python3
"""Menu keypress tests: the interactive flow, driven through a real pty.

These are the tests that catch what a unit test cannot - arrow keys, the
options menu, the nested choice list, typing a custom size, backing out.

Run: python3 tests/menu_smoke.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (Checker, EFFEX, audio_codec, env_for,  # noqa: E402
                     make_clip, require_tools, temp_folders, video_dims)
from pty_driver import PtyDriver  # noqa: E402

DOWN = b"\x1bOB"
RIGHT = b"\x1bOC"
ENTER = b"\r"


def start(env):
    return PtyDriver([sys.executable, str(EFFEX)], rows=44, cols=110,
                     env_extra=env, timeout=8)


def last_frame(d) -> str:
    """only the newest screen - the buffer keeps every frame of the session"""
    return d.text().split("\033[H")[-1]


def wait_last(d, marker: str, timeout: float = 6.0) -> bool:
    """wait for a marker in the NEWEST frame.

    a session visits the same screen twice (pick a video twice), and the pty
    buffer keeps every frame, so wait_for() on the whole buffer can match an
    earlier visit and let the test race ahead of the app.
    """
    end = time.time() + timeout
    while time.time() < end:
        if marker in last_frame(d):
            return True
        d.pump(0.2)
    return False


def press_and_wait(d, key: bytes, timeout: float = 3.0) -> bool:
    before = last_frame(d)
    d.send(key)
    end = time.time() + timeout
    while time.time() < end:
        if last_frame(d) != before:
            return True
        d.pump(0.05)
    return False


def next_effect_group(d, count: int = 1) -> None:
    for _ in range(count):
        press_and_wait(d, b"]")
    d.pump(0.5)


def scenario_render_flow(c: Checker):
    """main menu -> video -> effect -> options -> confirm -> render -> quit"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "aaa.mp4")
    make_clip(in_dir / "bbb.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    c.check("main menu appears", d.wait_for("main menu"))
    c.check("home screen sketches the video workflow",
            all(label in last_frame(d)
                for label in ("SOURCE", "EFFECTS", "OUTPUT")))

    d.send(b"p")                                    # key jump: process
    c.check("video picker lists the input folder", d.wait_for("pick a video"))

    d.send(b"2")                                    # key jump: second video
    c.check("effect picker names the chosen video",
            d.wait_for("pick an effect") and "bbb" in d.text())

    d.send(ENTER)
    c.check("options menu opens for the effect", d.wait_for("stretch options"))
    c.check("preview starts at the default shape", "1080x1920" in d.text())

    d.send(RIGHT)
    c.check("right arrow cycles the target shape", "1920x1080" in d.text())

    d.send(DOWN)
    d.send(RIGHT)
    c.check("right arrow cycles the resample filter",
            "flags=bilinear" in d.text(), "filter line not updated")

    d.send(b"r")                                    # accelerator: render
    c.check("confirm screen shows the plan",
            d.wait_for("render this video?") and "1920x1080" in d.text())

    d.send(ENTER)                                   # default yes
    c.check("render monitor shows the ASCII pipeline and telemetry",
            d.wait_for("[SOURCE] -> [EFFECTS] -> [ENCODE] -> [OUTPUT]")
            and "elapsed" in d.text() and "frames" in d.text())
    c.check("render finishes", d.wait_for("wrote", timeout=90))

    d.send(ENTER)                                   # enter to continue
    c.check("back at the main menu", d.wait_for("main menu"))

    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"quit exits cleanly (rc={rc})", rc == 0)
    c.check("no traceback in the menu run", "Traceback" not in d.text())

    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("menu render reached disk", out is not None)
    if out:
        c.check("menu options reached ffmpeg (1920x1080)",
                video_dims(out) == (1920, 1080), str(video_dims(out)))
    d.close()


def scenario_options_menu(c: Checker):
    """nested choice list, typing a custom size, rejecting nonsense"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    d.send(ENTER)
    c.check("options menu opens", d.wait_for("stretch options"))

    d.send(ENTER)                                   # enter opens the shape list
    c.check("enter opens the choice list",
            d.wait_for("portrait, phone"), "list hints missing")
    d.send(b"4")                                    # 1:1
    c.check("picking from the list applies it", "1080x1080" in d.text())

    d.send(ENTER)
    d.send(b"6")                                    # custom WxH
    c.check("custom shape asks for a size", d.wait_for("size (WxH):"))
    d.send(b"720x1280\r")
    c.check("typed size is applied", "720x1280" in d.text())

    d.send(ENTER)
    d.send(b"6")
    d.wait_for("size (WxH):")
    d.send(b"banana\r")
    c.check("nonsense size is rejected", "is not a WxH size" in d.text())
    c.check("old shape survives bad input", "720x1280" in d.text())

    d.send(b"r")
    d.send(ENTER)
    c.check("render finishes", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    c.check("no traceback", "Traceback" not in d.text())

    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("custom size reached ffmpeg (720x1280)",
            out is not None and video_dims(out) == (720, 1280),
            str(video_dims(out) if out else None))
    d.close()


def scenario_backout(c: Checker):
    """rescan, back out of every screen, render nothing"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")

    d.send(b"?")
    c.check("help screen opens", d.wait_for("rescan input/"))
    d.send(ENTER)

    d.send(b"e")
    c.check("effects screen lists option defaults",
            d.wait_for("squash the frame") and "default" in d.text())
    d.send(ENTER)

    d.send(b"p")
    d.wait_for("pick a video")
    d.send(b"r")
    c.check("r rescans the video list", "pick a video" in d.text())
    d.send(ENTER)
    d.wait_for("pick an effect")
    d.send(ENTER)
    d.wait_for("stretch options")
    d.send(b"b")
    c.check("b backs out of the options menu", d.wait_for("pick an effect"))
    d.send(b"q")
    c.check("q backs out to the video list", d.wait_for("pick a video"))
    d.send(b"q")
    c.check("q backs out to the main menu", d.wait_for("main menu"))
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"quit exits cleanly (rc={rc})", rc == 0)
    c.check("nothing was rendered", not list(out_dir.glob("*.mp4")))
    c.check("no traceback", "Traceback" not in d.text())
    d.close()


def scenario_pixelize(c: Checker):
    """second effect: number option, arrow stepping, typed value, render"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")

    d.send(b"2")                                   # jump: pixelize
    c.check("pixelize options open", d.wait_for("pixelize options"))
    c.check("block size is shown with its range", "2-128" in d.text())
    c.check("pixelize preview keeps the source shape",
            "640x360" in d.text() and "neighbor" in d.text())

    d.send(RIGHT)
    c.check("right arrow steps the block size", "iw/26" in d.text(),
            "filter line did not follow the step")

    d.send(ENTER)
    c.check("enter asks for a number", d.wait_for("block size (2-128):"))
    d.send(b"40\r")
    c.check("typed number is applied", "iw/40" in d.text())

    d.send(ENTER)
    d.wait_for("block size (2-128):")
    d.send(b"999\r")
    c.check("out of range number is rejected", "outside 2-128" in d.text())

    d.send(b"r")
    c.check("confirm names the pixelize effect",
            d.wait_for("render this video?") and "PIXELIZED" in d.text())
    d.send(ENTER)
    c.check("pixelize render finishes", d.wait_for("wrote", timeout=90))

    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    c.check("no traceback", "Traceback" not in d.text())

    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("pixelize reached disk", out is not None)
    if out:
        c.check("pixelize kept the source frame size",
                video_dims(out) == (640, 360), str(video_dims(out)))
        c.check("pixelize kept the audio", audio_codec(out) == "aac")
    d.close()


def scenario_wobble(c: Checker):
    """a later effect with a number + choice param, driven through the menu"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")

    d.send(b"4")                                   # jump: wobble
    c.check("wobble options open", d.wait_for("wobble options"))
    c.check("wobble preview shows the rotate expression",
            "sin(2*PI*t/2.5)" in d.text())

    d.send(RIGHT)
    c.check("right arrow steps the rock amount", "5*(PI/180)" in d.text())

    d.send(DOWN)                                   # speed row
    d.send(RIGHT)
    c.check("right arrow steps the speed", "t/1.2" in d.text())

    d.send(b"r")
    c.check("confirm names the wobble effect",
            d.wait_for("render this video?") and "WOBBLE" in d.text())
    d.send(ENTER)
    c.check("wobble render finishes", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)

    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("wobble reached disk", out is not None)
    if out:
        c.check("wobble kept the frame size",
                video_dims(out) == (640, 360), str(video_dims(out)))
    d.close()


def scenario_preview(c: Checker):
    """p renders a 3 second taste first, then the real thing still works"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4", seconds=5)
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    d.send(ENTER)
    d.wait_for("stretch options")

    d.send(b"p")                                   # preview key
    c.check("preview is reported on the options screen",
            d.wait_for("preview wrote", timeout=90))
    c.check("preview names the preview file", "PREVIEW" in d.text())

    d.send(b"r")                                   # and the real render
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("full render still finishes after a preview",
            d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)

    from harness import ffprobe_streams
    previews = [p for p in out_dir.glob("*PREVIEW*.mp4")]
    c.check("the preview landed in output/", len(previews) >= 1,
            str(list(out_dir.glob("*.mp4"))))
    if previews:
        fmt = ffprobe_streams(previews[0]).get("format", {})
        duration = float(fmt.get("duration", 0) or 0)
        c.check("the preview is ~3 seconds, not the whole clip",
                2.0 <= duration <= 4.5, f"duration {duration}")
    d.close()


def scenario_decline(c: Checker):
    """n at the confirm must say so, return to the options, and not write"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    d.send(ENTER)
    d.wait_for("stretch options")

    d.send(b"r")
    d.wait_for("render this video?")
    d.send(b"n")
    c.check("declining returns to the options screen, not the top",
            d.wait_for("stretch options"))
    c.check("the decline says nothing was rendered",
            "nothing was rendered" in d.text())
    c.check("no file was written by the declined run",
            not list(out_dir.glob("*.mp4")))

    d.send(b"r")
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("render still works after a decline",
            d.wait_for("wrote", timeout=90))
    c.check("done screen offers to open the output folder",
            "open output folder" in d.text())
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    c.check("exactly one file was written in the end",
            len(list(out_dir.glob("*.mp4"))) == 1,
            str(list(out_dir.glob("*.mp4"))))
    d.close()


def scenario_datamosh(c: Checker):
    """the mpeg4 datamosh runs its two passes from the menu"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d, 3)                       # codec & blocks group
    d.send(ENTER)
    c.check("datamosh options screen opens",
            d.wait_for("datamosh options"), last_frame(d))
    c.check("the cut depth row is shown", "cut depth" in d.text())
    c.check("the hold row is shown", "hold" in d.text())
    c.check("no preview key on a two-pass effect",
            "p preview" not in d.text())
    d.send(b"r")
    c.check("confirm screen opens", d.wait_for("render this video?"))
    d.send(ENTER)
    c.check("the mosh writes a file", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    c.check("exactly one file was written",
            len(list(out_dir.glob("*.mp4"))) == 1,
            str(list(out_dir.glob("*.mp4"))))
    d.close()


def scenario_datamosh_second(c: Checker):
    """pick a second clip in the datamosh options and join both streams"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "aaa.mp4")
    make_clip(in_dir / "bbb.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)                                  # aaa.mp4
    d.wait_for("pick an effect")
    next_effect_group(d, 3)                       # codec & blocks group
    d.send(ENTER)
    c.check("datamosh options open for a two-source render",
            d.wait_for("datamosh options"))
    d.send(ENTER)                                  # second video row
    c.check("secondary clip picker lists the other source",
            d.wait_for("bbb.mp4"))
    d.send(b"2")                                   # none, then bbb.mp4
    c.check("the chosen second clip appears in the options",
            wait_last(d, "bbb.mp4"))
    d.send(b"r")
    c.check("confirm shows the two-source datamosh plan",
            d.wait_for("render this video?") and
            "WITH_bbb" in last_frame(d) and
            "mpeg4" in last_frame(d) and "gop 150" in last_frame(d),
            last_frame(d)[-300:])
    d.send(ENTER)
    c.check("the combined datamosh renders from the menu",
            d.wait_for("wrote", timeout=120))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    outputs = list(out_dir.glob("*.mp4"))
    c.check("the combined output names both sources",
            len(outputs) == 1 and "WITH_bbb" in outputs[0].name,
            str([p.name for p in outputs]))
    d.close()


def scenario_newkinds(c: Checker):
    """multi picker, float stepping and percent typing in the menu"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    # rgbsplit multi-pick: toggle red off, render with blue only
    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d)                           # color & texture
    d.send(ENTER)                                  # rgbsplit
    d.wait_for("rgbsplit options")
    d.send(DOWN)
    d.send(DOWN)                                   # channels row
    d.send(ENTER)
    c.check("multi picker opens", d.wait_for("space toggle"))
    d.send(b" ")                                   # red off
    d.send(ENTER)                                  # done
    d.pump(0.6)
    frame = d.text().split("\x1b[H")[-1]
    c.check("single channel reaches the filter",
            "chromashift=cbh=12" in frame and "crh" not in frame,
            frame[-120:])
    d.send(b"r")
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("single-channel split renders", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()

    # float stepping on spin, percent typing on deepfry
    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    d.send(b"5")                                   # spin
    d.wait_for("spin options")
    d.send(RIGHT)
    c.check("right arrow steps the fractional period",
            "2*PI*t/6.5" in d.text())
    d.send(b"q")
    d.pump(0.5)                                    # options -> main menu
    d.send(b"p")
    d.pump(0.5)                                    # main menu -> video picker
    d.send(ENTER)
    d.pump(0.5)                                    # video picker -> effect picker
    next_effect_group(d)                           # color & texture
    d.send(DOWN)
    d.send(ENTER)                                  # deepfry
    c.check("deepfry options open", d.wait_for("deepfry options"))
    d.send(ENTER)
    c.check("percent prompt names the range",
            d.wait_for("strength (10-100%):"))
    d.send(b"55%\r")
    c.check("typed percent is applied",
            d.wait_for("55%") and "55%" in d.text())
    d.send(b"q")
    d.pump(0.5)
    d.send(b"q")
    d.pump(0.5)
    d.send(b"q")
    d.pump(0.5)
    rc = d.expect_exit(6)
    c.check(f"second session exits cleanly (rc={rc})", rc == 0)
    d.close()


def scenario_vhs(c: Checker):
    """an effect past the digit range, reached with j keys, then rendered"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d)
    d.send(DOWN * 4)                               # vhs
    d.send(ENTER)
    c.check("vhs options open", d.wait_for("vhs options"))
    c.check("vhs rows are shown", "chroma bleed" in d.text()
            and "tracking" in d.text())
    d.send(b"r")
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("vhs render finishes", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()


def scenario_channelswap(c: Checker):
    """a circuit-bend effect, reached past the digit range, then rendered"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d)
    d.send(DOWN * 3)                               # channelswap
    d.send(ENTER)
    c.check("channelswap options open", d.wait_for("channelswap options"))
    c.check("channelswap rows are shown",
            "swap" in d.text() and "bleed" in d.text())
    d.send(b"r")
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("channelswap render finishes", d.wait_for("wrote", timeout=90))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()


def scenario_timescratch(c: Checker):
    """a multi-pass scrub effect, opened past the digit range, rendered"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d, 2)                         # time & memory group
    d.send(DOWN)                                   # timescratch
    d.send(ENTER)
    c.check("timescratch options open", d.wait_for("timescratch options"))
    c.check("timescratch rows are shown", "curve" in d.text()
            and "cuts" in d.text())
    c.check("no preview key on a multi-pass effect",
            "p preview" not in d.text())
    d.send(b"r")
    d.wait_for("render this video?")
    d.send(ENTER)
    c.check("timescratch render finishes", d.wait_for("wrote", timeout=150))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()


def scenario_temporal_sediment(c: Checker):
    """the stateful effect exposes its controls and uses the normal render flow"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    d.wait_for("pick an effect")
    next_effect_group(d, 2)                         # time & memory group
    d.send(DOWN * 2)                               # temporal sediment
    d.send(ENTER)
    c.check("temporal sediment options open",
            d.wait_for("temporal sediment options"))
    c.check("sediment options expose history and excavation controls",
            all(label in d.text() for label in
                ("deposit rate", "decay rate", "excavation",
                 "color memory", "random seed")))
    c.check("stateful effect has no preview key", "p preview" not in d.text())
    before_tune = last_frame(d)
    d.send(RIGHT)                                    # tune intensity in menu
    after_tune = last_frame(d)
    c.check("sediment intensity can be adjusted in the menu",
            before_tune != after_tune and "intensity" in after_tune
            and "70%" in after_tune)
    d.send(b"r")
    c.check("sediment confirm identifies the frame pass",
            d.wait_for("render this video?") and "CPU frame pass" in d.text())
    d.send(ENTER)
    c.check("sediment render finishes from the menu",
            d.wait_for("wrote", timeout=150))
    d.send(ENTER)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("menu render reached disk with source dimensions",
            out is not None and video_dims(out) == (640, 360),
            str(video_dims(out) if out else None))
    c.check("menu sediment render keeps AAC audio",
            out is not None and audio_codec(out) == "aac")
    d.close()


def scenario_chain(c: Checker):
    """chip two effects in the picker, render them as one chain (E3.3)"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "clip.mp4")
    env = env_for(in_dir, out_dir)

    d = start(env)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    d.send(ENTER)
    c.check("the effect picker offers chaining",
            d.wait_for("pick an effect") and "space chain" in d.text())
    c.check("effect picker groups visual and color effects",
            "SHAPE & MOTION" in last_frame(d)
            and "COLOR & TEXTURE" in last_frame(d))

    d.send(b" ")                                  # chip stretch (row 1)
    c.check("chipping marks the effect and shows the chain",
            d.wait_for("1 effect") and "chain" in d.text())

    d.send(DOWN)
    d.send(b" ")                                  # chip pixelize (row 2)
    c.check("the second chip shows the run order",
            "2 effects" in d.text(), "chain count missing")

    d.send(ENTER)
    c.check("the chain options screen names both effects",
            d.wait_for("stretch+pixelize options"))
    c.check("each effect gets its own labelled block",
            "stretch" in last_frame(d) and "pixelize" in last_frame(d))
    c.check("the chain preview shows the merged frame and both chains",
            "2 chained" in d.text()
            and "force_original_aspect_ratio=disable" in d.text()
            and "scale=w=iw/24" in d.text(),
            "per-effect filter lines missing")
    c.check("the chain output name carries both effects",
            "STRETCHED+PIXELIZED" in last_frame(d))

    d.send(DOWN)
    d.send(RIGHT)
    c.check("a value in the second block is editable",
            "flags=bilinear" in last_frame(d), "resample did not change")

    d.send(b"p")                                  # preview the whole chain
    c.check("preview works on a chain",
            d.wait_for("preview wrote", timeout=90))

    d.send(b"r")
    c.check("the confirm screen shows the chain",
            d.wait_for("render this video?") and
            "stretch+pixelize" in last_frame(d))
    d.send(ENTER)
    c.check("chain render finishes",
            d.wait_for("enter main menu", timeout=120), "no done screen")
    c.check("the done screen reports the chain output",
            "wrote" in last_frame(d) and
            "render failed" not in last_frame(d))
    d.send(ENTER)
    c.check("back at the main menu after a chain render",
            wait_last(d, "main menu"))

    # a multi-pass effect refuses to join a chain
    d.send(b"p")
    c.check("second visit reaches the video picker",
            wait_last(d, "pick a video"))
    d.send(ENTER)
    c.check("second visit reaches the effect picker",
            wait_last(d, "space chain"))
    next_effect_group(d, 3)                       # codec & blocks group
    d.send(b" ")
    c.check("a multi-pass effect refuses to be chained",
            wait_last(d, "nothing to chain it with"),
            "no refusal notice for datamosh")
    c.check("the refusal does not chip the row",
            "1 effect" not in last_frame(d))

    d.send(b"q")
    d.pump(0.5)
    d.send(b"q")
    d.pump(0.5)
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    c.check("no traceback", "Traceback" not in d.text())

    from harness import newest_mp4
    out = newest_mp4(out_dir)
    c.check("the menu chain reached disk with both effects in the name",
            out is not None and "STRETCHED+PIXELIZED" in out.name,
            out.name if out else "no file")
    c.check("the menu chain rendered at the stretched size",
            out is not None and video_dims(out) == (1080, 1920),
            str(video_dims(out) if out else None))
    d.close()


def scenario_psychedelic(c: Checker):
    """both new color effects can be tuned, previewed and rendered"""
    from harness import newest_mp4

    for offset, name, label, tuned in (
            (6, "channel lobotomy", "red levels", "16 levels"),
            (7, "neon hue cycle", "hue swing", "130")):
        in_dir, out_dir = temp_folders()
        make_clip(in_dir / "clip.mp4", seconds=1, size="160x90")
        d = start(env_for(in_dir, out_dir))
        try:
            d.wait_for("main menu")
            d.send(b"p")
            d.wait_for("pick a video")
            d.send(ENTER)
            d.wait_for("pick an effect")
            next_effect_group(d)
            d.send(DOWN * offset)
            d.send(ENTER)
            c.check(f"{name} options open", wait_last(d, f"{name} options"))
            c.check(f"{name} exposes its controls", label in last_frame(d))
            press_and_wait(d, RIGHT)
            c.check(f"{name} can be tuned", tuned in last_frame(d))
            d.send(b"p")
            c.check(f"{name} preview succeeds",
                    wait_last(d, "preview wrote", timeout=30))
            d.send(b"r")
            c.check(f"{name} reaches confirmation",
                    wait_last(d, "render this video?"))
            d.send(ENTER)
            c.check(f"{name} render succeeds", wait_last(d, "wrote", timeout=30))
            out = newest_mp4(out_dir)
            c.check(f"{name} preserves dimensions and audio",
                    out is not None and "PREVIEW" not in out.name
                    and video_dims(out) == (160, 90) and audio_codec(out) == "aac")
            d.send(ENTER)
            wait_last(d, "main menu")
            d.send(b"q")
            c.check(f"{name} exits cleanly", d.expect_exit(6) == 0)
        finally:
            d.close()


def scenario_collapse(c: Checker):
    """elastic and offline seam modes share controls and the normal renderer"""
    import importlib.util
    from harness import newest_mp4

    if importlib.util.find_spec("cv2") is None or importlib.util.find_spec("numpy") is None:
        print("  SKIP collapse menu tests need requirements-collapse.txt")
        return
    for mode in ("elastic", "seam"):
        in_dir, out_dir = temp_folders()
        make_clip(in_dir / "clip.mp4", seconds=1, size="160x90")
        d = start(env_for(in_dir, out_dir))
        try:
            d.wait_for("main menu")
            d.send(b"p")
            d.wait_for("pick a video")
            d.send(ENTER)
            d.wait_for("pick an effect")
            d.send(b"8")                           # collapse, last shape effect
            c.check(f"collapse {mode}: options open", wait_last(d, "collapse options"))
            c.check(f"collapse {mode}: controls present", all(
                label in last_frame(d) for label in
                ("horizontal deformation amount", "vertical deformation amount",
                 "protect strong edges", "seam strength",
                 "smooth field changes", "random seed")))
            press_and_wait(d, RIGHT)
            c.check(f"collapse {mode}: intensity is editable", "70%" in last_frame(d))
            if mode == "seam":
                for _ in range(4):
                    press_and_wait(d, DOWN)
                press_and_wait(d, RIGHT)
                c.check("collapse seam: offline mode can be selected", "seam (offline)" in last_frame(d))
            c.check(f"collapse {mode}: no unsupported preview key", "p preview" not in last_frame(d))
            d.send(b"r")
            c.check(f"collapse {mode}: confirm opens", wait_last(d, "render this video?"))
            d.send(ENTER)
            c.check(f"collapse {mode}: render finishes", wait_last(d, "wrote", timeout=30))
            out = newest_mp4(out_dir)
            c.check(f"collapse {mode}: size and AAC preserved",
                    out is not None and video_dims(out) == (160, 90) and audio_codec(out) == "aac")
            d.send(ENTER)
            wait_last(d, "main menu")
            d.send(b"q")
            c.check(f"collapse {mode}: exits cleanly", d.expect_exit(6) == 0)
        finally:
            d.close()


def main() -> int:
    c = Checker("menu smoke")
    if not require_tools(c):
        return 0
    for scenario in (scenario_render_flow, scenario_options_menu,
                     scenario_pixelize, scenario_wobble, scenario_preview,
                     scenario_decline, scenario_datamosh,
                     scenario_datamosh_second, scenario_newkinds,
                     scenario_vhs, scenario_channelswap,
                     scenario_timescratch, scenario_temporal_sediment,
                     scenario_chain, scenario_psychedelic, scenario_collapse,
                     scenario_backout):
        print(f"  -- {scenario.__name__}")
        scenario(c)
    return c.summary()


if __name__ == "__main__":
    sys.exit(main())
