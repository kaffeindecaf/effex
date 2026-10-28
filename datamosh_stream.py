"""MPEG-4 Part 2 prediction reuse with the destination VOP's clock intact.

Bloom/pulse techniques: github.com/GlitchTools/tomato and
 github.com/tiberiuiancu/datamoshing. Independent implementation; no vendored code.
Only the rectangular, non-partitioned streams produced by Effex are supported.
"""
import re


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


def _clock_end(vop: bytes, width: int) -> int:
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
    donor_end = _clock_end(donor, width)
    target_end = _clock_end(target, width)
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


def bloom_pvops(raw: bytes, repeat: int, window: int) -> bytes:
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
    for start, end in zip(starts, starts[1:]):
        unit = raw[start:end]
        if unit.startswith(b"\x00\x00\x01\xb6") and len(unit) > 4:
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
