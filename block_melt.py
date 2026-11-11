"""Block Melt: a pixel-domain macroblock datamosh.

An MPEG decoder rebuilds each P-frame by copying macroblocks from the previous
picture along their motion vectors and then adding a residual. Block Melt
keeps its own "decoded" canvas, estimates real block motion from the source,
and replays that motion on the canvas while dropping most of the residual -
so the stale picture is shoved around block by block, the way a moshed
stream smears.
"""
import math


class BlockMelt:
    MAX_STATE_BYTES = 128 * 1024 * 1024
    BLOCK_SIZES = (8, 16, 32)
    DEFAULTS = dict(block=16, search=16, residual=4, keyframe=0, repeat=1,
                    leak=1, intra=30, grit=40, seed=0)
    _BOUNDS = dict(block=(8, 32), search=(4, 32), residual=(0, 100),
                   keyframe=(0, 600), repeat=(1, 8), leak=(0, 100), intra=(0, 100),
                   grit=(0, 100), seed=(0, 2147483647))
    # Encoder-style rate terms on the downscaled luma, in grey levels per pixel:
    # a still block keeps a zero vector unless motion clearly beats it, and among
    # near-ties the shortest vector wins - flat areas do not jitter.
    ZERO_BIAS = 3.0
    VECTOR_COST = .2
    MEDIAN_SLACK = 3.0
    # intra=100 refreshes blocks whose mean match error beats INTRA_FLOOR grey
    # levels; lower settings raise that bar linearly by up to INTRA_SPAN.
    INTRA_FLOOR = 6.0
    INTRA_SPAN = 50.0

    def __init__(self):
        self._np = None
        self._canvas = None
        self._prev = None
        self._prev_luma = None
        self._rng = None
        self._seed = 0
        self.last_vectors = None
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
            raise RuntimeError("Block Melt needs NumPy; install requirements-stateful.txt") from exc
        if len(frame_shape) != 3 or frame_shape[2] != 3 or min(frame_shape) < 1:
            raise ValueError("Block Melt expects an HxWx3 RGB frame")
        self._np = np
        self._shape = tuple(int(v) for v in frame_shape)
        self._fps = self._number(fps, 30, .001, 1000)
        self._config = {}
        for key, default in self.DEFAULTS.items():
            self._config[key] = int(round(self._number(config.get(key, default), default,
                                                       *self._BOUNDS[key])))
        block = min(self.BLOCK_SIZES, key=lambda size: abs(size - self._config["block"]))
        self._config["block"] = block
        h, w, _ = self._shape
        frame_bytes = h * w * 3
        if 2 * frame_bytes + h * w * 4 > self.MAX_STATE_BYTES:
            raise ValueError("Block Melt cannot fit its canvas in the memory budget; resize first")

        # Motion search runs on a downscaled luma plane; each block is a `cell`
        # square there; vectors are refined to single pixels afterwards.
        self._block = block
        self._scale = 4 if block >= 16 else 2
        self._cell = block // self._scale
        self._nby, self._nbx = math.ceil(h / block), math.ceil(w / block)
        self._padded = (self._nby * block, self._nbx * block)
        radius = max(1, int(round(self._config["search"] / self._scale)))
        step = max(1, math.ceil(radius / 4))
        span = radius // step
        coarse = [(dy * step, dx * step) for dy in range(-span, span + 1)
                  for dx in range(-span, span + 1)]
        coarse.sort(key=lambda v: (abs(v[0]) + abs(v[1]), v))
        self._coarse = coarse
        self._refine = [(dy, dx) for dy in range(1 - step, step)
                        for dx in range(1 - step, step) if dy or dx]
        self._radius = span * step + step - 1
        hd, wd = self._nby * self._cell, self._nbx * self._cell
        self._ys_small = np.arange(hd, dtype=np.intp)[:, None]
        self._xs_small = np.arange(wd, dtype=np.intp)[None, :]
        self._ys = np.arange(h, dtype=np.intp)[:, None]
        self._xs = np.arange(w, dtype=np.intp)[None, :]
        self._seed = int(seed) & 0x7fffffff
        self.runtime_notes = []
        self.reset()

    def reset(self):
        self._canvas = None
        self._prev = None
        self._prev_luma = None
        self._count = 0
        self._next_index = None
        self._last_time = None
        self._source = None
        self.last_vectors = None
        if self._np is not None:
            self._rng = self._np.random.default_rng(self._seed)

    @property
    def state_nbytes(self):
        return sum(a.nbytes for a in (self._canvas, self._prev, self._prev_luma) if a is not None)

    def serialize_config(self):
        return dict(self._config)

    # -- motion estimation -------------------------------------------------

    def _luma(self, frame):
        np = self._np
        h, w, _ = self._shape
        ph, pw = self._padded
        rgb = frame
        if (ph, pw) != (h, w):
            rgb = np.pad(frame, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
        luma = rgb @ np.array([.299, .587, .114], dtype=np.float32)
        s = self._scale
        return luma.reshape(ph // s, s, pw // s, s).mean(axis=(1, 3), dtype=np.float32)

    def _block_sad(self, diff):
        c = self._cell
        return self._np.abs(diff).reshape(self._nby, c, self._nbx, c).sum(axis=(1, 3))

    def _vectors(self, luma):
        """best (dy, dx) per block in downscaled pixels: cur(p) ~ prev(p + v)."""
        np = self._np
        r = self._radius + 1  # one spare cell for the sub-cell neighbours
        prev = np.pad(self._prev_luma, r, mode="edge")
        hd, wd = luma.shape
        area = self._cell * self._cell
        best = np.full((self._nby, self._nbx), np.inf, dtype=np.float32)
        vy = np.zeros((self._nby, self._nbx), dtype=np.intp)
        vx = np.zeros_like(vy)
        for dy, dx in self._coarse:
            cost = self._block_sad(luma - prev[r + dy:r + dy + hd, r + dx:r + dx + wd])
            cost += area * (self.VECTOR_COST * (abs(dy) + abs(dx))
                            - (self.ZERO_BIAS if not (dy or dx) else 0))
            better = cost < best
            best[better], vy[better], vx[better] = cost[better], dy, dx
        c = self._cell
        flat = prev.ravel()
        stride = prev.shape[1]

        def gathered_cost(ty, tx):
            rows = self._ys_small + r + np.repeat(ty, c, 0).repeat(c, 1)
            cols = self._xs_small + r + np.repeat(tx, c, 0).repeat(c, 1)
            cost = self._block_sad(luma - flat[rows * stride + cols])
            return cost + area * self.VECTOR_COST * (np.abs(ty) + np.abs(tx))

        def consider(ty, tx, slack=0.0):
            cost = gathered_cost(ty, tx)
            better = cost - slack < best
            best[better], vy[better], vx[better] = cost[better], ty[better], tx[better]

        for ry, rx in self._refine:
            consider(vy + ry, vx + rx)
        # Encoders predict each vector from its neighbours, so real motion
        # fields are coherent; prefer the neighbourhood median when it is
        # nearly as good, which keeps flat areas from shuffling into noise.
        if min(vy.shape) >= 3:
            consider(self._median3(vy), self._median3(vx), area * self.MEDIAN_SLACK)
        # Sub-cell precision from a parabola through the neighbouring costs, so
        # the canvas moves in single pixels: diverging blocks then stretch into
        # streaks instead of repeating whole search cells as a comb.
        centre = gathered_cost(vy, vx)
        fy = self._parabola(gathered_cost(vy - 1, vx), centre, gathered_cost(vy + 1, vx))
        fx = self._parabola(gathered_cost(vy, vx - 1), centre, gathered_cost(vy, vx + 1))
        s = self._scale
        pix_y = np.rint((vy + fy) * s).astype(np.intp)
        pix_x = np.rint((vx + fx) * s).astype(np.intp)
        return pix_y, pix_x, best / area

    def _parabola(self, before, centre, after):
        np = self._np
        curve = before + after - 2 * centre
        with np.errstate(divide="ignore", invalid="ignore"):
            shift = np.where(curve > 1e-6, (before - after) / (2 * curve), 0)
        return np.clip(shift, -.5, .5)

    def _median3(self, field):
        np = self._np
        padded = np.pad(field, 1, mode="edge")
        h, w = field.shape
        stack = [padded[y:y + h, x:x + w] for y in range(3) for x in range(3)]
        return np.median(np.stack(stack), axis=0).astype(field.dtype)

    # -- frame pass --------------------------------------------------------

    def _refresh(self, canvas, frame, leak):
        """leaked blocks come back from the real frame, coarsened by grit:
        a sub-block smear plus posterize, like a starved intra refresh."""
        np = self._np
        h, w, _ = self._shape
        ph, pw = self._padded
        b = self._block
        padded = frame
        if (ph, pw) != (h, w):
            padded = np.pad(frame, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
        else:
            padded = frame.copy()
        q = self._config["grit"] / 100
        if q > 0:
            blocks = padded.reshape(self._nby, b, self._nbx, b, 3).swapaxes(1, 2)
            picked = blocks[leak].astype(np.float32)
            sub = max(2, b // 4)
            n, k = len(picked), b // sub
            means = picked.reshape(n, k, sub, k, sub, 3).mean(axis=(2, 4))
            coarse = means.repeat(sub, 1).repeat(sub, 2)
            mixed = picked * (1 - q) + coarse * q
            step = 1 + int(q * 40)
            blocks[leak] = np.clip((mixed // step) * step + step // 2, 0, 255).astype(np.uint8)
        mask = np.repeat(leak, b, 0).repeat(b, 1)[:h, :w]
        np.copyto(canvas, padded[:h, :w], where=mask[..., None])

    def process(self, frame, frame_index, timestamp, context=None):
        if self._np is None:
            raise RuntimeError("initialize Block Melt before processing")
        np = self._np
        if not isinstance(frame, np.ndarray) or frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"Block Melt expects uint8 RGB frame {self._shape}")
        context = context or {}
        index = int(frame_index)
        stamp = self._number(timestamp, index / self._fps, -1e12, 1e12)
        clip = context.get("clip")
        source = context.get("source_id", getattr(clip, "path", None))
        # Scene cuts are deliberately moshed through: the new shot's motion
        # drags the old picture, like a stream with its I-frames removed.
        if ((self._next_index is not None and index != self._next_index)
                or (self._last_time is not None and stamp <= self._last_time)
                or (self._source is not None and source != self._source)):
            self.reset()
        luma = self._luma(frame)
        keyframe = self._config["keyframe"]
        leak_draw = self._rng.random((self._nby, self._nbx))
        if self._canvas is None or (keyframe and self._count % keyframe == 0):
            self._canvas = frame.copy()
            self.last_vectors = np.zeros((self._nby, self._nbx, 2), dtype=np.intp)
        else:
            vy, vx, error = self._vectors(luma)
            b = self._block
            h, w, _ = self._shape
            self.last_vectors = np.stack((vy, vx), axis=-1)
            pix_y = np.repeat(vy, b, 0).repeat(b, 1)[:h, :w]
            pix_x = np.repeat(vx, b, 0).repeat(b, 1)[:h, :w]
            index_map = (np.clip(self._ys + pix_y, 0, h - 1) * w
                         + np.clip(self._xs + pix_x, 0, w - 1)).ravel()
            canvas = self._canvas.reshape(-1, 3)
            for _ in range(self._config["repeat"]):
                canvas = np.take(canvas, index_map, axis=0)
            residual = self._config["residual"] / 100
            if residual > 0:
                predicted = np.take(self._prev.reshape(-1, 3), index_map, axis=0)
                delta = frame.reshape(-1, 3).astype(np.int16) - predicted
                canvas = np.clip(canvas + np.rint(delta * residual).astype(np.int16),
                                 0, 255).astype(np.uint8)
            canvas = canvas.reshape(self._shape)
            leak = leak_draw < self._config["leak"] / 100
            intra = self._config["intra"]
            if intra:
                # Like an encoder giving up on prediction: blocks whose best
                # match is still badly wrong are coded fresh from the source.
                leak |= error > self.INTRA_FLOOR + (100 - intra) * self.INTRA_SPAN / 100
            if leak.any():
                self._refresh(canvas, frame, leak)
            self._canvas = canvas
        self._prev = frame.copy()
        self._prev_luma = luma
        self._count += 1
        self._last_time = stamp
        self._next_index = index + 1
        self._source = source
        return self._canvas.copy()
