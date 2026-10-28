"""Bounded, stateful frame processors used by effex's raw-frame runner."""
import math
from collections import deque
from dataclasses import replace
from typing import Any, Optional


class TemporalSediment:
    """A small stack of decaying frame memories with seeded excavation patches."""

    MAX_STATE_BYTES: int = 128 * 1024 * 1024
    _LAYER_FACTORS = (0.25, 1.0, 3.0)  # long, medium, fast memory
    _LAYER_WEIGHTS = (0.5, 0.3, 0.2)

    def __init__(self):
        self._np: Any = None
        self._config = {}
        self._shape = (0, 0, 0)
        self._fps = 30.0
        self._seed = 0
        self._layers: Any = None
        self._previous_gray: Any = None
        self._previous_rgb: Any = None
        self._rng: Any = None
        self._next_index = 0
        self._previous_timestamp: Optional[float] = None
        self._work_shape = (0, 0)
        self._source_y: Any = None
        self._source_x: Any = None
        self._output_y: Any = None
        self._output_x: Any = None

    @staticmethod
    def _percent(config: dict, key: str, default: int) -> int:
        try:
            return max(0, min(100, int(config.get(key, default))))
        except (TypeError, ValueError):
            return default

    def initialize(self, config: dict, frame_shape: tuple, fps: float,
                   seed: int) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError(
                "Temporal Sediment needs NumPy; install "
                "requirements-stateful.txt"
            ) from exc

        if len(frame_shape) != 3 or int(frame_shape[2]) != 3:
            raise ValueError("Temporal Sediment expects an HxWx3 RGB frame")
        height, width, channels = (int(v) for v in frame_shape)
        if height < 1 or width < 1 or channels != 3:
            raise ValueError("Temporal Sediment received invalid frame dimensions")
        try:
            fps_value = float(fps)
        except (TypeError, ValueError):
            fps_value = 30.0
        if not math.isfinite(fps_value) or fps_value <= 0:
            fps_value = 30.0

        self._np = np
        self._config = dict(config)
        self._shape = (height, width, channels)
        self._fps = fps_value
        try:
            self._seed = int(seed) & 0x7fffffff
        except (TypeError, ValueError):
            self._seed = 0

        try:
            layers = int(str(config.get("layers", "3")))
        except (TypeError, ValueError):
            layers = 3
        layers = max(1, min(3, layers))
        bytes_per_pixel = (layers * 3 * np.dtype(np.float16).itemsize
                          + 4 * np.dtype(np.float16).itemsize)
        max_pixels = max(1, self.MAX_STATE_BYTES // bytes_per_pixel)
        scale = min(1.0, math.sqrt(max_pixels / (height * width)))
        work_height = max(1, int(height * scale))
        work_width = max(1, int(width * scale))
        while work_height * work_width * bytes_per_pixel > self.MAX_STATE_BYTES:
            if work_width >= work_height and work_width > 1:
                work_width -= 1
            elif work_height > 1:
                work_height -= 1
            else:
                break
        self._work_shape = (work_height, work_width)
        self._source_y = (np.arange(work_height, dtype=np.int32) * height
                          // work_height)
        self._source_x = (np.arange(work_width, dtype=np.int32) * width
                          // work_width)
        self._output_y = (np.arange(height, dtype=np.int32) * work_height
                          // height)
        self._output_x = (np.arange(width, dtype=np.int32) * work_width
                          // width)
        self._layers = np.zeros(
            (layers, work_height, work_width, 3), dtype=np.float16)
        self._previous_gray = None
        self._previous_rgb = None
        self._rng = np.random.default_rng(self._seed)
        self._next_index = 0
        self._previous_timestamp = None

    @property
    def work_shape(self) -> tuple:
        return self._work_shape

    @property
    def state_nbytes(self) -> int:
        total = int(self._layers.nbytes) if self._layers is not None else 0
        if self._previous_gray is not None:
            total += int(self._previous_gray.nbytes)
        if self._previous_rgb is not None:
            total += int(self._previous_rgb.nbytes)
        return total

    def serialize_config(self) -> dict:
        return dict(self._config)

    def reset(self) -> None:
        if self._np is None or self._layers is None:
            return
        self._layers.fill(0)
        self._previous_gray = None
        self._previous_rgb = None
        self._rng = self._np.random.default_rng(self._seed)
        self._next_index = 0
        self._previous_timestamp = None

    def _sample(self, frame):
        np = self._np
        if not isinstance(frame, np.ndarray):
            frame = np.asarray(frame)
        if frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(
                f"expected uint8 RGB frame {self._shape}, got "
                f"{frame.dtype} {frame.shape}"
            )
        sampled = frame[self._source_y[:, None], self._source_x[None, :], :]
        return sampled.astype(np.float32) / 255.0

    def _first_frame(self, frame, current, frame_index, timestamp):
        np = self._np
        self._layers[:] = current.astype(np.float16)
        gray = (current[:, :, 0] * 0.299 + current[:, :, 1] * 0.587
                + current[:, :, 2] * 0.114)
        self._previous_gray = gray.astype(np.float16)
        self._previous_rgb = current.astype(np.float16)
        self._next_index = frame_index + 1
        try:
            self._previous_timestamp = (float(timestamp)
                                        if timestamp is not None else None)
        except (TypeError, ValueError):
            self._previous_timestamp = None
        return frame.copy()

    def _history_image(self):
        np = self._np
        count = self._layers.shape[0]
        if count == 1:
            weights = (1.0,)
        elif count == 2:
            weights = (0.7, 0.3)
        else:
            weights = self._LAYER_WEIGHTS
        history = np.zeros(self._work_shape + (3,), dtype=np.float32)
        for index, weight in enumerate(weights):
            history += self._layers[index].astype(np.float32) * weight
        return np.clip(history, 0.0, 1.0)

    def _apply_color_memory(self, history):
        np = self._np
        color_mode = str(self._config.get("color", "keep"))
        if color_mode == "warm":
            history *= np.array((1.0, 0.82, 0.62), dtype=np.float32)
        elif color_mode == "cool":
            history *= np.array((0.62, 0.82, 1.0), dtype=np.float32)
        elif color_mode == "mono":
            gray = (history[:, :, 0] * 0.299 + history[:, :, 1] * 0.587
                    + history[:, :, 2] * 0.114)
            history[:] = gray[:, :, None]
        return np.clip(history, 0.0, 1.0)

    def _excavate(self, history) -> None:
        np = self._np
        chance = self._percent(self._config, "excavation", 5) / 100.0
        if chance <= 0 or self._rng.random() >= chance:
            return
        height, width = self._work_shape
        size = self._percent(self._config, "excavation_size", 15) / 100.0
        patch_h = max(1, min(height, int(round(height * size))))
        patch_w = max(1, min(width, int(round(width * size))))
        y0 = int(self._rng.integers(0, max(1, height - patch_h + 1)))
        x0 = int(self._rng.integers(0, max(1, width - patch_w + 1)))
        region = (slice(y0, y0 + patch_h), slice(x0, x0 + patch_w))
        history[region] = self._layers[0][region].astype(np.float32)
        if self._layers.shape[0] > 1:
            self._layers[-1][region] = self._layers[0][region]

    def process(self, frame, frame_index: int, timestamp: Optional[float],
                context: Optional[dict] = None):
        if self._np is None or self._layers is None:
            raise RuntimeError("initialize Temporal Sediment before processing")
        np = self._np
        if frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(
                f"expected uint8 RGB frame {self._shape}, got "
                f"{frame.dtype} {frame.shape}"
            )
        if context is None:
            context = {}
        current = self._sample(frame)
        index = int(frame_index)
        if index != self._next_index or context.get("scene_cut"):
            self.reset()
        if self._previous_gray is None:
            return self._first_frame(frame, current, index, timestamp)

        gray = (current[:, :, 0] * 0.299 + current[:, :, 1] * 0.587
                + current[:, :, 2] * 0.114)
        previous_gray = self._previous_gray.astype(np.float32)
        previous_rgb = self._previous_rgb.astype(np.float32)
        if float(np.mean(np.abs(current - previous_rgb))) >= 0.55:
            self.reset()
            return self._first_frame(frame, current, index, timestamp)

        try:
            current_timestamp = (float(timestamp)
                                if timestamp is not None else None)
        except (TypeError, ValueError):
            current_timestamp = None
        if (current_timestamp is not None
                and self._previous_timestamp is not None
                and current_timestamp < self._previous_timestamp):
            self.reset()
            return self._first_frame(frame, current, index, current_timestamp)
        if current_timestamp is not None and self._previous_timestamp is not None:
            elapsed = current_timestamp - self._previous_timestamp
        else:
            elapsed = 1.0 / self._fps

        motion = np.abs(gray - previous_gray)
        sensitivity = self._percent(self._config, "motion", 50) / 100.0
        threshold = 0.15 - 0.12 * sensitivity
        motion_mask = np.clip((motion - threshold) / (0.35 - threshold),
                              0.0, 1.0)
        deposit_mask = 0.25 + 0.75 * motion_mask
        deposit = self._percent(self._config, "deposit", 70) / 100.0
        decay = self._percent(self._config, "decay", 35) / 100.0
        rate = 4.0 * decay
        factors = self._LAYER_FACTORS[:self._layers.shape[0]]
        for layer_index, factor in enumerate(factors):
            retention = math.exp(-rate * elapsed * factor)
            old = self._layers[layer_index].astype(np.float32)
            updated = (old * retention
                       + current * (deposit_mask[:, :, None]
                                    * deposit * (1.0 - retention)))
            self._layers[layer_index] = np.clip(updated, 0.0, 1.0).astype(
                np.float16)

        history = self._history_image()
        self._excavate(history)
        history = self._apply_color_memory(history)
        intensity = self._percent(self._config, "intensity", 65) / 100.0
        self._previous_gray = gray.astype(np.float16)
        self._previous_rgb = current.astype(np.float16)
        self._next_index = index + 1
        self._previous_timestamp = current_timestamp
        if intensity == 0.0:
            return frame.copy()

        if self._work_shape == self._shape[:2]:
            full_history = history
        else:
            full_history = history[self._output_y[:, None],
                                   self._output_x[None, :], :]
        original = frame.astype(np.float32) / 255.0
        result = original * (1.0 - intensity) + full_history * intensity
        return np.ascontiguousarray(np.clip(result * 255.0, 0.0, 255.0)
                                    .round().astype(np.uint8))


class ElasticCollapse:
    """Progressive content-aware scaling with pixel-mosh texture.

    Seam mode linearly scales a reduced-resolution carved field over clip time;
    elastic mode evolves a seeded nonuniform field. A bounded pixel grid adds
    blocky output, with limited previous-frame carry in moving areas. OpenCV is
    loaded only on initialization; full-size maps are transient.
    """

    MAX_WORK_SIDE = 192
    MAX_STATE_BYTES = 4 * 1024 * 1024
    DEFAULTS = {
        "intensity": 100, "horizontal": 100, "vertical": 100, "preserve": 75,
        "mode": "seam", "seam_strength": 100, "motion": 50,
        "smoothness": 70, "speed": 0.35, "coherence": 80, "seed": 0,
        "pixelate": False, "block_size": 4, "mosh": 60,
    }

    def __init__(self):
        self._np: Any = None
        self._cv: Any = None
        self._config = {}
        self._shape = (0, 0, 0)
        self._work_shape = (0, 0)
        self._fps = 30.0
        self._clip_duration: Optional[float] = None
        self._progress = 1.0
        self._seed = 0
        self._x: Any = None
        self._y: Any = None
        self._phase: Any = None
        self._field: Any = None
        self._previous_rgb: Any = None
        self._previous_mosh_grid: Any = None
        self._previous_timestamp: Optional[float] = None
        self._next_index: Optional[int] = None
        self._clock = 0.0
        self._seam_target: Any = None
        self._next_seam_time = 0.0
        self.seam_updates = 0

    @staticmethod
    def _number(value, default, low, high):
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            return default
        return max(low, min(high, number)) if math.isfinite(number) else default

    @staticmethod
    def _flag(value, default: bool) -> bool:
        """Accept bools, 0/1 and on/off-style strings from the CLI and menu."""
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value).strip().lower()
        if text in ("on", "yes", "true", "1", "y"):
            return True
        if text in ("off", "no", "false", "0", "n"):
            return False
        return default

    def initialize(self, config: dict, frame_shape: tuple, fps: float,
                   seed: int) -> None:
        try:
            import numpy as np
            import cv2
        except ImportError as exc:
            raise RuntimeError(
                "Elastic Collapse needs NumPy and headless OpenCV; install "
                "requirements-collapse.txt"
            ) from exc
        if (len(frame_shape) != 3 or frame_shape[2] != 3
                or any(int(v) < 1 for v in frame_shape)):
            raise ValueError("Elastic Collapse expects an HxWx3 RGB frame")
        self._np, self._cv = np, cv2
        self._shape = tuple(int(v) for v in frame_shape)
        self._config = {}
        for key, default in self.DEFAULTS.items():
            if key == "mode":
                mode = str(config.get(key, default))
                if mode not in ("elastic", "seam"):
                    raise ValueError("collapse mode must be elastic or seam")
                self._config[key] = mode
            elif key == "pixelate":
                self._config[key] = self._flag(config.get(key, default), default)
            else:
                high = 4.0 if key == "speed" else (2147483647 if key == "seed" else 100)
                if key == "block_size":
                    high = 16
                number = self._number(config.get(key, default), default, 0, high)
                self._config[key] = number if key == "speed" else int(number)
        self._fps = self._number(fps, 30.0, 0.001, 1000)
        duration = self._number(config.get("_clip_duration"), None, 0, 1e12)
        self._clip_duration = duration if duration and duration > 0 else None
        self._seed = int(self._number(seed, 0, 0, 2147483647))
        height, width = self._shape[:2]
        scale = min(1.0, self.MAX_WORK_SIDE / max(height, width))
        self._work_shape = (max(1, round(height * scale)),
                            max(1, round(width * scale)))
        self._y, self._x = np.meshgrid(
            np.linspace(0, 1, self._work_shape[0], dtype=np.float32),
            np.linspace(0, 1, self._work_shape[1], dtype=np.float32), indexing="ij")
        self.reset()

    @property
    def work_shape(self) -> tuple:
        return self._work_shape

    @property
    def state_nbytes(self) -> int:
        return sum(int(value.nbytes) for value in
                   (self._x, self._y, self._phase, self._field, self._previous_rgb,
                    self._seam_target, self._previous_mosh_grid)
                   if value is not None)

    @property
    def displacement(self) -> Any:
        """A copy of the applied normalized (dx, dy) field for diagnostics."""
        if self._field is None:
            return None
        amount = (-self._progress if self._config["mode"] == "seam"
                  else self._config["intensity"] / 100.0)
        return self._field * amount

    def serialize_config(self) -> dict:
        return dict(self._config)

    def reset(self) -> None:
        if self._np is None:
            return
        self._phase = self._np.random.default_rng(self._seed).uniform(0, 2 * math.pi, 4)
        self._field = None
        self._previous_rgb = None
        self._previous_mosh_grid = None
        self._previous_timestamp = None
        self._next_index = None
        self._clock = 0.0
        self._progress = 1.0
        self._seam_target = None
        self._next_seam_time = 0.0
        self.seam_updates = 0

    def _seam_field(self, current):
        from seam_carving import build_seam_field

        # Update the target at most 5Hz; frame rendering scales it by clip progress.
        if self._seam_target is None or self._clock + 1e-9 >= self._next_seam_time:
            target = build_seam_field(
                current, self._config["horizontal"] / 100.0,
                self._config["vertical"] / 100.0,
                (self._config["intensity"] * self._config["seam_strength"]
                 / 10000.0),
                self._config["preserve"] / 100.0,
                max_seams=None, max_shrink=0.5,
            )
            self._seam_target = self._cv.GaussianBlur(
                target, (0, 0), 0.5 + 3 * self._config["smoothness"] / 100.0)
            self._next_seam_time = self._clock + 0.2
            self.seam_updates += 1
        return self._seam_target

    def _elastic_field(self, current):
        np, cv = self._np, self._cv
        x, y, phase = self._x, self._y, self._phase
        smooth = self._config["smoothness"] / 100.0
        frequency = 1.0 + (1.0 - smooth) * 4.0
        t = self._clock * self._config["speed"] * 2 * math.pi
        dx = (np.sin(2 * math.pi * frequency * y + phase[0] + t)
              + 0.5 * np.sin(2 * math.pi * (x + y) + phase[1] - t * 0.7))
        dy = (np.sin(2 * math.pi * frequency * x + phase[2] - t)
              + 0.5 * np.sin(2 * math.pi * (x - y) + phase[3] + t * 0.7))
        field = np.stack((dx * self._config["horizontal"] / 100.0,
                          dy * self._config["vertical"] / 100.0), axis=-1)
        # Base field is at most 18% per axis; motion below can amplify it 2x.
        field *= (0.12 * np.sin(math.pi * x) * np.sin(math.pi * y))[:, :, None]
        field = cv.GaussianBlur(field, (0, 0), 0.5 + 4.0 * smooth)
        gray = cv.cvtColor(current, cv.COLOR_RGB2GRAY).astype(np.float32)
        gx = cv.Sobel(gray, cv.CV_32F, 1, 0, ksize=3, scale=0.25)
        gy = cv.Sobel(gray, cv.CV_32F, 0, 1, ksize=3, scale=0.25)
        energy = cv.GaussianBlur(np.minimum(cv.magnitude(gx, gy) / 64.0, 1),
                                 (0, 0), 1.2)
        field *= (1 - 0.95 * self._config["preserve"] / 100.0 * energy)[:, :, None]
        if self._previous_rgb is not None and self._config["motion"]:
            difference = np.abs(current.astype(np.float32)
                                - self._previous_rgb.astype(np.float32)).mean(axis=2)
            moving = cv.GaussianBlur(np.minimum(difference / 64.0, 1), (0, 0), 2.0)
            field *= (1 + self._config["motion"] / 100.0 * moving)[:, :, None]
        return field.astype(np.float32)

    def _pixel_mosh(self, frame, motion_delta):
        """Pixelate on the bounded work grid and carry old blocks in motion areas."""
        np, cv = self._np, self._cv
        block_size = self._config["block_size"]
        mosh = self._config["mosh"] / 100.0
        if block_size == 1 and mosh == 0:
            self._previous_mosh_grid = None
            return frame

        work_height, work_width = self._work_shape
        height, width = frame.shape[:2]
        reduced = cv.resize(frame, (work_width, work_height),
                            interpolation=cv.INTER_AREA)
        step = max(1, block_size)
        grid_width = max(1, (work_width + step - 1) // step)
        grid_height = max(1, (work_height + step - 1) // step)
        current_grid = cv.resize(reduced, (grid_width, grid_height),
                                 interpolation=cv.INTER_AREA)
        styled = current_grid.astype(np.float32)
        if (mosh and motion_delta is not None
                and self._previous_mosh_grid is not None):
            motion_grid = cv.resize(motion_delta.astype(np.float32),
                                    (grid_width, grid_height),
                                    interpolation=cv.INTER_AREA)
            moving = np.clip((motion_grid - 4.0) / 48.0, 0.0, 1.0)
            weight = (moving * (0.5 * mosh))[:, :, None]
            previous = self._previous_mosh_grid.astype(np.float32)
            styled = styled * (1.0 - weight) + previous * weight
        self._previous_mosh_grid = current_grid.copy()
        styled = np.clip(np.rint(styled), 0, 255).astype(np.uint8)
        return cv.resize(styled, (width, height), interpolation=cv.INTER_NEAREST)

    def process(self, frame, frame_index: int, timestamp: Optional[float],
                context: Optional[dict] = None):
        if self._np is None:
            raise RuntimeError("initialize Elastic Collapse before processing")
        np, cv = self._np, self._cv
        if not isinstance(frame, np.ndarray) or frame.shape != self._shape or frame.dtype != np.uint8:
            raise ValueError(f"expected uint8 RGB frame {self._shape}")
        height, width = self._shape[:2]
        current = cv.resize(frame, (self._work_shape[1], self._work_shape[0]),
                            interpolation=cv.INTER_AREA)
        stamp = self._number(timestamp, None, -1e12, 1e12)
        index = int(frame_index)
        rewind = (stamp is not None and self._previous_timestamp is not None
                  and stamp < self._previous_timestamp)
        cut = (self._previous_rgb is not None and
               float(np.abs(current.astype(np.float32)
                            - self._previous_rgb.astype(np.float32)).mean()) / 255 >= 0.55)
        if ((self._next_index is not None and index != self._next_index)
                or rewind or cut or (context or {}).get("scene_cut")):
            self.reset()
        motion_delta = (np.abs(current.astype(np.float32)
                               - self._previous_rgb.astype(np.float32)).mean(axis=2)
                        if self._previous_rgb is not None else None)
        if self._config["mode"] == "seam":
            self._progress = (
                min(1.0, max(0.0, stamp / self._clip_duration))
                if stamp is not None and self._clip_duration else 1.0
            )
        else:
            self._progress = 1.0
        dt = (max(0.0, stamp - self._previous_timestamp)
              if stamp is not None and self._previous_timestamp is not None
              else 1.0 / self._fps)
        if self._previous_rgb is not None:
            self._clock += dt
        target = (self._seam_field(current) if self._config["mode"] == "seam"
                  else self._elastic_field(current))
        coherence = self._config["coherence"] / 100.0
        alpha = 1.0 if coherence == 0 else -math.expm1(-dt / (coherence * coherence * 0.75))
        if self._field is None:
            self._field = target
        else:
            self._field += (target - self._field) * alpha
        self._previous_rgb = current.copy()
        self._previous_timestamp = stamp
        self._next_index = index + 1
        intensity = self._config["intensity"] / 100.0
        if (intensity == 0
                or (self._config["mode"] == "seam"
                    and self._config["seam_strength"] == 0)
                or not (self._config["horizontal"] or self._config["vertical"])):
            return frame.copy()
        full = cv.resize(self._field, (width, height), interpolation=cv.INTER_LINEAR)
        # Seam removal's resize map expands the retained content; invert it to shrink.
        amount = (-self._progress if self._config["mode"] == "seam"
                  else intensity)
        map_x = full[:, :, 0] * (amount * max(0, width - 1))
        map_y = full[:, :, 1] * (amount * max(0, height - 1))
        map_x += np.arange(width, dtype=np.float32)[None, :]
        map_y += np.arange(height, dtype=np.float32)[:, None]
        warped = cv.remap(frame, map_x, map_y, interpolation=cv.INTER_LINEAR,
                          borderMode=cv.BORDER_REFLECT_101)
        if not self._config["pixelate"]:
            self._previous_mosh_grid = None
            return warped
        return self._pixel_mosh(warped, motion_delta)


class CausalityInversion:
    """Bounded future-pixel borrowing; emit each packet on its original timeline."""

    requires_timestamps = True
    MAX_STATE_BYTES: int = 128 * 1024 * 1024
    MAX_FRAMES = 128
    DEFAULTS = {
        "lookahead": 0.5, "offset": 0.5, "strength": 65, "mode": "blend",
        "region": "full", "mask_speed": 0.25, "alignment": 100,
        "feather": 10, "seed": 0,
    }

    def __init__(self):
        self._np: Any = None
        self._config = {}
        self._shape = (0, 0, 0)
        self._queue = deque()
        self._seed = 0
        self._capacity = 0
        self._next_index = None
        self._last_timestamp = None
        self._scene_sample: Any = None
        self._coverage: Any = None
        self.capacity_fallbacks = 0
        self.effective_lookahead = 0.0
        self.effective_offset = 0.0
        self.runtime_notes = []

    def initialize(self, config: dict, frame_shape: tuple, fps: float, seed: int) -> None:
        try:
            import numpy as np
        except ImportError as exc:
            raise RuntimeError("Causality Inversion needs requirements-inversion.txt") from exc
        if (len(frame_shape) != 3 or frame_shape[2] != 3
                or any(int(v) < 1 for v in frame_shape)):
            raise ValueError("Causality Inversion expects an HxWx3 RGB frame")
        self._np = np
        self._shape = tuple(int(v) for v in frame_shape)
        self._config = dict(self.DEFAULTS)
        for key, default in self.DEFAULTS.items():
            if key in ("mode", "region"):
                value = str(config.get(key, default))
                choices = (("blend", "replace", "tear", "align") if key == "mode"
                           else ("full", "bands", "blocks", "patch"))
                if value not in choices:
                    raise ValueError(f"invalid inversion {key}: {value}")
                self._config[key] = value
            else:
                high = (5 if key in ("lookahead", "offset") else
                        4 if key == "mask_speed" else
                        2147483647 if key == "seed" else 100)
                value = ElasticCollapse._number(config.get(key, default), default, 0, high)
                self._config[key] = (value if key in ("lookahead", "offset", "mask_speed")
                                     else int(value))
        if self._config["offset"] > self._config["lookahead"]:
            raise ValueError("inversion offset must not exceed lookahead seconds")
        if self._config["mode"] == "align" and self._config["alignment"]:
            try:
                import cv2  # noqa: F401
            except ImportError as exc:
                raise RuntimeError("Aligned inversion needs requirements-collapse.txt") from exc
        self._seed = int(ElasticCollapse._number(seed, 0, 0, 2147483647))
        frame_bytes = math.prod(self._shape)
        self._capacity = min(self.MAX_FRAMES, (self.MAX_STATE_BYTES - 65536) // frame_bytes)
        delay = self._config["lookahead"] if self._config["strength"] and self._config["offset"] else 0
        rate = ElasticCollapse._number(fps, 30.0, 0.001, 1000)
        if self._capacity < 1:
            raise ValueError(
                "inversion cannot fit one RGB frame in its 128 MiB buffer budget; "
                "resize before inversion"
            )
        self.effective_lookahead = min(delay, (self._capacity - 1) / rate)
        self.effective_offset = min(self._config["offset"], self.effective_lookahead)
        self.runtime_notes = []
        if self.effective_lookahead + 1e-9 < delay:
            self.runtime_notes.append(
                f"inversion buffer budget limits look-ahead to {self.effective_lookahead:.3f}s "
                f"and future offset to {self.effective_offset:.3f}s "
                f"at {self._shape[1]}x{self._shape[0]} / {rate:g} fps; "
                "resize before inversion for a longer horizon")
        self.reset()

    @property
    def state_nbytes(self) -> int:
        total = sum(packet.frame.nbytes for packet in self._queue)
        return int(total + sum(a.nbytes for a in (self._scene_sample, self._coverage)
                               if a is not None))

    @property
    def buffered_frames(self) -> int:
        return len(self._queue)

    @property
    def capacity(self) -> int:
        return self._capacity

    def serialize_config(self) -> dict:
        return dict(self._config)

    def reset(self) -> None:
        self._queue.clear()
        self._next_index = None
        self._last_timestamp = None
        self._scene_sample = None
        self._coverage = None
        self.capacity_fallbacks = 0

    def _mask(self, timestamp):
        np = self._np
        height, width = self._shape[:2]
        y = np.arange(height, dtype=np.float32)[:, None] / max(1, height - 1)
        x = np.arange(width, dtype=np.float32)[None, :] / max(1, width - 1)
        phase = (self._seed % 997) / 997 * 2 * math.pi
        t = float(timestamp) * self._config["mask_speed"] * 2 * math.pi
        region = self._config["region"]
        if region == "full":
            return np.ones((height, width), np.float32)
        if region == "bands":
            signed = np.broadcast_to(np.sin(y * 8 * math.pi + t + phase), (height, width))
        elif region == "blocks":
            signed = np.sin(x * 8 * math.pi + t + phase) * np.sin(y * 6 * math.pi - t)
        else:
            cx, cy = 0.5 + 0.23 * math.sin(t + phase), 0.5 + 0.23 * math.cos(t + phase)
            signed = 0.25 - np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        feather = self._config["feather"] / 100.0
        return (np.clip(0.5 + signed / (2 * feather), 0, 1).astype(np.float32)
                if feather else (signed >= 0).astype(np.float32))

    def _borrow(self, current, future):
        np = self._np
        mode = self._config["mode"]
        height, width = self._shape[:2]
        if mode == "tear":
            rows = np.arange(height)[:, None]
            phase = float(current.timestamp) * self._config["mask_speed"] * 2 * math.pi
            offsets = np.rint(np.sin(rows // max(1, height // 8) + phase
                                     + self._seed) * max(1, width // 8)).astype(np.int32)
            return future.frame[rows, (np.arange(width)[None, :] + offsets) % width]
        if mode != "align" or not self._config["alignment"] or min(height, width) < 8:
            return future.frame
        import cv2 as cv
        scale = min(1.0, 192 / max(height, width))
        size = (max(8, round(width * scale)), max(8, round(height * scale)))
        a = cv.cvtColor(cv.resize(current.frame, size), cv.COLOR_RGB2GRAY)
        b = cv.cvtColor(cv.resize(future.frame, size), cv.COLOR_RGB2GRAY)
        flow = cv.calcOpticalFlowFarneback(a, b, None, 0.5, 3, 21, 5, 7, 1.5, 0)
        flow = cv.resize(flow, (width, height))
        gain = self._config["alignment"] / 100.0
        mx = np.arange(width, dtype=np.float32)[None, :] + np.clip(flow[:, :, 0] * width / size[0], -width / 4, width / 4) * gain
        my = np.arange(height, dtype=np.float32)[:, None] + np.clip(flow[:, :, 1] * height / size[1], -height / 4, height / 4) * gain
        return cv.remap(future.frame, mx, my, cv.INTER_LINEAR, borderMode=cv.BORDER_REFLECT_101)

    def _mix(self, current, future):
        np = self._np
        strength = self._config["strength"] / 100.0
        if strength == 0 or current.timestamp == future.timestamp:
            return replace(current, frame=current.frame.copy())
        borrowed = self._borrow(current, future)
        mask = self._mask(current.timestamp)
        if self._config["mode"] == "replace":
            if self._coverage is None:
                self._coverage = np.random.default_rng(self._seed).random((16, 16)).astype(np.float32)
            height, width = self._shape[:2]
            rows = np.arange(height) * 16 // height
            cols = np.arange(width) * 16 // width
            alpha = (self._coverage[rows[:, None], cols[None, :]] < strength).astype(np.float32)
            alpha = (alpha * mask)[:, :, None]
        else:
            alpha = (strength * mask)[:, :, None]
        result = current.frame.astype(np.float32) * (1 - alpha) + borrowed.astype(np.float32) * alpha
        return replace(current, frame=np.ascontiguousarray(np.clip(result, 0, 255).round().astype(np.uint8)))

    def _emit_oldest(self):
        current = self._queue.popleft()
        target = float(current.timestamp) + self.effective_offset
        future = next((p for p in self._queue if float(p.timestamp) + 1e-9 >= target),
                      self._queue[-1] if self._queue else current)
        return self._mix(current, future)

    def push(self, packet):
        if self._np is None:
            raise RuntimeError("initialize Causality Inversion before buffering")
        np = self._np
        if (not isinstance(packet.frame, np.ndarray) or packet.frame.shape != self._shape
                or packet.frame.dtype != np.uint8):
            raise ValueError(f"expected uint8 RGB frame {self._shape}")
        stamp = float(packet.timestamp)
        if not math.isfinite(stamp):
            raise ValueError("inversion needs finite presentation timestamps")
        if ((self._next_index is not None and packet.index != self._next_index)
                or (self._last_timestamp is not None and stamp < self._last_timestamp)):
            self.reset()
        sample = packet.frame[::max(1, self._shape[0] // 32),
                              ::max(1, self._shape[1] // 32)].copy()
        cut = (self._scene_sample is not None and
               float(np.abs(sample.astype(np.float32) - self._scene_sample.astype(np.float32)).mean()) >= 140.25)
        if cut or packet.context.get("scene_cut"):
            yield from self.flush()
        self._scene_sample = sample
        self._next_index = packet.index + 1
        self._last_timestamp = stamp
        if self._config["strength"] == 0 or self.effective_offset == 0:
            yield replace(packet, frame=packet.frame.copy())
            return
        while len(self._queue) >= self._capacity:
            self.capacity_fallbacks += 1
            yield self._emit_oldest()
        self._queue.append(replace(packet, frame=packet.frame.copy()))
        while self._queue and stamp - float(self._queue[0].timestamp) + 1e-9 >= self.effective_lookahead:
            yield self._emit_oldest()

    def flush(self):
        """EOF/cut: clamp unavailable future requests to the last buffered frame."""
        while self._queue:
            yield self._emit_oldest()
        self._scene_sample = None
        self._coverage = None
