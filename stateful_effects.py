"""Bounded, stateful frame processors used by effex's raw-frame runner."""
import math
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
