"""Bounded raster desynchronization: travelling bands sample different past times."""
from collections import deque
import math


class RasterSlip:
    MAX_STATE_BYTES = 128 * 1024 * 1024
    MAX_FRAMES = 64
    DEFAULTS = dict(strength=85, coverage=40, band_size=6, delay=.35,
                    shift=12, speed=.25, split=1, seed=0)
    _BOUNDS = dict(strength=(0, 100), coverage=(0, 100), band_size=(1, 30),
                   delay=(0, 2), shift=(0, 50), speed=(0, 3), split=(0, 10),
                   seed=(0, 2147483647))

    def __init__(self):
        self._np = None
        self._history = deque()
        self._sample = None
        self._next_index = None
        self._last_time = None
        self._source = None
        self._origin = 0.0
        self._arrays = ()
        self.runtime_notes = []

    @staticmethod
    def _number(value, default, low, high):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value)) if math.isfinite(value) else default

    def initialize(self, config, frame_shape, fps, seed):
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Raster Slip needs NumPy; install requirements-stateful.txt") from exc
        if len(frame_shape) != 3 or frame_shape[2] != 3 or min(frame_shape) < 1:
            raise ValueError("Raster Slip expects an HxWx3 RGB frame")
        self._np = np
        self._shape = tuple(int(v) for v in frame_shape)
        self._fps = self._number(fps, 30, .001, 1000)
        self._config = {}
        for key, default in self.DEFAULTS.items():
            value = self._number(config.get(key, default), default, *self._BOUNDS[key])
            self._config[key] = float(value) if key in ("delay", "speed") else int(value)
        self._enabled = (self._config["strength"] > 0 and self._config["coverage"] > 0
                         and self._config["delay"] > 0)
        h, w, _ = self._shape
        self._band_height = max(1, int(round(h * self._config["band_size"] / 100)))
        count = math.ceil(h / self._band_height)
        rng = np.random.default_rng(int(seed) & 0x7fffffff)
        self._active = np.zeros(count, dtype=bool)
        active_count = int(math.ceil(count * self._config["coverage"] / 100))
        self._active[rng.permutation(count)[:active_count]] = True
        self._ages = rng.uniform(.25, 1, count)
        self._offsets = rng.choice((-1, 1), count) * rng.uniform(.4, 1, count)
        self._phases = rng.uniform(0, 2 * math.pi, count)
        self._ys = np.arange(h, dtype=np.int32)
        self._xs = np.arange(w, dtype=np.int32)
        self._arrays = (self._active, self._ages, self._offsets, self._phases, self._ys, self._xs)
        sample_bytes = len(range(0, h, max(1, h // 32))) * len(range(0, w, max(1, w // 32))) * 3
        frame_bytes = h * w * 3
        room = max(0, self.MAX_STATE_BYTES - sum(a.nbytes for a in self._arrays) - sample_bytes)
        capacity = min(self.MAX_FRAMES, int(math.ceil(self._config["delay"] * self._fps)),
                       room // frame_bytes) if self._enabled else 0
        if self._enabled and capacity < 1:
            raise ValueError("Raster Slip cannot fit one history frame in its memory budget; resize first")
        self.effective_delay = min(self._config["delay"], capacity / self._fps)
        self.runtime_notes = []
        if self._enabled and self.effective_delay + 1e-9 < self._config["delay"]:
            self.runtime_notes.append(
                f"raster slip history budget limits delay to {self.effective_delay:.3f}s "
                f"(requested {self._config['delay']:g}s)")
        self._history = deque(maxlen=capacity)
        self.reset()

    def reset(self):
        self._history.clear()
        self._sample = None
        self._next_index = None
        self._last_time = None
        self._source = None
        self._origin = 0.0

    @property
    def state_nbytes(self):
        return (sum(a.nbytes for a in self._arrays) +
                sum(frame.nbytes for _, frame in self._history) +
                (self._sample.nbytes if self._sample is not None else 0))

    def serialize_config(self):
        return dict(self._config)

    def process(self, frame, frame_index, timestamp, context=None):
        if self._np is None:
            raise RuntimeError("initialize Raster Slip before processing")
        np = self._np
        if not isinstance(frame, np.ndarray) or frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"Raster Slip expects uint8 RGB frame {self._shape}")
        if not self._enabled:
            return frame.copy()
        context = context or {}
        index = int(frame_index)
        stamp = self._number(timestamp, index / self._fps, -1e12, 1e12)
        clip = context.get("clip")
        source = context.get("source_id", getattr(clip, "path", None))
        if (context.get("scene_cut") or (self._next_index is not None and index != self._next_index)
                or (self._last_time is not None and stamp <= self._last_time)
                or (self._source is not None and source != self._source)):
            self.reset()
        h, w, _ = self._shape
        sample = frame[::max(1, h // 32), ::max(1, w // 32)].copy()
        if self._sample is not None and np.abs(sample.astype(np.int16) - self._sample).mean() > 140:
            self.reset()
        output = frame.copy()
        if not self._history:
            self._origin = stamp
        else:
            elapsed = stamp - self._origin
            travel = int(elapsed * self._config["speed"] * h) % h
            bands = ((self._ys + travel) % h) // self._band_height
            alpha = self._config["strength"] / 100
            split = int(round(w * self._config["split"] / 100))
            for band in np.flatnonzero(self._active):
                rows = np.flatnonzero(bands == band)
                if not len(rows):
                    continue
                target = stamp - self.effective_delay * self._ages[band]
                donor = self._history[0][1]
                for time, saved in reversed(self._history):
                    if time <= target:
                        donor = saved
                        break
                wobble = .6 + .4 * math.sin(elapsed * self._config["speed"] * 2 * math.pi + self._phases[band])
                shift = int(round(w * self._config["shift"] / 100 * self._offsets[band] * wobble))
                for channel, chroma in enumerate((split, 0, -split)):
                    past = donor[rows[:, None], (self._xs - shift - chroma) % w, channel]
                    if alpha == 1:
                        output[rows, :, channel] = past
                    else:
                        output[rows, :, channel] = np.rint(
                            frame[rows, :, channel] * (1 - alpha) + past * alpha).astype(np.uint8)
        self._history.append((stamp, frame.copy()))
        self._sample = sample
        self._last_time = stamp
        self._next_index = index + 1
        self._source = source
        return output
