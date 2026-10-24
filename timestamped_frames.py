"""Optional NUT transport preserving presentation times through RGB processors.

FFmpeg still owns decoding/encoding and process cleanup. PyAV carries timestamped
RGB frames over those pipes; nothing here starts a second renderer.
"""
from dataclasses import replace
from fractions import Fraction

from frame_pipeline import FramePacket


def read_packets(pipe, context, stop_event):
    import av

    pending = None
    previous_gap = Fraction(0)
    with av.open(pipe, "r", format="nut") as source:
        for index, frame in enumerate(source.decode(video=0)):
            if stop_event.is_set():
                return
            if frame.pts is None or frame.time_base is None:
                raise ValueError("timestamped decoder returned a frame without PTS")
            base = Fraction(frame.time_base)
            packet = FramePacket(frame.to_ndarray(format="rgb24"), index,
                                 frame.pts * base, base,
                                 (frame.duration or 0) * base, dict(context))
            if pending is not None:
                gap = packet.timestamp - pending.timestamp
                if gap <= 0:
                    raise ValueError("inversion requires strictly increasing video PTS")
                previous_gap = gap
                yield replace(pending, duration=gap)
            pending = packet
        if pending is not None:
            yield replace(pending, duration=pending.duration or previous_gap or Fraction(1, 30))


class _PipeWriter:
    """PyAV expects full writes; FileIO on a pipe can return short writes."""

    def __init__(self, pipe, stop_event):
        self.pipe = pipe
        self.stop_event = stop_event
        self.position = 0

    def write(self, data):
        remaining = memoryview(data)
        size = len(remaining)
        while remaining:
            if self.stop_event.is_set():
                raise InterruptedError("timestamped frame export cancelled")
            count = self.pipe.write(remaining)
            if count is None or count <= 0:
                raise BrokenPipeError("FFmpeg encoder stopped reading timestamped frames")
            remaining = remaining[count:]
            self.position += count
        return size

    def tell(self):
        return self.position

    def flush(self):
        self.pipe.flush()


def write_packets(packets, pipe, shape, fps, stop_event):
    import av
    import numpy as np

    iterator = iter(packets)
    first = next(iterator, None)
    if first is None:
        raise ValueError("decoder produced no video frames")
    base = Fraction(first.time_base)
    writer = _PipeWriter(pipe, stop_event)
    count = 0
    previous = None
    with av.open(writer, "w", format="nut") as output:
        stream = output.add_stream("rawvideo")
        stream.width, stream.height = shape[1], shape[0]
        stream.pix_fmt = "rgb24"
        stream.time_base = base
        stream.codec_context.time_base = base
        stream.codec_context.framerate = Fraction(str(fps)).limit_denominator(1000000)

        def send(packet):
            if (not isinstance(packet.frame, np.ndarray) or packet.frame.shape != shape
                    or packet.frame.dtype != np.uint8):
                raise ValueError("stateful processor returned an invalid RGB frame")
            pts = packet.timestamp / base
            duration = packet.duration / base
            if pts.denominator != 1 or duration.denominator != 1:
                raise ValueError("processor changed timestamps outside the decoder time base")
            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(packet.frame), format="rgb24")
            frame.pts, frame.time_base = int(pts), base
            frame.duration = int(duration)
            for encoded in stream.encode(frame):
                # rawvideo encode drops duration unless it is restored on packets.
                encoded.pts = encoded.dts = int(pts)
                encoded.time_base, encoded.duration = base, int(duration)
                output.mux(encoded)

        for packet in _prepend(first, iterator):
            if stop_event.is_set():
                return count
            if previous is not None and packet.timestamp <= previous:
                raise ValueError("processor returned non-monotonic presentation timestamps")
            send(packet)
            previous = packet.timestamp
            count += 1
        for encoded in stream.encode():
            output.mux(encoded)
    return count


def _prepend(first, iterator):
    yield first
    yield from iterator
