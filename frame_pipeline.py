"""Timestamp-carrying packets and composable streaming frame stages.

No numeric/video dependencies are imported until a processor needs them.
"""
from dataclasses import dataclass, field, replace
from fractions import Fraction
from typing import Any


@dataclass(frozen=True)
class FramePacket:
    frame: Any
    index: int
    timestamp: Fraction
    time_base: Fraction
    duration: Fraction = Fraction(0)
    context: dict = field(default_factory=dict)


def _stage(packets, processor):
    if hasattr(processor, "push"):
        for packet in packets:
            yield from processor.push(packet)
        yield from processor.flush()
    else:
        for packet in packets:
            result = processor.process(packet.frame, packet.index,
                                       float(packet.timestamp), packet.context)
            yield replace(packet, frame=result)


def process_packets(packets, processors):
    """Compose streaming and immediate processors, draining each stage at EOF."""
    stream = packets
    for processor in processors:
        stream = _stage(stream, processor)
    yield from stream
