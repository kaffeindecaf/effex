"""FutureCut: bounded future-frame cutout compositor (E2.47a-d).

Borrows a coherent cutout from a *future* frame, composites it into the present
frame, holds it sharp for a few frames, then dissolves it so the video returns
to itself. NumPy and OpenCV are imported lazily on initialize(); importing this
module only touches the standard library, so it never pulls NumPy/cv2 in on its
own.

Design notes
------------
* Timestamped push/flush stage: packets are buffered and each is emitted with
  its ORIGINAL index/timestamp/duration. Output is delayed by
  ``ceil(lookahead*fps)+1`` frames so a real future donor always exists.
* At each spawn slot a seed tile is chosen (coherent = similar surface moved by
  a different phase; dissimilar = largest descriptor distance under a cap;
  random = baseline), grown into ONE blob by region growing on descriptor
  distance, morphologically cleaned and feathered. The donor pixels come from
  the buffered future frame at ``min(gap, lookahead_frames)`` frames ahead.
* Lifecycle per spawn: birth -> ``hold`` sharp frames -> ``melt`` dissolve
  frames. ``mode=slide`` dissolves the mask in blotches via a seeded noise
  threshold (never a linear fade); ``mode=smudge`` keeps a feedback accumulator
  ``acc = warp(acc, flow)*retention + future*(1-retention)`` composited through
  the decaying mask. After ``hold+melt`` frames the cutout is gone and the
  emitted frame is the live frame, bit-identical.
* Determinism: same seed + same packets => identical output. The spawn phase,
  dissolve noise and random pick all derive from the seed. Input frames are
  never mutated (buffers own copies).
"""
import math
from collections import deque
from dataclasses import replace
from typing import Any, Optional

# Tile/growth constants (analysis scale).
_TILE_DIV = 6          # analysis shorter side // _TILE_DIV => tile side
_DESC_SIDE = 8         # descriptor patch side
_GROW_DIST = 0.6       # region-growing descriptor-distance budget (normalized)
_BLOCK = 8             # numpy flow block size
_SEARCH = 8            # numpy flow +/- search radius


class FutureCut:
    """Borrow a future cutout, hold it, then dissolve it back into the video."""

    requires_timestamps = True
    MAX_STATE_BYTES: int = 128 * 1024 * 1024
    MAX_FRAMES = 128
    DEFAULTS = {
        "lookahead": 0.6, "gap": 18, "cutouts": 1, "spawn_every": 24,
        "size": 18, "pick": "coherent", "min_motion": 2, "max_motion": 25,
        "similarity": 60, "shape": "blob", "seam": "poisson",
        "feather": 35, "hold": 6, "melt": 14, "mode": "smudge",
        "retention": 70, "drift": 100, "seed": 0,
    }
    _CHOICES = {
        "pick": ("coherent", "dissimilar", "random"),
        "shape": ("blob", "rect"),
        "seam": ("none", "poisson"),
        "mode": ("slide", "smudge"),
    }
    _SCENE_CUT = 0.55            # fraction of 255 on the analysis frame
    _ANALYSIS_SIDE = 160         # longest side for scoring/flow
    _SCENE_SIDE = 32             # scene-cut detection sample side

    def __init__(self):
        self._np: Any = None
        self._config: dict = {}
        self._shape = (0, 0, 0)
        self._fps = 30.0
        self._seed = 0
        self._capacity = 1
        self._analysis_shape = (0, 0)
        self._lookahead_frames = 0
        self._delay = 0
        self._effective_gap = 1
        self._queue: deque = deque()
        self._cutouts: list = []
        self._next_index: Optional[int] = None
        self._last_timestamp: Optional[float] = None
        self._scene_sample: Any = None
        self._spawns = 0
        self._total_pushed = 0

    # --- config helpers -------------------------------------------------

    @staticmethod
    def _clamp_int(value, default, low, high):
        try:
            number = int(value)
        except (TypeError, ValueError):
            return default
        return max(low, min(high, number))

    @staticmethod
    def _clamp_float(value, default, low, high):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return default
        if not math.isfinite(number):
            return default
        return max(low, min(high, number))

    @staticmethod
    def _choice(key, value, default):
        text = str(value).strip().lower() if value is not None else default
        if text not in FutureCut._CHOICES[key]:
            raise ValueError(f"invalid future-cut {key}: {value!r}")
        return text

    # --- lifecycle ------------------------------------------------------

    def initialize(self, config: dict, frame_shape: tuple, fps: float,
                   seed: int) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "FutureCut needs NumPy; install requirements-stateful.txt"
            ) from exc
        if (len(frame_shape) != 3 or int(frame_shape[2]) != 3
                or any(int(v) < 1 for v in frame_shape)):
            raise ValueError("FutureCut expects an HxWx3 RGB frame")
        height, width, channels = (int(v) for v in frame_shape)
        self._np = np
        self._shape = (height, width, channels)
        self._fps = self._clamp_float(fps, 30.0, 0.001, 1000.0)

        config = dict(config) if config else {}
        resolved = {}
        for key, default in self.DEFAULTS.items():
            if key in self._CHOICES:
                resolved[key] = self._choice(key, config.get(key), default)
            elif key == "lookahead":
                resolved[key] = self._clamp_float(config.get(key), default,
                                                  0.1, 2.0)
            elif key == "seed":
                resolved[key] = self._clamp_int(config.get(key), default,
                                                0, 2147483647)
            else:
                low, high = {
                    "gap": (1, 60), "cutouts": (1, 3),
                    "spawn_every": (4, 120), "size": (5, 40),
                    "min_motion": (0, 30), "max_motion": (1, 100),
                    "similarity": (0, 100), "feather": (0, 100),
                    "hold": (1, 30), "melt": (2, 60),
                    "retention": (0, 95), "drift": (0, 100),
                }[key]
                resolved[key] = self._clamp_int(config.get(key), default,
                                                low, high)
        self._config = resolved

        # seed: config value wins over the argument (arg is the fallback).
        self._seed = self._clamp_int(
            config.get("seed", seed), seed, 0, 2147483647)

        # analysis resolution (longest side <= 160 px)
        scale = min(1.0, self._ANALYSIS_SIDE / max(height, width))
        self._analysis_shape = (max(1, round(height * scale)),
                                max(1, round(width * scale)))

        # bounded memory: a live cutout costs roughly seven frames of state
        # (donor copy, float32 accumulator, float32 mask, coarse noise field),
        # so reserve that before handing the rest to the look-ahead buffer.
        frame_bytes = height * width * channels
        reserve = int(resolved["cutouts"] * 3 * frame_bytes) + 65536
        self._capacity = max(1, min(self.MAX_FRAMES,
                                    (self.MAX_STATE_BYTES - reserve) // frame_bytes))
        if self._capacity < 1:
            raise ValueError(
                "future-cut cannot fit one RGB frame in its 128 MiB buffer "
                "budget; resize before future-cut")

        # buffering horizon, clamped so the queue can never outgrow the budget
        requested = int(math.ceil(resolved["lookahead"] * self._fps))
        self._lookahead_frames = max(1, min(self._capacity - 1, requested)) \
            if self._capacity > 1 else 1
        self._delay = self._lookahead_frames + 1
        self._effective_gap = max(1, min(resolved["gap"], self._lookahead_frames))

        self.runtime_notes: list = []
        if self._lookahead_frames < requested:
            self.runtime_notes.append(
                f"look-ahead limited to {self._lookahead_frames} frames "
                f"({self._lookahead_frames / max(1e-6, self._fps):.2f}s) by the "
                f"128 MiB buffer at {width}x{height}; resize for a longer horizon")
        self.reset()

    # --- diagnostics ----------------------------------------------------

    @property
    def state_nbytes(self) -> int:
        total = sum(p.frame.nbytes for p in self._queue)
        for cut in self._cutouts:
            total += cut["donor"].nbytes
            if cut["acc"] is not None:
                total += cut["acc"].nbytes
            total += cut["mask"].nbytes
            total += cut["noise"].nbytes
        return int(total)

    @property
    def buffered_frames(self) -> int:
        return len(self._queue)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def spawns(self) -> int:
        return self._spawns

    @property
    def active_cutouts(self) -> int:
        return len(self._cutouts)

    def serialize_config(self) -> dict:
        return dict(self._config)

    def reset(self) -> None:
        self._queue.clear()
        self._cutouts.clear()
        self._next_index = None
        self._last_timestamp = None
        self._scene_sample = None
        self._spawns = 0
        self._total_pushed = 0

    # --- numpy helpers --------------------------------------------------

    def _gray(self, rgb):
        return (rgb[..., 0].astype(self._np.float32) * 0.299
                + rgb[..., 1].astype(self._np.float32) * 0.587
                + rgb[..., 2].astype(self._np.float32) * 0.114)

    def _resize(self, img, shape):
        """Nearest-neighbour resize of a (H,W[,C]) array to (h, w)."""
        np = self._np
        src_h, src_w = img.shape[:2]
        h, w = shape
        if (h, w) == (src_h, src_w):
            return img
        ys = (np.arange(h) * (src_h - 1) / max(1, h - 1)).round().astype(np.int64)
        xs = (np.arange(w) * (src_w - 1) / max(1, w - 1)).round().astype(np.int64)
        return img[ys[:, None], xs[None, :]]

    def _try_cv2(self):
        try:
            import cv2
            return cv2
        except (ImportError, ModuleNotFoundError, TypeError):
            return None

    # --- flow -----------------------------------------------------------

    def _flow_cv2(self, cv2, gray0, gray1):
        try:
            dis = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_MEDIUM)
            return dis.calc(gray0, gray1, None)
        except Exception:
            return cv2.calcOpticalFlowFarneback(
                gray0, gray1, None, 0.5, 3, 21, 5, 7, 1.5, 0)

    def _flow_numpy(self, gray0, gray1):
        np = self._np
        h, w = gray0.shape
        flow = np.zeros((h, w, 2), dtype=np.float32)
        g0 = gray0.astype(np.float32)
        g1 = gray1.astype(np.float32)
        gh, gw = h // _BLOCK, w // _BLOCK
        for by in range(gh):
            for bx in range(gw):
                y0, x0 = by * _BLOCK, bx * _BLOCK
                ref = g0[y0:y0 + _BLOCK, x0:x0 + _BLOCK]
                best, best_sad = (0, 0), float("inf")
                for dy in range(-_SEARCH, _SEARCH + 1):
                    for dx in range(-_SEARCH, _SEARCH + 1):
                        sy, sx = y0 + dy, x0 + dx
                        if (sy < 0 or sx < 0
                                or sy + _BLOCK > h or sx + _BLOCK > w):
                            continue
                        sad = float(np.abs(
                            ref - g1[sy:sy + _BLOCK, sx:sx + _BLOCK]).mean())
                        if sad < best_sad:
                            best_sad, best = sad, (dx, dy)
                flow[y0:y0 + _BLOCK, x0:x0 + _BLOCK] = best
        return flow

    def _flow(self, gray0, gray1):
        cv2 = self._try_cv2()
        if cv2 is not None:
            try:
                return self._flow_cv2(cv2, gray0, gray1)
            except Exception:
                pass
        return self._flow_numpy(gray0, gray1)

    # --- tile scoring ---------------------------------------------------

    @staticmethod
    def _tile_desc(gray, y, x, t):
        import numpy as _np
        patch = _np.asarray(gray[y:y + t, x:x + t], dtype=_np.float32)
        patch = FutureCut._resize_2d(_np, patch, (_DESC_SIDE, _DESC_SIDE))
        patch = patch - patch.mean()
        norm = float(_np.sqrt((patch * patch).sum()))
        if norm > 1e-6:
            patch = patch / norm
        return patch.reshape(-1)

    @staticmethod
    def _resize_2d(np, img, shape):
        src_h, src_w = img.shape[:2]
        h, w = shape
        ys = (np.arange(h) * (src_h - 1) / max(1, h - 1)).round().astype(np.int64)
        xs = (np.arange(w) * (src_w - 1) / max(1, w - 1)).round().astype(np.int64)
        return img[ys[:, None], xs[None, :]]

    def _choose_seed(self, pa, fa, flow):
        """Score overlapping tiles; return the chosen tile (y, x) or None."""
        np = self._np
        ah, aw = pa.shape
        short = min(ah, aw)
        tile = max(4, min(short, short // _TILE_DIV))
        if short < tile or ah < tile or aw < tile:
            return None
        step = max(1, tile // 2)

        gy, gx = np.gradient(pa)
        grad_mag = np.sqrt(gy * gy + gx * gx)
        flow_mag = np.sqrt(flow[..., 0] ** 2 + flow[..., 1] ** 2)

        min_motion = self._config["min_motion"] / 100.0 * aw
        max_motion = self._config["max_motion"] / 100.0 * aw
        sim_threshold = self._config["similarity"] / 100.0
        pick = self._config["pick"]

        records = []
        for y in range(0, ah - tile + 1, step):
            for x in range(0, aw - tile + 1, step):
                energy = float(grad_mag[y:y + tile, x:x + tile].mean())
                motion = float(flow_mag[y:y + tile, x:x + tile].mean())
                if energy < 0.5:          # reject flat tiles
                    continue
                dp = self._tile_desc(pa, y, x, tile)
                df = self._tile_desc(fa, y, x, tile)
                dist = float(np.sqrt(((dp - df) ** 2).sum()))  # in [0, 2]
                similarity = 1.0 - dist / 2.0
                spot = float(np.abs(pa[y:y + tile, x:x + tile]
                                    - fa[y:y + tile, x:x + tile]).mean()) / 255.0
                records.append((y, x, energy, motion, similarity, dist, spot))

        if not records:
            return None

        def motion_ok(m):
            return min_motion <= m <= max_motion

        motion_gated = [r for r in records if motion_ok(r[3])]
        # A cutout is only worth compositing when the future actually differs
        # from the present at that spot; identical-looking tiles make the
        # borrow invisible (the old archaeology failure mode).
        floor = max(2.0 / 255.0, (1.0 - sim_threshold) * 0.08)
        eligible = [r for r in motion_gated
                    if r[4] >= sim_threshold and r[6] >= floor]
        if not eligible:
            eligible = [r for r in motion_gated if r[6] > 0] or motion_gated
        pool = eligible or records  # final fallback: any textured tile

        if pick == "random":
            rng = np.random.default_rng(self._seed * 1000003)
            y, x = pool[int(rng.integers(0, len(pool)))]
            return (y, x)
        if pick == "dissimilar":
            # largest descriptor distance under a cap of 1.0 (normalized)
            capped = [r for r in pool if r[5] <= 1.0] or pool
            y, x, *_ = max(capped, key=lambda r: r[5])
            return (y, x)
        # coherent: same surface class, but visibly displaced ahead - prefer the
        # strongest visible difference so the slab reads as borrowed content
        span = max(1e-3, (max_motion - min_motion))
        y, x, *_ = max(
            pool, key=lambda r: 0.75 * min(1.0, r[6] / 0.25)
            + 0.25 * min(1.0, (r[3] - min_motion) / span)
        )
        return (y, x)

    def _grow_mask(self, pa, seed, target_area):
        """Region-grow from the seed tile into ONE blob; bool mask at analysis
        scale."""
        np = self._np
        ah, aw = pa.shape
        short = min(ah, aw)
        tile = max(4, min(short, short // _TILE_DIV))
        step = max(1, tile // 2)
        seed_desc = self._tile_desc(pa, seed[0], seed[1], tile)

        coverage = np.zeros((ah, aw), dtype=bool)
        visited = {seed}
        # Grow the seed into one blob, relaxing the descriptor budget in
        # passes so a patchy surface still reaches the requested area.
        for budget in (_GROW_DIST, _GROW_DIST * 1.7, _GROW_DIST * 3.0):
            seeds = [(y, x) for y in range(0, ah - tile + 1, step)
                     for x in range(0, aw - tile + 1, step)
                     if coverage[y:y + tile, x:x + tile].any()] or [seed]
            queue = deque(seeds)
            while queue and int(coverage.sum()) < target_area:
                y, x = queue.popleft()
                if int(coverage.sum()) + tile * tile > target_area * 1.1:
                    break  # stop before the blob overshoots the setting
                coverage[y:y + tile, x:x + tile] = True
                for dy in (-step, 0, step):
                    for dx in (-step, 0, step):
                        if dy == 0 and dx == 0:
                            continue
                        ny, nx = y + dy, x + dx
                        if not (0 <= ny <= ah - tile and 0 <= nx <= aw - tile):
                            continue
                        if (ny, nx) in visited:
                            continue
                        visited.add((ny, nx))
                        d = float(np.sqrt(
                            ((self._tile_desc(pa, ny, nx, tile) - seed_desc)
                             ** 2).sum()))
                        if d <= budget:
                            queue.append((ny, nx))
            if int(coverage.sum()) >= target_area:
                break

        # Still short (very small frame, tight mask): dilate until the area
        # matches the setting, keeping the organic grown shape.
        cv2 = self._try_cv2()
        guard = 0
        while int(coverage.sum()) < target_area and guard < 64:
            guard += 1
            if cv2 is not None:
                grown = cv2.dilate(coverage.astype(np.uint8), np.ones((3, 3), np.uint8))
            else:
                padded = np.pad(coverage, 1, mode="constant", constant_values=False)
                grown = (padded[1:-1, 1:-1] | padded[:-2, 1:-1] | padded[2:, 1:-1]
                         | padded[1:-1, :-2] | padded[1:-1, 2:])
            if not grown.any() or np.array_equal(grown, coverage):
                break
            coverage = grown.astype(bool)
            if int(coverage.sum()) > target_area * 1.1:
                break

        if cv2 is not None:
            kernel = np.ones((5, 5), np.uint8)
            cleaned = cv2.morphologyEx(coverage.astype(np.uint8), cv2.MORPH_CLOSE, kernel)
            cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel)
            if cleaned.any():
                coverage = cleaned.astype(bool)
        return coverage

    def _feather(self, mask):
        np = self._np
        feather = self._config["feather"]
        if feather <= 0:
            return mask.astype(np.float32)
        # Scale the softness to the blob, not the frame: a frame-sized blur
        # turns an 18% cutout into a soft rectangle covering half the picture.
        rows = np.where(mask.max(axis=1) > 0.01)[0]
        cols = np.where(mask.max(axis=0) > 0.01)[0]
        if rows.size == 0 or cols.size == 0:
            return mask.astype(np.float32)
        blob = max(3, min(int(rows.size), int(cols.size)))
        radius = max(1, int(round(feather / 100.0 * 0.5 * blob)))
        cv2 = self._try_cv2()
        if cv2 is not None:
            k = max(1, radius | 1)
            return cv2.GaussianBlur(mask.astype(np.float32), (k, k), 0)
        blurred = mask.astype(np.float32)
        kernel = np.ones((3, 3), np.float32) / 9.0
        for _ in range(radius):
            padded = np.pad(blurred, 1, mode="edge")
            blurred = (padded[:-2, :-2] * kernel[0, 0] + padded[:-2, 1:-1] * kernel[0, 1]
                       + padded[:-2, 2:] * kernel[0, 2] + padded[1:-1, :-2] * kernel[1, 0]
                       + padded[1:-1, 1:-1] * kernel[1, 1] + padded[1:-1, 2:] * kernel[1, 2]
                       + padded[2:, :-2] * kernel[2, 0] + padded[2:, 1:-1] * kernel[2, 1]
                       + padded[2:, 2:] * kernel[2, 2])
        return blurred

    # --- spawn / composite ---------------------------------------------

    def _donor_index(self, birth):
        newest = self._queue[-1].index if self._queue else birth
        return min(birth + self._effective_gap, newest)

    def _noise_field(self, birth):
        np = self._np
        rng = np.random.default_rng(self._seed * 1000003 + birth)
        h, w = self._shape[:2]
        # Low-frequency noise: a per-pixel field dissolves the cutout into salt
        # and pepper dust, which reads as compression garbage instead of a
        # melt. Coarse noise breaks it into blotches.
        small = rng.integers(0, 256, (max(2, h // 16), max(2, w // 16)),
                             dtype=np.uint8)
        coarse = self._resize(small.astype(np.float32), (h, w))
        cv2 = self._try_cv2()
        if cv2 is not None:
            sigma = max(1.0, min(h, w) * 0.02)
            coarse = cv2.GaussianBlur(coarse, (0, 0), sigma)
        else:
            for _ in range(3):
                padded = np.pad(coarse, 1, mode="edge")
                coarse = (padded[:-2, :-2] + padded[:-2, 1:-1] + padded[:-2, 2:]
                          + padded[1:-1, :-2] + padded[1:-1, 1:-1] + padded[1:-1, 2:]
                          + padded[2:, :-2] + padded[2:, 1:-1] + padded[2:, 2:]) / 9.0
        span = float(coarse.max() - coarse.min()) or 1.0
        return np.clip((coarse - coarse.min()) / span * 255.0, 0, 255).astype(np.uint8)

    def _spawn(self, packet):
        np = self._np
        birth = packet.index
        newest = self._queue[-1].index if self._queue else birth
        target = birth + self._effective_gap
        if target > newest or newest <= birth:
            return  # no real future donor at the gap distance
        donor_index = min(target, newest)
        # locate the donor packet by contiguous index offset
        front = self._queue[0].index
        donor = self._queue[donor_index - front].frame

        h, w = self._shape[:2]
        pa = self._resize(self._gray(packet.frame), self._analysis_shape)
        fa = self._resize(self._gray(donor), self._analysis_shape)
        flow = self._flow(pa, fa)

        seed = self._choose_seed(pa, fa, flow)
        if seed is None:
            return
        ah, aw = self._analysis_shape
        target_area = int(self._config["size"] / 100.0 * ah * aw)

        if self._config["shape"] == "rect":
            mask_a = np.zeros((ah, aw), dtype=bool)
            side = max(1, int(round(target_area ** 0.5)))
            cy = min(ah - 1, seed[0] + (ah // 6))
            cx = min(aw - 1, seed[1] + (aw // 6))
            y0, x0 = max(0, cy - side // 2), max(0, cx - side // 2)
            mask_a[y0:min(ah, y0 + side), x0:min(aw, x0 + side)] = True
        else:
            mask_a = self._grow_mask(pa, seed, target_area)

        if not mask_a.any():
            return
        mask_full = self._resize(mask_a.astype(np.float32), (h, w))
        mask_full = self._feather(mask_full)

        # region flow at analysis scale, scaled to full resolution
        region = mask_a
        fx = float(flow[..., 0][region].mean())
        fy = float(flow[..., 1][region].mean())
        sx = w / max(1, aw)
        sy = h / max(1, ah)

        center = np.argwhere(mask_full > 0.5)
        cy_full, cx_full = (int(center[:, 0].mean()), int(center[:, 1].mean()))

        cut = {
            "birth": birth,
            "donor": donor.copy(),
            # uint8 mask/accumulator keep a live cutout near three frames of
            # memory instead of seven; the blend converts per frame anyway
            "mask": np.clip(mask_full * 255.0, 0, 255).round().astype(np.uint8),
            "acc": donor.copy() if self._config["mode"] == "smudge" else None,
            "flow": (fx * sx, fy * sy),
            "noise": self._noise_field(birth),
            "center": (cx_full, cy_full),
        }
        self._cutouts.append(cut)
        self._spawns += 1

    def _composite(self, packet):
        np = self._np
        out = packet.frame  # buffer already owns a copy; never mutate input
        hold = self._config["hold"]
        melt = self._config["melt"]
        mode = self._config["mode"]
        drift = self._config["drift"] / 100.0
        retention = self._config["retention"] / 100.0

        alive = []
        for cut in self._cutouts:
            age = packet.index - cut["birth"]
            if age < 0 or age >= hold + melt:
                continue
            alive.append(cut)

            # life mask: full during hold, blotchy dissolve during melt
            if age < hold:
                life = np.float32(1.0)
                fade = 1.0
            else:
                progress = (age - hold + 1) / melt
                life = (cut["noise"] > int(progress * 255)).astype(np.float32)
                # ease the opacity down as the blotches open up, otherwise the
                # tail of the melt stays fully opaque and eats the live frame
                fade = float(max(0.0, 1.0 - progress) ** 0.6)

            mask = cut["mask"].astype(np.float32) / 255.0
            alpha = mask * life * fade
            if alpha.max() <= 0:
                continue

            if mode == "smudge":
                fx, fy = cut["flow"]
                acc = cut["acc"].astype(np.float32)
                warped = np.roll(np.roll(acc, int(round(fy)), axis=0),
                                 int(round(fx)), axis=1)
                acc = warped * retention + cut["donor"].astype(np.float32) * (1.0 - retention)
                cut["acc"] = np.clip(acc, 0, 255).round().astype(np.uint8)
                src = acc
            else:
                dx = int(round(cut["flow"][0] * drift))
                dy = int(round(cut["flow"][1] * drift))
                src = np.roll(np.roll(cut["donor"], dy, axis=0), dx, axis=1)

            # Poisson seam (NORMAL_CLONE) during the sharp hold phase, else
            # masked alpha blend. The dissolving mask is recomputed per frame
            # so seamlessClone never receives a stale out-of-mask center.
            cv2 = self._try_cv2()
            mask_bin = (alpha > 0.5).astype(np.uint8) * 255
            if (self._config["seam"] == "poisson" and cv2 is not None
                    and age < hold and mask_bin.any()):
                ys, xs = np.nonzero(mask_bin)
                center = (int(round(xs.mean())), int(round(ys.mean())))
                try:
                    src8 = (np.ascontiguousarray(src)
                            if src.dtype == np.uint8
                            else np.ascontiguousarray(
                                np.clip(src, 0, 255).astype(np.uint8)))
                    out = cv2.seamlessClone(
                        src8, np.ascontiguousarray(out),
                        np.ascontiguousarray(mask_bin), center,
                        cv2.NORMAL_CLONE)
                    continue
                except Exception:
                    pass  # fall through to alpha blend on any clone failure
            a = alpha[..., None]
            out = (out.astype(np.float32) * (1.0 - a)
                   + src.astype(np.float32) * a)
            out = np.clip(out, 0, 255).round().astype(np.uint8)
        self._cutouts = alive
        return np.ascontiguousarray(out)

    # --- push / flush ---------------------------------------------------

    def _emit_front(self):
        packet = self._queue.popleft()
        if (len(self._cutouts) < self._config["cutouts"]
                and packet.index % self._config["spawn_every"]
                == self._seed % self._config["spawn_every"]):
            self._spawn(packet)
        frame = self._composite(packet)
        yield replace(packet, frame=frame)

    def _drain(self):
        while self._queue:
            yield from self._emit_front()
        self._cutouts.clear()
        self._scene_sample = None

    def push(self, packet):
        if self._np is None:
            raise RuntimeError("initialize FutureCut before buffering")
        np = self._np
        if (not isinstance(packet.frame, np.ndarray)
                or packet.frame.shape != self._shape
                or packet.frame.dtype != np.uint8):
            raise ValueError(f"expected uint8 RGB frame {self._shape}")
        stamp = float(packet.timestamp)
        if not math.isfinite(stamp):
            raise ValueError("future-cut needs finite presentation timestamps")

        # reset on index discontinuity or timestamp rewind
        if ((self._next_index is not None and packet.index != self._next_index)
                or (self._last_timestamp is not None
                    and stamp < self._last_timestamp)):
            yield from self._drain()
            self._next_index = None
            self._last_timestamp = None

        # scene-cut detection on the analysis frame
        sample = self._resize(
            packet.frame, (min(self._SCENE_SIDE, self._shape[0]),
                           min(self._SCENE_SIDE, self._shape[1]))).astype(np.float32)
        cut = (self._scene_sample is not None and
               float(np.abs(sample - self._scene_sample).mean())
               >= self._SCENE_CUT * 255)
        if cut or packet.context.get("scene_cut"):
            yield from self._drain()
            self._next_index = None
            self._last_timestamp = None

        self._scene_sample = sample
        self._next_index = packet.index + 1
        self._last_timestamp = stamp
        self._queue.append(replace(packet, frame=packet.frame.copy()))
        self._total_pushed += 1

        while (self._queue
               and self._queue[-1].index - self._queue[0].index >= self._delay):
            yield from self._emit_front()

    def flush(self):
        """EOF: drain remaining packets, borrowing the latest available future."""
        if self._total_pushed <= self._effective_gap:
            self.runtime_notes.append(
                f"clip ({self._total_pushed} frames) is shorter than the "
                f"look-ahead+gap horizon ({self._lookahead_frames}"
                f"+{self._config['gap']} frames); passing through unchanged")
        elif self._total_pushed < self._lookahead_frames + self._config["gap"]:
            self.runtime_notes.append(
                f"clip ({self._total_pushed} frames) shorter than "
                f"look-ahead+gap; donor gap clamped")
        if self._try_cv2() is None:
            self.runtime_notes.append(
                "flow is approximate (numpy block matching; install "
                "requirements-collapse.txt for OpenCV DIS optical flow)")
        yield from self._drain()
