"""Bitstream rot: corrupt the coded payload of MPEG-4 Part 2 VOPs.

Pure functions on bytes. Start codes, VOS/VOL headers and the VOP header
(coding type, clock, quant and fcode fields) are never touched, and no edit
may emulate a 00 00 01 start code, so the stream keeps its frame structure and
the decoder's error concealment turns the damage into macroblock garbage that
drifts through the following P-VOPs until the next keyframe.
"""
import random

from datamosh_stream import clock_bits, clock_end

START = b"\x00\x00\x01"
VOP = b"\x00\x00\x01\xb6"
MODES = ("flip", "zero", "smear")
# vop_coded, rounding, intra_dc_vlc_thr(3), vop_quant(5), fcode_forward(3)
_HEADER_TAIL_BITS = 1 + 1 + 3 + 5 + 3


def start_codes(raw: bytes) -> list:
    """Byte offsets of every 00 00 01 prefix, the way a parser finds them."""
    found, at = [], raw.find(START)
    while at != -1:
        found.append(at)
        at = raw.find(START, at + 1)
    return found


def vop_units(raw: bytes) -> list:
    """(start, end, coding type) for every VOP; 0 = I, 1 = P."""
    starts = start_codes(raw) + [len(raw)]
    units = []
    for start, end in zip(starts, starts[1:]):
        if raw.startswith(VOP, start) and end - start > 4:
            units.append((start, end, raw[start + 4] >> 6))
    return units


def payload_start(vop: bytes, width: int) -> int:
    """First byte offset inside a VOP that holds only macroblock data.

    The header is parsed up to the clock, the remaining fixed fields are
    covered conservatively, and one more byte of margin keeps the edit off
    the header even if a field was shorter than assumed.
    """
    bits = clock_end(vop, width) + _HEADER_TAIL_BITS
    return (bits + 7) // 8 + 1


def pick_frames(kinds: list, rate: int, target: str, burst: int,
                rng: random.Random) -> list:
    """Indices of the VOPs to damage.

    rate is the percent chance an eligible VOP starts a hit; burst is the
    percent chance that hit carries on through the next few eligible VOPs.
    """
    eligible = [i for i, kind in enumerate(kinds)
                if target == "all" or (target == "i") == (kind == 0)]
    chosen, run = [], 0
    for index in eligible:
        if run > 0:
            chosen.append(index)
            run -= 1
        elif rng.random() * 100 < rate:
            chosen.append(index)
            if rng.random() * 100 < burst:
                run = rng.randint(2, 6)
    return chosen


def _emulates(buf: bytearray, lo: int, hi: int, a: int, b: int) -> bool:
    # A start code needs 00 00 01 entirely inside this VOP: its tail cannot
    # form one with the next unit, which always begins with 00 00 01 itself.
    return buf.find(START, max(lo, a - 2), min(hi, b + 2)) != -1


def _edit(buf: bytearray, lo: int, hi: int, mode: str,
          rng: random.Random) -> bool:
    """Apply one in-place, length-preserving edit to buf[lo:hi]."""
    span = hi - lo
    if mode == "flip":
        at = rng.randrange(lo, hi)
        old = buf[at:at + 1]
        for _ in range(rng.randint(1, 3)):
            buf[at] ^= 1 << rng.randrange(8)
        a, b = at, at + 1
    elif mode == "zero":
        size = min(span, rng.randint(2, 24))
        at = rng.randrange(lo, hi - size + 1)
        old = buf[at:at + size]
        buf[at:at + size] = bytes(size)
        a, b = at, at + size
    else:  # smear: overwrite a run with bytes from elsewhere in the VOP
        size = min(span // 2, rng.randint(8, 96))
        if size < 2:
            return False
        src = rng.randrange(lo, hi - size + 1)
        at = rng.randrange(lo, hi - size + 1)
        old = buf[at:at + size]
        buf[at:at + size] = buf[src:src + size]
        a, b = at, at + size
    if _emulates(buf, lo, hi, a, b):
        buf[a:b] = old
        return False
    return True


def rot_stream(raw: bytes, rate: int, mode: str = "mix", target: str = "p",
               burst: int = 0, hits: int = 3, seed: int = 0) -> bytes:
    """Return raw with damaged VOP payloads; same length and start codes."""
    rate = max(0, min(100, int(rate)))
    if rate == 0 or hits <= 0:
        return raw
    width = clock_bits(raw)
    units = vop_units(raw)
    rng = random.Random(seed)
    chosen = pick_frames([kind for _s, _e, kind in units], rate, target,
                         max(0, min(100, int(burst))), rng)
    buf = bytearray(raw)
    for index in chosen:
        start, end, _kind = units[index]
        try:
            lo = start + payload_start(raw[start:end], width)
        except ValueError:
            continue
        if end - lo < 4:
            continue  # not-coded or tiny VOP: nothing to rot
        for _ in range(hits):
            edit = rng.choice(MODES) if mode == "mix" else mode
            for _attempt in range(4):
                if _edit(buf, lo, end, edit, rng):
                    break
    return bytes(buf)
