"""Pixel-domain optical-flow datamosh.

A float canvas holds the moshed picture. Every frame the dense optical flow
between the previous and current *source* frames is measured on a small gray
copy, scaled back up, optionally quantized into macroblocks, amplified, and
used to drag the canvas along - the new motion applied to stale pixels, the
way P-frames smear when their I-frame is missing. A little of the real frame
can bleed back in, still areas can be pulled back to the live picture, and the
canvas can be refreshed on a keyframe interval or at scene cuts.
"""
import math


class FlowMosh:
    MAX_STATE_BYTES = 128 * 1024 * 1024
    DETAIL = {"low": 160, "medium": 320, "high": 480}
    DEFAULTS = dict(amplify=1.25, keyframe=75, bleed=2, blocks=12, still=0,
                    refresh="never", detail="medium", seed=0)
    _BOUNDS = dict(amplify=(0, 6), keyframe=(0, 600), bleed=(0, 100),
                   blocks=(0, 64), still=(0, 100), seed=(0, 2147483647))
    STILL_FLOW = 0.6  # analysis-scale pixels per frame counted as "not moving"
    CUT_DELTA = 120   # mean abs difference of a coarse sample that marks a cut

    def __init__(self):
        self._np = None
        self._cv2 = None
        self._canvas = None
        self._prev_gray = None
        self._sample = None
        self._residual = None
        self._grid = ()
        self._next_index = None
        self._last_time = None
        self._source = None
        self._since_key = 0
        self._next_key = 0
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
            raise RuntimeError("flowmosh needs NumPy; install requirements-stateful.txt") from exc
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("flowmosh needs OpenCV; install requirements-collapse.txt") from exc
        if len(frame_shape) != 3 or frame_shape[2] != 3 or min(frame_shape) < 1:
            raise ValueError("flowmosh expects an HxWx3 RGB frame")
        self._np, self._cv2 = np, cv2
        self._shape = tuple(int(v) for v in frame_shape)
        self._fps = self._number(fps, 30, .001, 1000)
        config = config or {}
        self._config = {}
        for key, default in self.DEFAULTS.items():
            if key in ("refresh", "detail"):
                value = str(config.get(key, default))
                allowed = ("never", "scene") if key == "refresh" else tuple(self.DETAIL)
                self._config[key] = value if value in allowed else default
                continue
            value = self._number(config.get(key, default), default, *self._BOUNDS[key])
            self._config[key] = float(value) if key == "amplify" else int(value)
        h, w, _ = self._shape
        side = self.DETAIL[self._config["detail"]]
        scale = min(1.0, side / max(h, w))
        self._analysis = (max(8, int(round(w * scale))), max(8, int(round(h * scale))))
        self._flow_scale = (w / self._analysis[0], h / self._analysis[1])
        state = (h * w * 3 * 4          # float canvas
                 + h * w * 4 * 2        # remap grids
                 + self._analysis[0] * self._analysis[1] * 2)
        if state > self.MAX_STATE_BYTES:
            raise ValueError("flowmosh canvas does not fit its 128 MiB budget; resize first")
        self._grid = (np.tile(np.arange(w, dtype=np.float32), (h, 1)),
                      np.repeat(np.arange(h, dtype=np.float32)[:, None], w, axis=1))
        self._rng_seed = int(seed) & 0x7fffffff
        self.runtime_notes = []
        self.reset()

    def reset(self):
        self._canvas = None
        self._prev_gray = None
        self._sample = None
        self._residual = None
        self._next_index = None
        self._last_time = None
        self._source = None
        self._since_key = 0
        if self._np is not None:
            self._rng = self._np.random.default_rng(self._rng_seed)
            self._next_key = self._key_interval()

    def _key_interval(self):
        base = self._config["keyframe"]
        if base <= 0:
            return 0
        # seeded +-25% jitter so refreshes do not land on a metronome
        return max(1, int(round(base * self._rng.uniform(.75, 1.25))))

    @property
    def state_nbytes(self):
        arrays = list(self._grid) + [a for a in (self._canvas, self._prev_gray, self._sample, self._residual)
                                     if a is not None]
        return sum(a.nbytes for a in arrays)

    def serialize_config(self):
        return dict(self._config)

    def _gray(self, frame):
        cv2 = self._cv2
        small = cv2.resize(frame, self._analysis, interpolation=cv2.INTER_AREA)
        return cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)

    def _key(self, frame, gray):
        self._canvas = frame.astype(self._np.float32)
        self._prev_gray = gray
        self._since_key = 0
        self._next_key = self._key_interval()

    def process(self, frame, frame_index, timestamp, context=None):
        if self._np is None:
            raise RuntimeError("initialize flowmosh before processing")
        np, cv2 = self._np, self._cv2
        if not isinstance(frame, np.ndarray) or frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"flowmosh expects uint8 RGB frame {self._shape}")
        context = context or {}
        index = int(frame_index)
        stamp = self._number(timestamp, index / self._fps, -1e12, 1e12)
        clip = context.get("clip")
        source = context.get("source_id", getattr(clip, "path", None))
        if ((self._next_index is not None and index != self._next_index)
                or (self._last_time is not None and stamp <= self._last_time)
                or (self._source is not None and source != self._source)):
            self.reset()
        h, w, _ = self._shape
        sample = frame[::max(1, h // 32), ::max(1, w // 32)].astype(np.int16)
        cut = bool(context.get("scene_cut")) or (
            self._sample is not None and np.abs(sample - self._sample).mean() > self.CUT_DELTA)
        self._sample = sample
        self._last_time = stamp
        self._next_index = index + 1
        self._source = source
        gray = self._gray(frame)

        if self._canvas is None or (cut and self._config["refresh"] == "scene"):
            self._key(frame, gray)
            return frame.copy()
        self._since_key += 1
        if self._next_key and self._since_key >= self._next_key:
            self._key(frame, gray)
            return frame.copy()

        amplify = self._config["amplify"]
        if cut or amplify <= 0:
            # flow across a cut is noise: hold the old picture still instead
            flow = None
        else:
            # backward flow: current pixel p came from p + flow(p) in the previous frame
            flow = cv2.calcOpticalFlowFarneback(gray, self._prev_gray, None,
                                                0.5, 3, 21, 3, 5, 1.2, 0)
        if flow is not None:
            magnitude = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)
            flow = flow * np.array([self._flow_scale[0] * amplify,
                                    self._flow_scale[1] * amplify], np.float32)
            block = self._config["blocks"]
            if block > 1:
                # macroblock motion vectors: one whole-pixel vector per block,
                # like a codec. Copying blocks by integer offsets keeps them
                # crisp; the sub-pixel remainder is carried to the next frame
                # so slow motion still drifts instead of freezing.
                bw = max(1, int(round(w / block)))
                bh = max(1, int(round(h / block)))
                step = cv2.resize(flow, (bw, bh), interpolation=cv2.INTER_AREA)
                if self._residual is None or self._residual.shape != step.shape:
                    self._residual = np.zeros_like(step)
                step += self._residual
                whole = np.rint(step)
                self._residual = step - whole
                flow = cv2.resize(whole, (w, h), interpolation=cv2.INTER_NEAREST)
                interp = cv2.INTER_NEAREST
            else:
                flow = cv2.resize(flow, (w, h), interpolation=cv2.INTER_LINEAR)
                interp = cv2.INTER_LINEAR
            gx, gy = self._grid
            self._canvas = cv2.remap(self._canvas, gx + flow[..., 0], gy + flow[..., 1],
                                     interp, borderMode=cv2.BORDER_REFLECT)
            still = self._config["still"] / 100
            if still > 0:
                weight = np.clip(1 - magnitude / self.STILL_FLOW, 0, 1) * still
                weight = cv2.resize(weight, (w, h), interpolation=cv2.INTER_LINEAR)[..., None]
                self._canvas += (frame - self._canvas) * weight
        bleed = self._config["bleed"] / 100
        if bleed > 0:
            self._canvas += (frame - self._canvas) * bleed
        self._prev_gray = gray
        return np.clip(np.rint(self._canvas), 0, 255).astype(np.uint8)
