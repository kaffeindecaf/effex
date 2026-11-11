"""Bounded, timestamped backward propagation of future visual corruption.

Forward Farneback flow F(t -> t+1) is a *sampling* map for bringing t+1
into t: remap(future, x + F.x, y + F.y). It must not be negated.
Windows share original packets and crossfade their overlapping reconstructions;
random fields are tied to source time, so overlap never restarts the noise.
"""
from dataclasses import replace
import math


class ReverseInfection:
    requires_timestamps = True
    MAX_STATE_BYTES = 512 * 1024 * 1024
    MAX_FRAMES = 129
    DEFAULTS = dict(intensity=.75, lookahead_frames=24, infection_strength=.8,
                    backward_decay=.95, spread_radius=3, block_size=16,
                    motion_alignment=1., mutation_strength=.25, window_overlap=8,
                    seed=42, lookahead_mode="frames", lookahead_seconds=1.,
                    flow_scale=.5)
    BOUNDS = dict(intensity=(0, 1), lookahead_frames=(0, 128),
                  infection_strength=(0, 1), backward_decay=(0, 1),
                  spread_radius=(-16, 16), block_size=(1, 128),
                  motion_alignment=(0, 1), mutation_strength=(0, 1),
                  window_overlap=(0, 64), seed=(0, 2147483647),
                  lookahead_seconds=(0, 5), flow_scale=(.125, 1))

    def __init__(self):
        self._np = self._cv2 = None
        self._queue = []
        self._overlap = {}
        self._grid = ()
        self._noise = ()
        self.runtime_notes = []

    @classmethod
    def validate_config(cls, config):
        resolved = {}
        for key, default in cls.DEFAULTS.items():
            value = (config or {}).get(key, default)
            if key == "lookahead_mode":
                if value not in ("frames", "seconds"):
                    raise ValueError("lookahead_mode must be frames or seconds")
            else:
                try:
                    value = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"{key} must be a finite number") from None
                low, high = cls.BOUNDS[key]
                if not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f"{key} must be in {low}..{high}")
                if isinstance(default, int):
                    if not value.is_integer():
                        raise ValueError(f"{key} must be a whole number")
                    value = int(value)
            resolved[key] = value
        return resolved

    def initialize(self, config, frame_shape, fps, seed):
        try:
            import numpy as np
            import cv2
        except ImportError as exc:
            raise RuntimeError("Reverse Infection needs requirements-reverse-infection.txt") from exc
        if len(frame_shape) != 3 or frame_shape[2] != 3 or min(frame_shape) < 1:
            raise ValueError("Reverse Infection expects an HxWx3 RGB frame")
        self._np, self._cv2 = np, cv2
        self._config = self.validate_config(config)
        self._shape = tuple(frame_shape)
        self._fps = float(fps)
        if not math.isfinite(self._fps) or self._fps <= 0:
            raise ValueError("fps must be positive and finite")
        self._seed = int((config or {}).get("seed", seed) or seed)
        c = self._config
        self._bypass = (c["intensity"] == 0 or c["infection_strength"] == 0
                        or (c["lookahead_mode"] == "frames" and c["lookahead_frames"] == 0)
                        or (c["lookahead_mode"] == "seconds" and c["lookahead_seconds"] == 0))
        if self._bypass:
            self._grid = self._noise = ()
            self.effective_lookahead_frames = self.effective_overlap = 0
            self.runtime_notes = []
            self.reset()
            return
        h, w, _ = self._shape
        scale = min(self._config["flow_scale"], 320 / max(h, w))
        self._analysis = (max(1, round(w * scale)), max(1, round(h * scale)))
        # Reserve full-resolution working arrays, including simultaneous old/new
        # remaps, mask growth, output conversion and overlap blending temporaries.
        pixels = h * w
        self._reserve = pixels * 128 + 65536
        # Include the backwards pass's pending RGB output for every input slot.
        entry_bytes = pixels * 6 + self._analysis[0] * self._analysis[1] * 9
        slots = min(self.MAX_FRAMES, (self.MAX_STATE_BYTES - self._reserve) // entry_bytes)
        if slots < 2:
            raise ValueError("Reverse Infection cannot fit its memory budget; resize the source first")
        requested = (self._config["lookahead_frames"]
                     if self._config["lookahead_mode"] == "frames" else self.MAX_FRAMES - 1)
        overlap = min(self._config["window_overlap"], max(0, requested - 1), (slots - 2) // 3)
        # Cached reconstructions use one RGB uint8 frame for each overlap slot.
        self._capacity = max(2, slots - overlap)
        self.effective_lookahead_frames = min(requested, self._capacity - 1)
        self.effective_overlap = min(overlap, max(0, self.effective_lookahead_frames - 1))
        self.runtime_notes = []
        if requested > self._capacity - 1:
            self.runtime_notes.append("Reverse Infection lookahead limited to "
                                      f"{self._capacity - 1} frames by the 512 MiB / 129-frame budget")
        horizon = (f"{self.effective_lookahead_frames} frames" if self._config["lookahead_mode"] == "frames"
                   else f"{self._config['lookahead_seconds']:g}s of source timestamps (capacity bounded)")
        self.runtime_notes.append(f"Reverse Infection buffers {horizon} before emitting; "
                                  f"{self.effective_overlap} overlap frames; EOF/cuts drain locally")
        self._grid = (np.broadcast_to(np.arange(w, dtype=np.float32), (h, w)),
                      np.broadcast_to(np.arange(h, dtype=np.float32)[:, None], (h, w)))
        block = self._config["block_size"]
        self._blocks = (max(1, math.ceil(w / block)), max(1, math.ceil(h / block)))
        rng = np.random.default_rng(self._seed)
        self._noise = tuple(rng.uniform(-1, 1, (self._blocks[1], self._blocks[0])).astype(np.float32)
                            for _ in range(3))
        self.reset()

    def serialize_config(self):
        return dict(self._config)

    def reset(self):
        self._queue.clear()
        self._overlap.clear()
        self._next_index = self._last_timestamp = self._source = None
        self.capacity_fallbacks = 0
        self.peak_buffered_frames = 0
        self.peak_state_bytes = 0

    @property
    def buffered_frames(self):
        return len(self._queue)

    @property
    def state_nbytes(self):
        return (sum(e["packet"].frame.nbytes + e["gray"].nbytes
                    + (e["flow"].nbytes if e["flow"] is not None else 0) for e in self._queue)
                + sum(f.nbytes for f in self._overlap.values())
                + sum(a.nbytes for a in self._noise)
                + sum(a.nbytes for a in self._grid))

    def _gray(self, frame):
        cv2 = self._cv2
        return cv2.cvtColor(cv2.resize(frame, self._analysis, interpolation=cv2.INTER_AREA),
                            cv2.COLOR_RGB2GRAY)

    def _estimate_flow(self, previous, future):
        if min(previous.shape) < 8:
            return self._np.zeros((*previous.shape, 2), self._np.float32)
        return self._cv2.calcOpticalFlowFarneback(previous, future, None,
                                                 .5, 3, 21, 3, 5, 1.2, 0)

    def _full_flow(self, small):
        h, w, _ = self._shape
        flow = self._cv2.resize(small, (w, h), interpolation=self._cv2.INTER_LINEAR)
        flow[..., 0] *= w / small.shape[1]
        flow[..., 1] *= h / small.shape[0]
        return flow

    def _warp(self, image, flow, displacement=None):
        x = self._grid[0] + flow[..., 0] * self._config["motion_alignment"]
        y = self._grid[1] + flow[..., 1] * self._config["motion_alignment"]
        if displacement is not None:
            x += displacement[0]
            y += displacement[1]
        return self._cv2.remap(image, x, y, self._cv2.INTER_LINEAR,
                               borderMode=self._cv2.BORDER_CONSTANT)

    def _displacement(self, timestamp):
        np, cv2 = self._np, self._cv2
        h, w, _ = self._shape
        amount = self._config["mutation_strength"] * self._config["block_size"]
        phase = float(timestamp) * 1.7
        return tuple(cv2.resize(field * np.float32(amount * math.sin(phase + offset)),
                                (w, h), interpolation=cv2.INTER_NEAREST)
                     for field, offset in zip(self._noise[:2], (.5, 2.1)))

    def _grow(self, image, mask, flow):
        np, cv2 = self._np, self._cv2
        radius = self._config["spread_radius"]
        if radius == 0:
            return image, mask
        k = abs(radius) * 2 + 1
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))
        if radius < 0:
            return image, cv2.erode(mask, kernel)
        expanded = cv2.dilate(mask, kernel)
        # Moving regions receive a second, smoothly weighted ring of growth.
        speed = np.minimum(np.linalg.norm(flow, axis=2) / self._config["block_size"], 1)
        expanded += (cv2.dilate(expanded, kernel) - expanded) * speed * .5
        # Extend the carried material into the new mask support. No additive
        # feedback: existing pixels keep their color; only new support is filled.
        support = cv2.dilate(mask, kernel)
        donor = cv2.dilate(image * mask[..., None], kernel)
        donor /= np.maximum(support[..., None], 1e-6)
        grown = expanded > mask + 1e-5
        image[grown] = donor[grown]
        return image, expanded

    def _reconstruct(self, end):
        """Yield (position, reconstruction) backward, carrying mutated pixels."""
        np, cv2 = self._np, self._cv2
        future = self._queue[end]["packet"].frame
        first = self._queue[0]["packet"].frame
        image = future.astype(np.float32)
        # Seed changed future material, rather than a full-frame ghost overlay.
        delta = np.max(np.abs(image - first.astype(np.float32)), axis=2)
        mask = np.clip((delta - 12) / 48, 0, 1).astype(np.float32)
        h, w, _ = self._shape
        fragments = cv2.resize(self._noise[2], (w, h), interpolation=cv2.INTER_NEAREST)
        mask *= np.clip(.8 + fragments * self._config["mutation_strength"], 0, 1)
        # Keep seeded holes through the recurrence, otherwise dilation quickly
        # fills every fragment and the result becomes one smooth temporal ghost.
        gate = (fragments >= -1 + self._config["mutation_strength"] * 1.2).astype(np.float32)
        if self._config["mutation_strength"] < .5:
            gate = cv2.GaussianBlur(gate, (3, 3), .6)
        for pos in range(end - 1, -1, -1):
            entry = self._queue[pos]
            flow = self._full_flow(entry["flow"])
            displacement = self._displacement(entry["packet"].timestamp)
            image = self._warp(image, flow, displacement)
            mask = self._warp(mask, flow, displacement)
            image, mask = self._grow(image, mask, flow)
            mask *= self._config["backward_decay"]
            mask *= gate
            np.clip(mask, 0, 1, out=mask)
            # Refresh only inactive donor pixels. Already-corrupted material is
            # carried through every earlier remap, without repeatedly fading RGB.
            original = entry["packet"].frame
            image[mask < 1e-5] = original[mask < 1e-5]
            alpha = (mask * self._config["intensity"]
                     * self._config["infection_strength"])[..., None]
            result = np.clip(original * (1 - alpha) + image * alpha, 0, 255).astype(np.uint8)
            yield pos, result

    def _window(self, end, final=False):
        if end < 1:
            packet = self._queue.pop(0)["packet"]
            self._overlap.pop(packet.index, None)
            yield replace(packet, frame=packet.frame.copy())
            return
        overlap = 0 if final else min(self.effective_overlap, end - 1)
        hop = end - overlap
        # At EOF output the source itself too, with its exact original metadata.
        if final:
            hop = end + 1
        outputs = {}
        new_overlap = {}
        for pos, result in self._reconstruct(end):
            packet = self._queue[pos]["packet"]
            old = self._overlap.get(packet.index)
            if old is not None:
                weight = (pos + 1) / (len(self._overlap) + 1)
                result = self._cv2.addWeighted(old, 1 - weight, result, weight, 0)
            if pos < hop:
                outputs[pos] = result
            else:
                new_overlap[packet.index] = result
        if final:
            outputs[end] = self._queue[end]["packet"].frame.copy()
        self._overlap = new_overlap
        for pos in range(hop):
            yield replace(self._queue[pos]["packet"], frame=outputs.pop(pos))
        del self._queue[:hop]

    def push(self, packet):
        if self._np is None:
            raise RuntimeError("initialize Reverse Infection before processing")
        np = self._np
        if (not isinstance(packet.frame, np.ndarray) or packet.frame.shape != self._shape
                or packet.frame.dtype != np.uint8):
            raise ValueError(f"Reverse Infection expects uint8 RGB {self._shape}")
        stamp = float(packet.timestamp)
        if not math.isfinite(stamp):
            raise ValueError("Reverse Infection needs finite timestamps")
        source = packet.context.get("source_id", getattr(packet.context.get("clip"), "path", None))
        if ((self._next_index is not None and packet.index != self._next_index)
                or (self._last_timestamp is not None and stamp <= self._last_timestamp)
                or (self._source is not None and source != self._source)):
            # Seek/source change invalidates the old timeline, just as other
            # buffered effects do. A scene cut instead drains all pending frames.
            self.reset()
        self._next_index, self._last_timestamp, self._source = packet.index + 1, stamp, source
        c = self._config
        if self._bypass:
            yield replace(packet, frame=packet.frame.copy())
            return
        gray = self._gray(packet.frame)
        cut = packet.context.get("scene_cut", False)
        if self._queue:
            old = self._queue[-1]
            # A coarse RGB difference also detects equal-luminance color cuts.
            a, b = old["packet"].frame, packet.frame
            stride = max(1, max(self._shape[:2]) // 32)
            cut = cut or np.abs(a[::stride, ::stride].astype(np.float32)
                                - b[::stride, ::stride]).mean() > 100
            if cut:
                yield from self.flush()
            else:
                old["flow"] = self._estimate_flow(old["gray"], gray)
        self._queue.append(dict(packet=replace(packet, frame=packet.frame.copy(),
                                               context=dict(packet.context)), gray=gray, flow=None))
        self.peak_buffered_frames = max(self.peak_buffered_frames, len(self._queue))
        self.peak_state_bytes = max(self.peak_state_bytes, self.state_nbytes)
        if c["lookahead_mode"] == "frames":
            if len(self._queue) >= self.effective_lookahead_frames + 1:
                yield from self._window(len(self._queue) - 1)
        else:
            while len(self._queue) > 1:
                elapsed = self._queue[-1]["packet"].timestamp - self._queue[0]["packet"].timestamp
                if float(elapsed) < c["lookahead_seconds"] and len(self._queue) < self._capacity:
                    break
                end = len(self._queue) - 1
                if float(elapsed) > c["lookahead_seconds"]:
                    end -= 1  # Never sample beyond the requested timestamp horizon.
                if len(self._queue) >= self._capacity:
                    self.capacity_fallbacks += 1
                yield from self._window(end)

    def flush(self):
        while self._queue:
            end = len(self._queue) - 1
            if self._config["lookahead_mode"] == "seconds":
                start = self._queue[0]["packet"].timestamp
                while end > 0 and float(self._queue[end]["packet"].timestamp - start) > self._config["lookahead_seconds"]:
                    end -= 1
            yield from self._window(end, final=True)
        self._overlap.clear()
