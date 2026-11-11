#!/usr/bin/env python3
"""Layout tests: every frame must fit the terminal and stay aligned.

The menu draws whole frames, so these tests can parse the frames straight out
of the pty stream and measure them:

  - nothing may be wider than the terminal (a wrapped line is what leaves
    artifacts behind on the next redraw)
  - the menu runs on the alternate screen, cleared before the first frame
  - every frame line clears to its end, so a longer line of the previous
    screen never shows through
  - list rows must all be the same width so columns line up
  - a frame must not move the cursor itself (no cursor-up tricks inside it)

Run: python3 tests/layout.py
"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (Checker, EFFEX, env_for, make_clip,  # noqa: E402
                     require_tools, temp_folders)
from pty_driver import PtyDriver  # noqa: E402

# colours, and erase-to-end-of-line: neither takes a cell on screen
SGR = re.compile(r"\033\[[0-9;]*[mK]")
ENTER = b"\r"

# sizes to check: narrow/wide scaling, the classic 80x24, and something wide
SIZES = [(36, 20), (60, 20), (80, 24), (120, 40), (160, 40)]


def vis(line: str) -> int:
    """visible width; the pty rewrites newlines to crlf, so drop the cr"""
    return len(plain_of(line))


def plain_of(line: str) -> str:
    return SGR.sub("", line).rstrip("\r")


def frames(text: str) -> list:
    """split the pty stream into frames on the home-cursor sequence"""
    return [part.split("\033[J")[0].rstrip("\n")
            for part in text.split("\033[H")[1:]]


def frame_with(text: str, marker: str) -> str:
    for frame in reversed(frames(text)):
        if marker in plain_of(frame):
            return frame
    return ""


def body_of(frame: str) -> list:
    """the rows between the two rules, without blanks or the more-marker"""
    plain = [plain_of(line) for line in frame.split("\n")]
    rules = [i for i, line in enumerate(plain)
             if line.strip() and set(line.strip()) <= {"\u2500"}]
    if len(rules) < 2:
        return []
    middle = plain[rules[0] + 1:rules[-1]]
    return [line for line in middle
            if line.strip() and not line.strip().startswith("\u2026")]


def lines(frame: str) -> list:
    return frame.split("\n")


def scenario_fit(c: Checker, cols: int, rows: int) -> PtyDriver:
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "a_long_video_name_for_testing_columns.mp4", seconds=1)
    make_clip(in_dir / "b.mp4", seconds=1)
    # Stale shell dimensions must not override a resized TTY.
    env = {**env_for(in_dir, out_dir), "COLUMNS": "84", "LINES": "24"}

    d = PtyDriver([sys.executable, str(EFFEX)], rows=rows, cols=cols,
                  env_extra=env, timeout=10)
    c.check(f"{cols}x{rows}: menu comes up", d.wait_for("main menu"))
    return d


def check_frame(c: Checker, label: str, frame: str, cols: int) -> None:
    body = lines(frame)
    widest = max((vis(line) for line in body), default=0)
    c.check(f"{label}: every line fits {cols} columns", widest <= cols,
            f"widest line is {widest}")
    c.check(f"{label}: no tabs in the frame", "\t" not in frame)
    c.check(f"{label}: frame does not move the cursor itself",
            not any(code in frame for code in ("\033[1A", "\033[2J",
                                               "\033[A")))
    c.check(f"{label}: every line clears to its end",
            all(line.rstrip("\r").endswith("\033[K") for line in body),
            repr([line[-12:] for line in body][:3]))
    c.check(f"{label}: has a header and a footer rule",
            plain_of(frame).count("\u2500" * 10) >= 2)


def check_rows_aligned(c: Checker, label: str, frame: str,
                       expected_width: int) -> None:
    """list rows are padded to one width, so their columns line up"""
    rows = body_of(frame)
    widths = {vis(line) for line in rows}
    c.check(f"{label}: rows share one width", len(widths) == 1,
            f"{len(rows)} rows, widths seen: {sorted(widths)}")
    c.check(f"{label}: rows fill the available width", widths == {expected_width},
            f"expected {expected_width}, got {sorted(widths)}")


def scenario_sizes(c: Checker) -> None:
    for cols, rows in SIZES:
        d = scenario_fit(c, cols, rows)
        stream = d.text()
        c.check(f"{cols}x{rows}: menu takes the alternate screen, cursor "
                "hidden, cleared before the first frame",
                stream.startswith("\033[?1049h\033[?25l\033[2J\033[H"),
                repr(stream[:24]))

        menu = frame_with(stream, "main menu")
        check_frame(c, f"{cols}x{rows} main menu", menu, cols)
        check_rows_aligned(c, f"{cols}x{rows} main menu", menu, cols - 2)

        d.send(b"p")
        d.wait_for("pick a video")
        picker = frame_with(d.text(), "pick a video")
        check_frame(c, f"{cols}x{rows} video list", picker, cols)
        check_rows_aligned(c, f"{cols}x{rows} video list", picker, cols - 2)
        c.check(f"{cols}x{rows}: long file names are cut, not wrapped",
                "a_long_video_name_for_testing_columns.mp4" in
                SGR.sub("", picker)
                or "\u2026" in SGR.sub("", picker))

        d.send(ENTER)
        d.wait_for("pick an effect")
        effects = frame_with(d.text(), "pick an effect")
        check_frame(c, f"{cols}x{rows} effect list", effects, cols)

        d.send(ENTER)
        d.wait_for("options")
        options = frame_with(d.text(), "options")
        check_frame(c, f"{cols}x{rows} options", options, cols)

        d.send(b"q")
        d.send(b"q")
        d.send(b"q")
        d.send(b"q")
        rc = d.expect_exit(6)
        c.check(f"{cols}x{rows}: clean exit (rc={rc})", rc == 0)
        c.check(f"{cols}x{rows}: no traceback", "Traceback" not in d.text())
        d.close()


def scenario_columns(c: Checker) -> None:
    """the meta columns of the video list start at the same x on every row"""
    in_dir, out_dir = temp_folders()
    make_clip(in_dir / "aa.mp4", seconds=1)
    make_clip(in_dir / "bbbbbbbbbbbbbb.mp4", seconds=1)
    env = env_for(in_dir, out_dir)

    d = PtyDriver([sys.executable, str(EFFEX)], rows=30, cols=110,
                  env_extra=env, timeout=10)
    d.wait_for("main menu")
    d.send(b"p")
    d.wait_for("pick a video")
    picker = frame_with(d.text(), "pick a video")

    rows = body_of(picker)
    res_starts = [m.start() for line in rows
                  for m in [re.search(r"\d{2,5}x\d{2,5}", line)] if m]
    time_starts = [m.start() for line in rows
                   for m in [re.search(r"\d{2}:\d{2}", line)] if m]
    c.check("video rows found for the column check", len(res_starts) == 2,
            str(res_starts))
    c.check("resolution column starts at the same x on every row",
            len(res_starts) == 2 and len(set(res_starts)) == 1,
            str(res_starts))
    c.check("duration column starts at the same x on every row",
            len(time_starts) == 2 and len(set(time_starts)) == 1,
            str(time_starts))
    d.send(b"q")
    d.send(b"q")
    rc = d.expect_exit(6)
    c.check(f"clean exit (rc={rc})", rc == 0)
    d.close()


# ---------------------------------------------------------------------------
# pure-function checks: key decoding, frame clipping, highlight, widths
# ---------------------------------------------------------------------------

def load_effex():
    import importlib
    sys.path.insert(0, str(EFFEX.parent))
    return importlib.import_module("effex")


def keys_from(fx, data: bytes) -> list:
    """every key the decoder reads out of a byte stream, swallowed ones
    (empty strings) dropped the way _read_key drops them"""
    buf = list(data)

    def read(timeout):
        if not buf:
            if timeout is None:
                raise EOFError
            return b""
        return bytes([buf.pop(0)])

    out = []
    try:
        while True:
            key = fx.decode_key(read)
            if key:
                out.append(key)
    except EOFError:
        pass
    return out


def sized(fx, cols: int, rows: int):
    """pin the terminal size the frame builders see"""
    fx.terminal_size = lambda: __import__("os").terminal_size((cols, rows))


def unit_key_decoding(c: Checker, fx) -> None:
    cases = [
        (b"\x1b[5~", ["pgup"]), (b"\x1b[6~", ["pgdn"]),
        (b"\x1b[H", ["home"]), (b"\x1b[F", ["end"]),
        (b"\x1b[1~", ["home"]), (b"\x1b[4~", ["end"]),
        (b"\x1b[7~", ["home"]), (b"\x1b[8~", ["end"]),
        (b"\x1bOH", ["home"]), (b"\x1bOF", ["end"]),
        (b"\x1b[3~", ["delete"]),
        (b"\x1b[A", ["up"]), (b"\x1bOB", ["down"]),
        (b"\x1b[1;5A", ["up"]), (b"\x1b[1;2C", ["right"]),
        (b"\x1b[5;5~", ["pgup"]),
        (b"\x1bOP", []), (b"\x1b[15~", []), (b"\x1b[24~", []),
        (b"\x1b[15~5", ["5"]),            # the 5 after F5 is a real key
        (b"\x1b[5~~", ["pgup", "~"]),
        (b"\x1b[<0;12;7M", []),           # sgr mouse report
        (b"\x1b[M !!x", ["x"]),           # x10 mouse report: 3 raw bytes
        (b"\x1b[200~", []),               # bracketed paste marker
        (b"\x1b", ["esc"]), (b"\x1b\x1b", ["esc"]),
        (b"\x1bx", []),                   # alt+x: not a binding
        (b"q\r ", ["q", "\r", " "]),
        ("é".encode(), ["é"]), ("日本".encode(), ["日", "本"]),
        ("🎬".encode() + b"j", ["🎬", "j"]),
    ]
    for data, want in cases:
        got = keys_from(fx, data)
        c.check(f"key decode {data!r} -> {want}", got == want, str(got))
    c.check("no long sequence decodes as esc (go back)",
            all("esc" not in keys_from(fx, d) for d in
                (b"\x1b[5~", b"\x1b[15~", b"\x1bOP", b"\x1b[1;5D")))
    c.check("windows key names unchanged",
            fx.decode_windows_key("\xe0", "I") == "pgup"
            and fx.decode_windows_key("\x00", "H") == "up"
            and fx.decode_windows_key("\x1b") == "esc"
            and fx.decode_windows_key("q") == "q")


def unit_frames(c: Checker, fx) -> None:
    fx.apply_palette(True)
    body = [f"  line {n}" for n in range(60)]
    for cols, rows in ((80, 24), (40, 10), (30, 6), (20, 3)):
        sized(fx, cols, rows)
        out = fx.build_frame("pick a video", body, ["q back"],
                             notice="a notice")
        frame = out.split("\n")
        c.check(f"{cols}x{rows}: tall body clipped to rows-1",
                len(frame) <= rows - 1 or (rows < 3 and len(frame) == 1),
                f"{len(frame)} lines")
        c.check(f"{cols}x{rows}: header kept on top",
                "effex" in plain_of(frame[0]))
        c.check(f"{cols}x{rows}: footer kept at the bottom",
                "q back" in plain_of(frame[-1]))
        c.check(f"{cols}x{rows}: no line wider than the terminal",
                max(vis(line) for line in frame) < cols,
                str(max(vis(line) for line in frame)))
    sized(fx, 80, 24)
    out = fx.build_frame("x", body, ["q"]).split("\n")
    c.check("a clipped body says how much is hidden",
            any("more lines" in plain_of(line) for line in out))
    short = fx.build_frame("x", body[:3], ["q"]).split("\n")
    c.check("a body that fits is untouched",
            [plain_of(line) for line in short[3:6]] == body[:3])

    sized(fx, 40, 24)
    crumb = "datamosh › second video start time in seconds"
    head = fx.build_frame(crumb, [], ["q"], note="clip.mp4").split("\n")[0]
    c.check("a long crumb is truncated, not wrapped",
            vis(head) <= 38 and "…" in plain_of(head), repr(plain_of(head)))
    c.check("the note gives way to a long crumb",
            "clip.mp4" not in plain_of(head))
    head = fx.build_frame("main menu", [], ["q"], note="3 in input/")
    c.check("a short crumb keeps the note on the right",
            plain_of(head.split("\n")[0]).rstrip().endswith("3 in input/"))

    sized(fx, 60, 24)
    wide = "  " + "日本語" * 20      # 120 cells of cjk
    clipped = fx.clip_visible(wide, 30)
    c.check("wide characters count two cells", fx.vlen("日本") == 4)
    c.check("clip_visible cuts wide characters by cells",
            fx.vlen(clipped) <= 30, str(fx.vlen(clipped)))
    c.check("trunc cuts wide characters by cells",
            fx.vlen(fx.trunc(wide, 21)) <= 21)
    colored = fx.C_FROST + "abcdefghij" + fx.NC
    c.check("clip_visible keeps the colours and resets after the cut",
            fx.clip_visible(colored, 5).startswith(fx.C_FROST)
            and fx.clip_visible(colored, 5).endswith(fx.NC)
            and fx.vlen(fx.clip_visible(colored, 5)) == 5)
    note = fx.build_frame("x", [], ["q"], notice=fx.C_AMB + "careful" + fx.NC)
    c.check("a notice keeps its colour", fx.C_AMB + "careful" in note)


def reversed_cells(text: str) -> list:
    """replay the sgr codes and say, for every visible cell, whether the
    reverse attribute is on - what the terminal would paint"""
    state = False
    cells = []
    pos = 0
    sgr = re.compile(r"\033\[([0-9;]*)m")
    while pos < len(text):
        m = sgr.match(text, pos)
        if m:
            for param in (m.group(1) or "0").split(";"):
                if param in ("", "0", "27"):
                    state = False
                elif param == "7":
                    state = True
            pos = m.end()
            continue
        cells.append(state)
        pos += 1
    return cells


def unit_row_highlight(c: Checker, fx) -> None:
    fx.apply_palette(True)
    sized(fx, 60, 24)
    text = ("  " + fx.C_GRN + "✓" + fx.NC + " " + fx.fit("label", 14)
            + fx.C_DIM + "a hint" + fx.NC + " tail")
    lit = fx.row(text, True)
    cells = reversed_cells(lit)[2:]           # the two-space gutter is plain
    c.check("highlight spans the whole row after embedded resets",
            cells and all(cells), f"{cells.count(False)} cells unlit")
    c.check("highlighted and plain rows have the same width",
            fx.vlen(lit) == fx.vlen(fx.row(text, False)) == 58)
    c.check("the highlight is switched off after the row",
            not reversed_cells(lit + "x")[-1])
    long_text = fx.C_FROST + "x" * 100 + fx.NC
    c.check("an overlong row keeps its colour when cut",
            fx.C_FROST in fx.row(long_text, False)
            and fx.vlen(fx.row(long_text, False)) == 58)
    fx.apply_palette(False)
    c.check("no colour: a highlighted row is plain text",
            "\033" not in fx.row("  plain", True))


def unit_lists(c: Checker, fx) -> None:
    c.check("empty list: movement keys do not divide by zero",
            all(fx.nav_index(k, 0, 0, 5) == 0
                for k in ("up", "down", "home", "end", "pgup", "pgdn")))
    c.check("nav: home/end/pgup/pgdn",
            (fx.nav_index("home", 7, 20, 5), fx.nav_index("end", 0, 20, 5),
             fx.nav_index("pgdn", 18, 20, 5), fx.nav_index("pgup", 3, 20, 5))
            == (0, 19, 19, 0))
    c.check("nav: other keys are not movement",
            fx.nav_index("q", 0, 3, 5) is None)
    c.check("a list that fits needs no more-line",
            fx.list_page(4, 10) == 4)
    c.check("an overflowing list saves a line for the more-line",
            fx.list_page(40, 10) == 9)
    c.check("after the terminal grows the list scrolls back up",
            fx.scroll_top(30, 25, 30, 40) == 10)

    # select() with an empty list on a tty: draw, move, enter, then back
    import io
    keys = iter(["down", "up", "end", "\r", " ", "1", "p", "q"])
    drawn = []
    real_stdin, real_read, real_draw = sys.stdin, fx.read_one_key, fx.draw

    class Tty(io.StringIO):
        def isatty(self):
            return True

    sys.stdin = Tty()
    fx.read_one_key = lambda: next(keys)
    fx.draw = drawn.append
    try:
        picked = fx.select("pick", [], lambda it, i, sel: "", keys=[],
                           jump={"p": 0})
        crashed = ""
    except Exception as exc:     # noqa: BLE001 - the regression is a crash
        picked, crashed = "crash", repr(exc)
    finally:
        sys.stdin, fx.read_one_key, fx.draw = real_stdin, real_read, real_draw
    c.check("select() on an empty list survives every key and goes back",
            picked is None and not crashed, crashed)
    c.check("an empty list says so",
            any("nothing to pick" in frame for frame in drawn))


def unit_draw(c: Checker, fx) -> None:
    import io

    class Tty(io.StringIO):
        def isatty(self):
            return True

    real = sys.stdout
    out = Tty()
    sys.stdout = out
    try:
        sized(fx, 40, 10)
        fx.SCREEN.drawn = None
        fx.draw("\n".join(f"line {n}" for n in range(30)))
        first = out.getvalue()
        out.seek(0)
        out.truncate()
        fx.draw("a\nb")
        second = out.getvalue()
        out.seek(0)
        out.truncate()
        sized(fx, 50, 12)
        fx.draw("a\nb")
        third = out.getvalue()
    finally:
        sys.stdout = real
    c.check("draw clips a tall frame to rows-1 lines",
            first.count("\n") == 9, str(first.count("\n")))
    c.check("draw keeps the last line of a clipped frame (the footer)",
            "line 29" in first)
    c.check("first frame clears the screen", first.startswith("\033[2J\033[H"))
    c.check("same size: just home the cursor", second.startswith("\033[H"))
    c.check("every drawn line erases to its end",
            second == "\033[Ha\033[K\nb\033[K\n\033[J", repr(second))
    c.check("a new size clears the whole screen first",
            third.startswith("\033[2J\033[H"))
    fx.SCREEN.seen = (50, 12)
    c.check("no resize reported while the size holds", not fx.SCREEN.resized())
    sized(fx, 70, 20)
    c.check("a size change is reported once",
            fx.SCREEN.resized() and not fx.SCREEN.resized())


def scenario_units(c: Checker) -> None:
    fx = load_effex()
    saved_size = fx.terminal_size
    try:
        unit_key_decoding(c, fx)
        unit_frames(c, fx)
        unit_row_highlight(c, fx)
        unit_lists(c, fx)
        unit_draw(c, fx)
    finally:
        fx.terminal_size = saved_size
        fx.apply_palette(False)


def main() -> int:
    c = Checker("layout")
    scenario_units(c)
    if not require_tools(c):
        return c.summary()
    scenario_sizes(c)
    scenario_effect_options(c, "datamosh", 3, 0, "audio")
    scenario_effect_options(c, "archaeology", 2, 5, "random seed")
    scenario_columns(c)
    return c.summary()


def scenario_effect_options(c: Checker, effect: str, group: int,
                            within_group: int, last_option: str) -> None:
    from menu_smoke import DOWN, last_frame, next_effect_group, wait_last
    for cols, height in ((36, 20), (60, 20), (80, 24)):
        d = scenario_fit(c, cols, height)
        d.send(b"p")
        d.wait_for("pick a video")
        d.send(ENTER)
        d.wait_for("pick an effect")
        next_effect_group(d, group)
        if within_group:
            d.send(DOWN * within_group)
        d.send(ENTER)
        d.wait_for(f"{effect} options")
        frame = last_frame(d).split("\033[J")[0].rstrip()
        c.check(f"{cols}x{height}: {effect} options fit vertically",
                len(frame.splitlines()) < height, str(len(frame.splitlines())))
        check_frame(c, f"{cols}x{height} {effect}", frame, cols)
        reached = False
        from menu_smoke import press_and_wait
        for _ in range(40):
            press_and_wait(d, DOWN)
            if last_option in last_frame(d):
                reached = True
                break
        frame = last_frame(d).split("\033[J")[0].rstrip()
        c.check(f"{cols}x{height}: bottom {last_option} option stays reachable",
                reached and len(frame.splitlines()) < height)
        for _ in range(4):
            d.send(b"q")
        c.check(f"{cols}x{height}: {effect} clean exit", d.expect_exit(6) == 0)
        d.close()


if __name__ == "__main__":
    sys.exit(main())
