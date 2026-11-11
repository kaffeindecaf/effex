"""Bounded, timestamped backward propagation of future visual corruption.

Forward Farneback flow F(t -> t+1) is a *sampling* map for bringing t+1
into t: remap(future, x + F.x, y + F.y). It must not be negated.
Windows share original packets and switch overlapping reconstructions by block.
Future pixels are a damaged prediction reference: block copies and wrong/held
motion vectors carry texture backward. Strength controls replacement coverage,
never translucent RGB opacity or color dilation.
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
                    bloom_strength=.9, diffusion=.85, seed=42,
                    lookahead_mode="frames", lookahead_seconds=1.,
                    flow_scale=.5)
    BOUNDS = dict(intensity=(0, 1), lookahead_frames=(0, 128),
                  infection_strength=(0, 1), backward_decay=(0, 1),
                  spread_radius=(-16, 16), block_size=(1, 128),
                  motion_alignment=(0, 1), mutation_strength=(0, 1),
                  window_overlap=(0, 64), bloom_strength=(0, 1),
                  diffusion=(0, 1), seed=(0, 2147483647),
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
            self._grid2 = None
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
        # One interleaved (x, y) sampling grid: OpenCV remaps take it directly.
        self._grid2 = np.empty((h, w, 2), np.float32)
        self._grid2[..., 0] = np.arange(w, dtype=np.float32)
        self._grid2[..., 1] = np.arange(h, dtype=np.float32)[:, None]
        self._grid = (self._grid2[..., 0], self._grid2[..., 1])
        block = self._config["block_size"]
        self._blocks = (max(1, math.ceil(w / block)), max(1, math.ceil(h / block)))
        gx, gy = self._blocks
        # Nearest-neighbour resize is far faster than ndarray.repeat; use it
        # only when it reproduces exact block tiles for this grid.
        probe = np.arange(gx * gy, dtype=np.float32).reshape(gy, gx)
        self._fast_expand = True
        self._fast_expand = np.array_equal(self._expand_blocks(probe),
                                           probe.repeat(block, 0).repeat(block, 1)[:h, :w])
        rng = np.random.default_rng(self._seed)
        self._noise = tuple(rng.uniform(-1, 1, (gy, gx)).astype(np.float32)
                            for _ in range(3))
        # Fixed per-block thresholds, computed once instead of every frame.
        yy, xx = np.indices((gy, gx))
        # An ordered 2x2 rank guarantees small subjects retain fragments; pure
        # random thresholds could reject every block occupied by a small object.
        bayer = np.array([[0, 2], [3, 1]], dtype=np.float32)
        self._replace_threshold = bayer[(yy + self._seed // 2) % 2, (xx + self._seed) % 2] * .125
        self._replace_threshold += (self._noise[2] + 1) * .0125
        self._overlap_threshold = (self._noise[0] + 1) * .5
        self._displacements = {}
        self.reset()

    def serialize_config(self):
        return dict(self._config)

    def reset(self):
        self._queue.clear()
        self._overlap.clear()
        self._feedback_frame = self._feedback_mask = self._feedback_gray = None
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
                    + (e["flow"].nbytes if e["flow"] is not None else 0)
                    + (e["mosh"].nbytes if e.get("mosh") is not None else 0) for e in self._queue)
                + sum(f.nbytes for f in self._overlap.values())
                + sum(a.nbytes for a in (self._feedback_frame, self._feedback_mask,
                                          self._feedback_gray) if a is not None)
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

    def _estimate_feedback_flow(self, current, previous):
        """Estimate the inverse sampling map used to carry pixels forward."""
        if min(current.shape) < 8:
            return self._np.zeros((*current.shape, 2), self._np.float32)
        return self._cv2.calcOpticalFlowFarneback(current, previous, None,
                                                 .5, 3, 21, 3, 5, 1.2, 0)

    def _full_flow(self, small):
        h, w, _ = self._shape
        # Scale vectors on the small field; linear resizing commutes with it.
        small = small * self._np.array((w / small.shape[1], h / small.shape[0]), self._np.float32)
        return self._cv2.resize(small, (w, h), interpolation=self._cv2.INTER_LINEAR)

    def _channel_max(self, image):
        cv2 = self._cv2
        r, g, b = cv2.split(image)
        return cv2.max(cv2.max(r, g), b)

    def _sample_map(self, flow):
        """grid + flow * motion_alignment as one interleaved remap table."""
        flow = self._np.ascontiguousarray(flow, dtype=self._np.float32)
        return self._cv2.scaleAdd(flow, float(self._config["motion_alignment"]), self._grid2)

    def _remap(self, image, sample_map, interpolation, border_mode):
        return self._cv2.remap(image, sample_map, None, interpolation, borderMode=border_mode)

    def _warp(self, image, flow, displacement=None, interpolation=None, border_mode=None):
        sample_map = self._sample_map(flow)
        if displacement is not None:
            sample_map[..., 0] += displacement[0]
            sample_map[..., 1] += displacement[1]
        cv2 = self._cv2
        return self._remap(image, sample_map,
                           cv2.INTER_LINEAR if interpolation is None else interpolation,
                           cv2.BORDER_CONSTANT if border_mode is None else border_mode)

    def _expand_blocks(self, cells):
        """Expand exact block_size tiles, cropping only the last edge tiles."""
        np = self._np
        b = self._config["block_size"]
        h, w = self._shape[:2]
        is_bool = cells.dtype == bool
        source = cells.view(np.uint8) if is_bool else cells
        if self._fast_expand and source.dtype in (np.uint8, np.float32) and source.ndim in (2, 3):
            gy, gx = source.shape[:2]
            out = self._cv2.resize(np.ascontiguousarray(source), (gx * b, gy * b),
                                   interpolation=self._cv2.INTER_NEAREST)
            if out.ndim < source.ndim:
                out = out[..., None]
            out = out[:h, :w]
        else:
            out = source.repeat(b, axis=0).repeat(b, axis=1)[:h, :w]
        return out.view(bool) if is_bool else out

    def _expand_mask(self, cells):
        """Block booleans as a full-size uint8 copy mask for cv2.copyTo."""
        return self._expand_blocks(cells.astype(self._np.uint8))

    def _pool_blocks(self, field, maximum=False):
        np = self._np
        b = self._config["block_size"]
        gx, gy = self._blocks
        h, w = field.shape[:2]
        if field.dtype == bool:
            field = field.view(np.uint8)
        field = np.ascontiguousarray(field)
        if gy * b != h or gx * b != w:
            field = self._cv2.copyMakeBorder(field, 0, gy * b - h, 0, gx * b - w,
                                             self._cv2.BORDER_REPLICATE)
        # Reduce rows, then columns: both reductions run over contiguous data.
        rows = field.reshape(gy, b, gx * b)
        if maximum:
            return rows.max(axis=1).reshape(gy, gx, b).max(axis=2)
        total = rows.sum(axis=1, dtype=np.float32).reshape(gy, gx, b).sum(axis=2)
        return total / np.float32(b * b)

    def _mosh_vectors(self, flow):
        """Per-block lost/held and overdriven prediction vectors, (gy, gx, 2)."""
        np = self._np
        mutation = self._config["mutation_strength"]
        blocks = []
        for component in range(2):
            vectors = self._pool_blocks(flow[..., component]) * (1 + mutation * 3)
            vectors = np.rint(vectors / 2) * 2
            vectors[self._noise[2] > 1 - mutation * 2] = 0
            blocks.append(vectors)
        return np.stack(blocks, axis=2).astype(np.float32)

    def _mosh_flow(self, flow):
        """Lose/hold and overdrive prediction vectors at macroblock boundaries."""
        if self._config["mutation_strength"] == 0:
            return flow
        return self._expand_blocks(self._mosh_vectors(flow))

    def _displacement_blocks(self, timestamp):
        # Hold damage for three source frames, then flip the error. This gives
        # sticking/tearing reference blocks without an unbounded random drift.
        sign = 1 if int(float(timestamp) * self._fps // 3) % 2 else -1
        cached = self._displacements.get(sign)
        if cached is None:
            np = self._np
            amount = self._config["mutation_strength"] * self._config["block_size"] * 1.5
            cached = np.stack([np.rint(field * amount / 2) * (2 * sign) for field in self._noise[:2]],
                              axis=2).astype(np.float32)
            self._displacements[sign] = cached
        return cached

    def _displacement(self, timestamp):
        blocks = self._displacement_blocks(timestamp)
        return tuple(self._expand_blocks(self._np.ascontiguousarray(blocks[..., i])) for i in range(2))

    def _flow_speed(self, flow):
        cv2, np = self._cv2, self._np
        flow = np.ascontiguousarray(flow, dtype=np.float32)
        return self._pool_blocks(cv2.magnitude(flow[..., 0].copy(), flow[..., 1].copy()))

    def _prediction(self, entry):
        """Sampling map and per-block motion speed for one backward step."""
        np = self._np
        if self._config["mutation_strength"] == 0:
            flow = self._full_flow(entry["flow"])
            return self._sample_map(flow), self._flow_speed(flow)
        # Damaged vectors are per block, so cache them for overlapping windows.
        vectors = entry.get("mosh")
        if vectors is None:
            vectors = entry["mosh"] = self._mosh_vectors(self._full_flow(entry["flow"]))
        offsets = (vectors * np.float32(self._config["motion_alignment"])
                   + self._displacement_blocks(entry["packet"].timestamp))
        speed = np.hypot(vectors[..., 0], vectors[..., 1])
        return self._grid2 + self._expand_blocks(offsets), speed

    def _grow(self, image, mask, flow, depth, original=None):
        image, mask, _ = self._grow_blocks(image, mask, self._flow_speed(flow), depth, original)
        return image, mask

    def _grow_blocks(self, image, mask, speed, depth, original=None):
        """Grow the mask in whole blocks; also return its block cells (or None)."""
        np, cv2 = self._np, self._cv2
        radius = self._config["spread_radius"]
        if radius == 0:
            return image, mask, None
        cells = self._pool_blocks(mask, maximum=True)
        if radius < 0:
            interval = max(1, math.ceil(self._config["block_size"] / abs(radius)))
            if depth % interval == 0:
                cells = cv2.erode(cells, np.ones((3, 3), np.uint8), borderType=cv2.BORDER_CONSTANT,
                                  borderValue=0)
            return image, self._expand_blocks(cells), cells
        # Grow in whole prediction blocks. New support inherits an actual tile
        # of the damaged reference, including texture/background, not max RGB
        # channel values expanded into a solid colored cloud.
        b = self._config["block_size"]
        rate = (radius + np.minimum(speed, b) * .25) / b
        ticks = np.floor(depth * rate) - np.floor((depth - 1) * rate)
        gx, gy = self._blocks
        yy, xx = np.indices((gy, gx), dtype=np.int32)
        for iteration in range(max(1, math.ceil(float(rate.max())))):
            difference = self._channel_max(cv2.absdiff(image, original) if original is not None
                                           else image)
            energy = self._pool_blocks(difference) / 255
            vacant = (cells < 1e-5) | (energy < .02)
            eligible = vacant & (ticks > iteration)
            if not np.any(eligible):
                continue
            padded = np.pad(cells, 1)
            padded_energy = np.pad(energy, 1)
            best = np.full_like(cells, -1)
            donors_x, donors_y = xx.copy(), yy.copy()
            grown = cells.copy()
            for direction, (dx, dy) in enumerate(((1, 0), (-1, 0), (0, 1), (0, -1))):
                candidate = padded[1 - dy:gy + 1 - dy, 1 - dx:gx + 1 - dx]
                detail = padded_energy[1 - dy:gy + 1 - dy, 1 - dx:gx + 1 - dx]
                priority = (self._noise[0] + direction * .37) % 1
                score = candidate * .2 + detail * 4 + priority * .01
                take = eligible & (candidate > 1e-5) & (detail > energy) & (score > best)
                donors_x[take], donors_y[take] = xx[take] - dx, yy[take] - dy
                grown[take], best[take] = candidate[take], score[take]
            added = (donors_x != xx) | (donors_y != yy)
            if np.any(added):
                offsets = np.stack((donors_x - xx, donors_y - yy), axis=2).astype(np.float32) * b
                copied = self._remap(image, self._grid2 + self._expand_blocks(offsets),
                                     cv2.INTER_NEAREST, cv2.BORDER_REFLECT_101)
                image = cv2.copyTo(copied, self._expand_mask(added), np.ascontiguousarray(image))
            cells = grown
        return image, self._expand_blocks(cells), cells

    def _replace_blocks(self, original, donor, coverage):
        # Fixed per-block thresholds make decay remove fragments spatially,
        # instead of fading the entire infection into a translucent silhouette.
        selected = self._expand_mask(coverage > self._replace_threshold)
        return self._cv2.copyTo(donor, selected, original.copy())

    def _mosh_feedback(self, original, infected, gray):
        """Carry damaged prediction blocks forward like a bounded codec bloom."""
        np, cv2 = self._np, self._cv2
        # The backward pass replaces whole blocks. Turn those changes into a
        # support mask; the pixels remain intact while only the edge of their
        # moving support is diffused.
        changed = cv2.compare(self._channel_max(cv2.absdiff(original, infected)), 3, cv2.CMP_GT)
        local_mask = self._expand_mask(self._pool_blocks(changed, maximum=True) > 0)
        local = local_mask.astype(np.float32)
        if self._feedback_frame is None:
            self._feedback_frame = infected.copy()
            self._feedback_mask = local
            self._feedback_gray = gray.copy()
            return infected

        # Farneback(current, previous) gives the sampling map required to pull
        # the previous infected canvas into current-frame coordinates.
        flow_small = self._estimate_feedback_flow(gray, self._feedback_gray)
        sample_map = self._sample_map(self._full_flow(flow_small))
        carried = self._remap(self._feedback_frame, sample_map,
                              cv2.INTER_LINEAR, cv2.BORDER_REFLECT_101)
        carried_mask = self._remap(self._feedback_mask, sample_map,
                                   cv2.INTER_LINEAR, cv2.BORDER_CONSTANT)
        strength = self._config["bloom_strength"]
        diffusion = self._config["diffusion"]
        # Let fresh reverse-infected blocks join the moving feedback canvas.
        support = np.maximum(carried_mask * np.float32(.94 + .05 * strength), local)
        radius = min(24, int(math.ceil(self._config["block_size"] * diffusion)))
        if radius:
            diameter = radius * 2 + 1
            support = cv2.dilate(support, np.ones((diameter, diameter), np.uint8))
        sigma = min(self._config["block_size"] * diffusion * .35, 12)
        if sigma > .1:
            size = max(3, int(math.ceil(sigma * 6)) | 1)
            support = cv2.GaussianBlur(support, (size, size), sigmaX=sigma,
                                       borderType=cv2.BORDER_REFLECT_101)
        alpha = np.clip(support * np.float32(strength), 0, .9).astype(np.float32)
        result = cv2.blendLinear(original, carried, 1 - alpha, alpha)
        # A fresh backward-propagated tile wins over an older, softer echo.
        # Diffusion applies around its boundary; it never erases the new block.
        result = cv2.copyTo(infected, local_mask, result)
        # Accumulate actual output so carried blocks can infect subsequent
        # frames. A small persistence loss avoids turning the whole clip into a
        # uniform overlay after a long run.
        self._feedback_frame = result
        self._feedback_mask = np.maximum(local, carried_mask * np.float32(.96))
        self._feedback_gray = gray.copy()
        return result

    def _reconstruct(self, end):
        """Yield (position, reconstruction) backward, carrying mutated pixels."""
        np, cv2 = self._np, self._cv2
        future = self._queue[end]["packet"].frame
        first = self._queue[0]["packet"].frame
        image = future.copy()
        delta = self._channel_max(cv2.absdiff(future, first))
        # Ignore isolated codec/ringing noise in an otherwise unchanged tile.
        seeds = (self._pool_blocks(delta > 24) > .04).astype(np.float32)
        mask = self._expand_blocks(seeds)
        decay = np.float32(self._config["backward_decay"])
        gain = self._config["intensity"] * self._config["infection_strength"]
        for pos in range(end - 1, -1, -1):
            entry = self._queue[pos]
            sample_map, speed = self._prediction(entry)
            image = self._remap(image, sample_map, cv2.INTER_NEAREST, cv2.BORDER_REFLECT_101)
            mask = self._remap(mask, sample_map, cv2.INTER_NEAREST, cv2.BORDER_CONSTANT)
            original = entry["packet"].frame
            image, mask, cells = self._grow_blocks(image, mask, speed, end - pos, original)
            mask = mask * decay
            image = cv2.copyTo(original, (mask < 1e-5).view(np.uint8), image)
            # A grown mask is whole blocks, so its block maxima are just its cells.
            peaks = cells * decay if cells is not None else self._pool_blocks(mask, maximum=True)
            result = self._replace_blocks(original, image, peaks * gain)
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
                take_new = self._expand_mask(self._overlap_threshold < weight)
                result = self._cv2.copyTo(result, take_new, old.copy())
            if pos < hop:
                outputs[pos] = result
            else:
                new_overlap[packet.index] = result
        if final:
            outputs[end] = self._queue[end]["packet"].frame.copy()
        self._overlap = new_overlap
        for pos in range(hop):
            entry = self._queue[pos]
            result = outputs.pop(pos)
            if not (final and pos == end):
                result = self._mosh_feedback(entry["packet"].frame, result, entry["gray"])
            yield replace(entry["packet"], frame=result)
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
                self._feedback_frame = self._feedback_mask = self._feedback_gray = None
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
