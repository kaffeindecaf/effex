"""Bounded seam-carving displacement helper for content-aware elastic collapse.

``build_seam_field`` produces a normalized HxWx2 float32 displacement field
(dx/(W-1), dy/(H-1)) that maps each output pixel back to the source coordinate
it samples after the lowest-energy seams are carved out of the image.

numpy and cv2 are imported lazily inside the helpers, so this module imports
with only the standard library.  cv2 (opencv-python-headless) is an optional
dependency needed only for the map resize; the analytic seam finders work with
numpy alone.
"""
import math
from typing import List

_MAX_SEAMS = 4


def _coerce_scalar(value: float) -> float:
    """Clamp a scalar to [0, 1]; non-finite values become 0.0 (disabled)."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return min(1.0, max(0.0, number))


def _coerce_seams(value) -> int:
    """Clamp max_seams to an integer in [0, 4]."""
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return _MAX_SEAMS
    if not math.isfinite(float(number)):
        return _MAX_SEAMS
    return min(_MAX_SEAMS, max(0, number))


def _gradient_energy(frame):
    """Gradient-magnitude energy map (float32, HxW) of a uint8 RGB frame."""
    import numpy as np
    gray = frame.astype(np.float32).mean(axis=2)
    gy, gx = np.gradient(gray)
    return np.sqrt(gx * gx + gy * gy).astype(np.float32)


def _weight_energy(energy, preservation: float):
    """Amplify high-energy pixels so high preservation protects them more.

    Returns ``energy * (1 + preservation * energy / max(energy))``.  At
    preservation 0 the map is unchanged; at 1 the highest-energy pixels carry
    twice the cost, steering seams around energy-rich content.  Preserves the
    input dtype.
    """
    import numpy as np
    energy = np.asarray(energy, dtype=np.float64)
    maximum = float(energy.max()) if energy.size else 0.0
    if maximum <= 0.0:
        return energy.astype(np.float32)
    weighted = energy * (1.0 + float(preservation) * (energy / maximum))
    return weighted.astype(np.float32)


def _cumulative_cost(energy):
    """Min-cost vertical-seam cumulative cost map (float64, HxW)."""
    import numpy as np
    energy = np.asarray(energy, dtype=np.float64)
    height, width = energy.shape
    cost = np.empty_like(energy)
    if height == 0 or width == 0:
        return cost
    cost[0] = energy[0]
    for y in range(1, height):
        left = np.concatenate(([np.inf], cost[y - 1, :-1]))
        up = cost[y - 1]
        right = np.concatenate((cost[y - 1, 1:], [np.inf]))
        cost[y] = energy[y] + np.minimum(np.minimum(left, up), right)
    return cost


def _find_vertical_seam(energy) -> List[int]:
    """Column indices (length H) of the min-cost vertical seam."""
    import numpy as np
    energy = np.asarray(energy, dtype=np.float64)
    height, width = energy.shape
    if height < 1 or width < 1:
        return []
    cost = _cumulative_cost(energy)
    path = np.empty(height, dtype=np.int32)
    path[height - 1] = int(np.argmin(cost[height - 1]))
    for y in range(height - 2, -1, -1):
        column = int(path[y + 1])
        low = max(0, column - 1)
        high = min(width - 1, column + 1) + 1
        path[y] = low + int(np.argmin(cost[y, low:high]))
    return [int(c) for c in path.tolist()]


def _find_horizontal_seam(energy) -> List[int]:
    """Row indices (length W) of the min-cost horizontal seam."""
    import numpy as np
    return _find_vertical_seam(np.asarray(energy).T)


def _remove_vertical_seam(array, seam: List[int]):
    """Remove one pixel per row along a vertical seam; width shrinks by 1."""
    import numpy as np
    height, width = array.shape
    rows = np.arange(height).reshape(-1, 1)
    cols = np.arange(width - 1).reshape(1, -1)
    seam_col = np.asarray(seam, dtype=np.int64).reshape(-1, 1)
    source_col = np.where(cols < seam_col, cols, cols + 1)
    return array[rows, source_col]


def _remove_horizontal_seam(array, seam: List[int]):
    """Remove one pixel per column along a horizontal seam; height shrinks by 1."""
    import numpy as np
    height, width = array.shape
    rows = np.arange(height - 1).reshape(-1, 1)
    cols = np.arange(width).reshape(1, -1)
    seam_row = np.asarray(seam, dtype=np.int64).reshape(1, -1)
    source_row = np.where(rows < seam_row, rows, rows + 1)
    return array[source_row, cols]


def build_seam_field(frame, horizontal, vertical, strength, preservation,
                     max_seams: int = 4):
    """Return a normalized HxWx2 displacement field from seam carving.

    ``frame`` is a reduced-resolution uint8 RGB array (H, W, 3).  The scalar
    controls are clamped to [0, 1] (non-finite becomes 0).  ``max_seams`` is
    clamped to [0, 4].  Up to ``strength * horizontal * max_seams`` vertical
    seams (one pixel per row) are removed, producing x displacement, and up to
    ``strength * vertical * max_seams`` horizontal seams (one pixel per
    column) are removed, producing y displacement.  Each seam is the min-cost
    path through the preservation-weighted gradient energy, found by dynamic
    programming.

    Source coordinate maps are carved alongside the energy, then resized back
    to the original HxW via cv2.resize.  The returned field is the carved
    coordinate offset from the identity grid, normalized to (dx/(W-1),
    dy/(H-1)) so every value lies in [-1, 1].  The input frame is never
    modified.  Disabled axes, zero strength, or degenerate sizes return an
    all-zero field.
    """
    import numpy as np

    horizontal = _coerce_scalar(horizontal)
    vertical = _coerce_scalar(vertical)
    strength = _coerce_scalar(strength)
    preservation = _coerce_scalar(preservation)
    max_seams = _coerce_seams(max_seams)

    frame = np.asarray(frame)
    if frame.ndim != 3 or frame.shape[2] != 3:
        raise ValueError("seam field expects an HxWx3 RGB frame")
    height, width = int(frame.shape[0]), int(frame.shape[1])
    if frame.dtype != np.uint8:
        frame = frame.astype(np.uint8)

    zero = np.zeros((height, width, 2), dtype=np.float32)
    if (height < 2 or width < 2 or strength == 0.0 or max_seams == 0
            or (horizontal == 0.0 and vertical == 0.0)):
        return zero

    n_vertical = min(int(round(strength * horizontal * max_seams)),
                     width - 1)
    n_horizontal = min(int(round(strength * vertical * max_seams)),
                       height - 1)
    if n_vertical == 0 and n_horizontal == 0:
        return zero

    energy = _weight_energy(_gradient_energy(frame), preservation)
    x_map = np.tile(np.arange(width, dtype=np.float32), (height, 1))
    y_map = np.tile(np.arange(height, dtype=np.float32)[:, None], (1, width))

    for _ in range(n_vertical):
        seam = _find_vertical_seam(energy)
        if not seam:
            break
        energy = _remove_vertical_seam(energy, seam)
        x_map = _remove_vertical_seam(x_map, seam)
        y_map = _remove_vertical_seam(y_map, seam)

    for _ in range(n_horizontal):
        seam = _find_horizontal_seam(energy)
        if not seam:
            break
        energy = _remove_horizontal_seam(energy, seam)
        x_map = _remove_horizontal_seam(x_map, seam)
        y_map = _remove_horizontal_seam(y_map, seam)

    import cv2

    displacement = np.zeros((height, width, 2), dtype=np.float32)
    if n_vertical > 0:
        resized_x = cv2.resize(x_map, (width, height),
                               interpolation=cv2.INTER_LINEAR)
        displacement[:, :, 0] = (
            resized_x - np.arange(width, dtype=np.float32)) / float(width - 1)
    if n_horizontal > 0:
        resized_y = cv2.resize(y_map, (width, height),
                               interpolation=cv2.INTER_LINEAR)
        displacement[:, :, 1] = (
            resized_y - np.arange(height, dtype=np.float32)[:, None]) / float(height - 1)
    return np.ascontiguousarray(displacement)
