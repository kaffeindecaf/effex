"""Ripple tank: motion drops stones into a simulated water surface over the picture.

A small height field runs the 2D wave equation. Wherever the source moves,
the frame difference pushes the surface; optional rain adds random drops. The
surface then refracts the live frame (pixels are sampled along its slope),
splits RGB slightly like a prism, and focuses light into caustics where the
surface curves. All state is a few low-resolution float fields, so memory
stays small at any frame size.
"""
import math


class RippleTank:
    MAX_STATE_BYTES = 128 * 1024 * 1024
    SIM_CELLS = 360  # longest side of the wave grid
    HEIGHT_LIMIT = 6.0
    DEFAULTS = dict(strength=60, threshold=6, rain=1.5, depth=14, ring=85,
                    speed=.45, prism=20, caustics=40, seed=0)
    _BOUNDS = dict(strength=(0, 100), threshold=(0, 50), rain=(0, 20),
                   depth=(0, 60), ring=(0, 100), speed=(.05, .7),
                   prism=(0, 100), caustics=(0, 100), seed=(0, 2147483647))
    _FLOATS = ("rain", "speed")

    def __init__(self):
        self._np = None
        self._arrays = ()
        self._height = self._previous = self._luma = None
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
            raise RuntimeError("Ripple Tank needs NumPy; install requirements-stateful.txt") from exc
        if len(frame_shape) != 3 or frame_shape[2] != 3 or min(frame_shape) < 1:
            raise ValueError("Ripple Tank expects an HxWx3 RGB frame")
        self._np = np
        self._shape = tuple(int(v) for v in frame_shape)
        self._fps = self._number(fps, 30, .001, 1000)
        self._config = {}
        for key, default in self.DEFAULTS.items():
            value = self._number(config.get(key, default), default, *self._BOUNDS[key])
            self._config[key] = float(value) if key in self._FLOATS else int(value)
        cfg = self._config
        self._enabled = ((cfg["strength"] > 0 or cfg["rain"] > 0)
                         and (cfg["depth"] > 0 or cfg["caustics"] > 0))
        h, w, _ = self._shape
        self._grid = max(1, math.ceil(max(h, w) / self.SIM_CELLS))
        self._sim = (math.ceil(h / self._grid), math.ceil(w / self._grid))
        self._c2 = cfg["speed"] ** 2  # stable while c^2 <= 0.5 on a 2D grid
        self._damp = .90 + .099 * cfg["ring"] / 100
        self._push = 3.0 * cfg["strength"] / 100
        self._threshold = cfg["threshold"] / 100
        self._drops_per_frame = cfg["rain"] / self._fps
        p = .5 * cfg["prism"] / 100
        self._channel_depth = tuple(cfg["depth"] * f for f in (1 - p, 1.0, 1 + p))
        self._rng = np.random.default_rng(int(seed) & 0x7fffffff)

        # bilinear lookup from pixel rows/cols into the wave grid
        sh, sw = self._sim
        y = np.arange(h, dtype=np.float32) / self._grid
        x = np.arange(w, dtype=np.float32) / self._grid
        self._y0 = np.minimum(y.astype(np.int32), sh - 1)
        self._y1 = np.minimum(self._y0 + 1, sh - 1)
        self._wy = (y - self._y0).astype(np.float32)[:, None]
        self._x0 = np.minimum(x.astype(np.int32), sw - 1)
        self._x1 = np.minimum(self._x0 + 1, sw - 1)
        self._wx = (x - self._x0).astype(np.float32)
        self._ys = np.arange(h, dtype=np.float32)[:, None]
        self._xs = np.arange(w, dtype=np.float32)[None, :]
        self._arrays = (self._y0, self._y1, self._wy, self._x0, self._x1,
                        self._wx, self._ys, self._xs)
        fields = 3 * sh * sw * 4
        if sum(a.nbytes for a in self._arrays) + fields > self.MAX_STATE_BYTES:
            raise ValueError("Ripple Tank cannot fit its wave grid in its memory budget; resize first")
        self.runtime_notes = []
        self.reset()

    def reset(self):
        if self._np is None:
            return
        np = self._np
        self._height = np.zeros(self._sim, np.float32)
        self._previous = np.zeros(self._sim, np.float32)
        self._luma = None
        self._next_index = None
        self._last_time = None
        self._source = None

    @property
    def state_nbytes(self):
        fields = (self._height, self._previous, self._luma)
        return (sum(a.nbytes for a in self._arrays) +
                sum(f.nbytes for f in fields if f is not None))

    def serialize_config(self):
        return dict(self._config)

    def _laplacian(self, field):
        p = self._np.pad(field, 1, mode="edge")
        return p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:] - 4 * field

    def _blur(self, field):
        p = self._np.pad(field, 1, mode="edge")
        rows = p[:-2] + p[1:-1] + p[2:]
        return (rows[:, :-2] + rows[:, 1:-1] + rows[:, 2:]) / 9

    def _upsample(self, field):
        rows = field[self._y0] * (1 - self._wy) + field[self._y1] * self._wy
        return rows[:, self._x0] * (1 - self._wx) + rows[:, self._x1] * self._wx

    def _rain(self):
        np = self._np
        sh, sw = self._sim
        for _ in range(int(self._rng.poisson(self._drops_per_frame))):
            radius = max(1.0, self._rng.uniform(.006, .018) * max(sh, sw))
            amp = self._rng.uniform(1.5, 3.0)
            cy, cx = self._rng.uniform(0, sh), self._rng.uniform(0, sw)
            reach = int(math.ceil(radius * 3))
            y0, y1 = max(0, int(cy) - reach), min(sh, int(cy) + reach + 1)
            x0, x1 = max(0, int(cx) - reach), min(sw, int(cx) + reach + 1)
            yy = np.arange(y0, y1, dtype=np.float32)[:, None] - cy
            xx = np.arange(x0, x1, dtype=np.float32)[None, :] - cx
            self._height[y0:y1, x0:x1] += amp * np.exp(-(yy * yy + xx * xx) / (radius * radius))

    def process(self, frame, frame_index, timestamp, context=None):
        if self._np is None:
            raise RuntimeError("initialize Ripple Tank before processing")
        np = self._np
        if not isinstance(frame, np.ndarray) or frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"Ripple Tank expects uint8 RGB frame {self._shape}")
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

        g = self._grid
        luma = frame[::g, ::g].mean(axis=2, dtype=np.float32) / 255
        if self._luma is not None:
            if self._push > 0:
                motion = np.maximum(np.abs(luma - self._luma) - self._threshold, 0)
                self._height += self._blur(motion) * self._push
            if self._drops_per_frame > 0:
                self._rain()
            step = (2 * self._height - self._previous
                    + self._c2 * self._laplacian(self._height)) * self._damp
            self._previous = self._height
            self._height = np.clip(step, -self.HEIGHT_LIMIT, self.HEIGHT_LIMIT).astype(np.float32)
        self._luma = luma
        self._last_time = stamp
        self._next_index = index + 1
        self._source = source

        if not np.any(self._height):
            return frame.copy()
        return self._render(frame)

    def _render(self, frame):
        np = self._np
        h, w, _ = self._shape
        field = np.pad(self._height, 1, mode="edge")
        gx = self._upsample((field[1:-1, 2:] - field[1:-1, :-2]) / 2)
        gy = self._upsample((field[2:, 1:-1] - field[:-2, 1:-1]) / 2)
        output = np.empty_like(frame)
        depths = self._channel_depth
        if depths[0] == depths[2]:
            iy = np.clip(np.rint(self._ys + gy * depths[1]), 0, h - 1).astype(np.intp)
            ix = np.clip(np.rint(self._xs + gx * depths[1]), 0, w - 1).astype(np.intp)
            output[:] = frame[iy, ix]
        else:
            for channel, depth in enumerate(depths):
                iy = np.clip(np.rint(self._ys + gy * depth), 0, h - 1).astype(np.intp)
                ix = np.clip(np.rint(self._xs + gx * depth), 0, w - 1).astype(np.intp)
                output[:, :, channel] = frame[iy, ix, channel]
        caustics = self._config["caustics"] / 100
        if caustics > 0:
            focus = np.clip(-1.5 * self._upsample(self._laplacian(self._height)), -.5, 1)
            gain = 1 + caustics * focus
            output = np.clip(np.rint(output * gain[:, :, None]), 0, 255).astype(np.uint8)
        return output
