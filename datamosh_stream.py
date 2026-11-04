"""MPEG-4 Part 2 prediction reuse with the destination VOP's clock intact.

Bloom/pulse techniques: github.com/GlitchTools/tomato and
 github.com/tiberiuiancu/datamoshing. Independent implementation; no vendored code.
Only the rectangular, non-partitioned streams produced by Effex are supported.
"""
import re
import math
import random


def self_mosh_segments(duration, fps, cut_length, count, reverse, seed, shuffle=False):
    """Return (start frame, frame count, reversed) cuts and a bounded plan.

    Automatic mode covers the timeline, including a shorter final cut. A fixed
    count samples nonoverlapping cuts evenly from the entire source.
    """
    # Container durations are often rounded to milliseconds (not frame time).
    # Ceil would invent a tail frame for e.g. a 30000/1001 clip reported as 2.003s.
    total = max(1, int(round(duration * fps)))
    length = max(1, int(round(cut_length * fps)))
    if count:
        if count * length > total:
            raise ValueError("segments × cut_length exceeds the source duration; "
                             "choose fewer segments or shorter cuts")
        starts = ([0] if count == 1 else
                  [round(i * (total - length) / (count - 1)) for i in range(count)])
    else:
        if math.ceil(total / length) > 128:
            raise ValueError("self-mosh supports up to 128 segments; increase cut_length "
                             "or choose a segment count")
        starts = range(0, total, length)
    rng = random.Random(seed)
    segments = [(start, min(length, total - start), rng.random() * 100 < reverse)
                for start in starts]
    if shuffle and len(segments) > 1:
        original = segments[:]
        rng.shuffle(segments)
        if segments == original:
            segments = segments[1:] + segments[:1]
    return segments


def transition_ranges(segments, fps, seconds):
    """Frame windows following each join; clean frames recover outside them."""
    length = max(0, int(round(seconds * fps)))
    cursor = 0
    windows = []
    for index, (_, frames, _) in enumerate(segments):
        if index and length:
            windows.append((cursor, cursor + min(length, frames)))
        cursor += frames
    return windows


def pulse_ranges(duration, fps, interval, length, start):
    """Return frame-aligned bursts, each separated by clean recovery frames."""
    total = max(1, int(round(duration * fps)))
    if length == 0:
        return []
    spacing = max(1, int(round(interval * fps)))
    frames = max(1, int(round(length * fps)))
    if frames >= spacing:
        raise ValueError("pulse_length must leave at least one clean frame before "
                         "the next pulse; increase pulse_interval or reduce pulse_length")
    first = max(0, int(round(start * fps)))
    starts = range(first, total, spacing)
    if len(starts) > 128:
        raise ValueError("bloom pulses supports up to 128 bursts; increase pulse_interval "
                         "or pulse_start")
    return [(frame, min(total, frame + frames)) for frame in starts]


class _Bits:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def read(self, count: int) -> int:
        if self.pos + count > len(self.data) * 8:
            raise ValueError("truncated MPEG-4 header")
        value = 0
        for _ in range(count):
            value = (value << 1) | ((self.data[self.pos // 8]
                                     >> (7 - self.pos % 8)) & 1)
            self.pos += 1
        return value


def clock_bits(raw: bytes) -> int:
    """Read vop_time_increment_resolution from the first video object layer."""
    match = re.search(b"\x00\x00\x01[\x20-\x2f]", raw)
    if match is None:
        raise ValueError("MPEG-4 stream has no video object layer")
    bits = _Bits(raw[match.end():match.end() + 64])
    bits.read(1)  # random_accessible_vol
    bits.read(8)  # video_object_type_indication
    version = 1
    if bits.read(1):
        version = bits.read(4)
        bits.read(3)
    if bits.read(4) == 15:  # extended aspect ratio
        bits.read(16)
    if bits.read(1):  # vol_control_parameters
        bits.read(3)
        if bits.read(1):  # vbv_parameters
            for count in (15, 1, 15, 1, 15, 1, 3, 11, 1, 15, 1):
                bits.read(count)
    shape = bits.read(2)
    if shape == 3 and version != 1:
        bits.read(4)
    if shape != 0 or bits.read(1) != 1:
        raise ValueError("unsupported MPEG-4 video object layer")
    resolution = bits.read(16)
    if not resolution:
        raise ValueError("invalid MPEG-4 clock resolution")
    return max(1, (resolution - 1).bit_length())


def clock_end(vop: bytes, width: int) -> int:
    bits = _Bits(vop)
    bits.read(32)  # VOP start code
    bits.read(2)   # coding type
    while bits.read(1):  # modulo_time_base (unary)
        pass
    if bits.read(1) != 1:
        raise ValueError("invalid MPEG-4 time marker")
    bits.read(width)
    if bits.read(1) != 1:
        raise ValueError("invalid MPEG-4 time marker")
    return bits.pos


def retime_vop(donor: bytes, target: bytes, width: int) -> bytes:
    """Reuse donor prediction bits with target time, without moving slice bits.

    Unary clock rollovers can change header length. Leave those slots alone
    rather than shifting byte-aligned resync markers inside the coded payload.
    """
    donor_end = clock_end(donor, width)
    target_end = clock_end(target, width)
    if donor_end != target_end:
        return target
    # Only the small timestamp prefix is parsed. Shift the payload as an integer
    # rather than materializing a many-megabyte string of individual bits.
    donor_size = len(donor) * 8
    target_size = len(target) * 8
    prefix = int.from_bytes(target, "big") >> (target_size - target_end)
    type_shift = target_end - 34
    prefix = (prefix & ~(3 << type_shift)) | ((donor[4] >> 6) << type_shift)
    payload_size = donor_size - donor_end
    payload = int.from_bytes(donor, "big") & ((1 << payload_size) - 1)
    size = target_end + payload_size
    padding = (-size) % 8
    return (((prefix << payload_size) | payload) << padding).to_bytes(
        (size + padding) // 8, "big")


def hold_vop(target: bytes, width: int) -> bytes:
    """A non-coded P-VOP holds the reference image at the target timestamp.

    Used when every clock-rollover slot is a cut: there may be no compatible
    coded P-VOP to borrow. The next coded prediction still inherits stale pixels.
    """
    end = clock_end(target, width)
    prefix = int.from_bytes(target, "big") >> (len(target) * 8 - end)
    shift = end - 34
    prefix = (prefix & ~(3 << shift)) | (1 << shift)
    size = end + 1  # vop_coded = 0
    padding = (-size) % 8
    return (((prefix << 1) << padding) | ((1 << padding) - 1)).to_bytes(
        (size + padding) // 8, "big")


def bloom_pvops(raw: bytes, repeat: int, window: int, active_ranges=None) -> bytes:
    """Replace, not insert, P-VOP slots with cyclic bursts of prediction data.

    Every burst captures a fresh short run. This retains the frame count and
    clock while repeatedly applying the same motion/residuals to stale pixels.
    I-VOPs reset the burst so an explicitly retained recovery frame still works.
    """
    if repeat <= 1:
        return raw
    width = clock_bits(raw)
    starts = [m.start() for m in re.finditer(b"\x00\x00\x01", raw)]
    starts.append(len(raw))
    out = bytearray(raw[:starts[0]])
    donors = []
    position = 0
    window = min(max(1, window), repeat)
    frame = -1
    for start, end in zip(starts, starts[1:]):
        unit = raw[start:end]
        if unit.startswith(b"\x00\x00\x01\xb6") and len(unit) > 4:
            frame += 1
            if active_ranges is not None and not any(
                    a <= frame < b for a, b in active_ranges):
                donors.clear()
                position = 0
                out.extend(unit)
                continue
            kind = unit[4] >> 6
            if kind == 0:
                donors.clear()
                position = 0
            elif kind == 1:
                if position == 0:
                    donors.clear()
                if len(donors) < window:
                    donors.append(unit)
                unit = retime_vop(donors[position % len(donors)], unit, width)
                position = (position + 1) % repeat
        out.extend(unit)
    return bytes(out)
