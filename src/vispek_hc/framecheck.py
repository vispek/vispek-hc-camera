# SPDX-License-Identifier: Apache-2.0
"""Per-frame checks: is it dark, is it clipped, has the picture settled.

All checks work on the per-pixel scalar of a frame (see :func:`reduce`), in raw code
values. Darkness is judged against the camera's black level, which this camera clamps to
5: a test against zero would never fire.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Literal

import numpy as np
from numpy.typing import NDArray

from vispek_hc.models import Channel

Reduction = Channel | Literal["luma"]

WHOLE_FRAME = 0.995  # "nearly all pixels"
LARGE_CHANGE_LIMIT = 0.10
CLIP_LEVEL = 255


class MaskBit(enum.IntFlag):
    """Per-pixel quality bits. Pixels with any of the first three are not valid."""

    INVALID = 1  # missing or not a number
    SATURATED = 2  # any RGB component or the scalar is clipped
    SMALL_DENOMINATOR = 4  # white reference too dark to divide by
    OUT_OF_RANGE = 8  # relative value outside [0, 1]; the value is kept
    RED_CLIPPED = 16
    GREEN_CLIPPED = 32
    BLUE_CLIPPED = 64
    SCALAR_CLIPPED = 128


def reduce(rgb: NDArray[np.uint8], reduction: Reduction) -> NDArray[np.uint8]:
    """One value per pixel: a colour channel, or integer BT.601 luma (legacy data)."""
    if reduction != "luma":
        return rgb[..., "RGB".index(reduction)]
    wide = rgb.astype(np.uint32)
    luma = (wide[..., 0] * 19595 + wide[..., 1] * 38470 + wide[..., 2] * 7471 + 0x8000) >> 16
    return luma.astype(np.uint8)


def nearly_black(values: NDArray[np.uint8], black_level: int) -> bool:
    """True if at least 99.5 % of the pixels are within 1 code value of the black level."""
    return float(np.mean(values <= black_level + 1)) >= WHOLE_FRAME


def nearly_overexposed(values: NDArray[np.uint8]) -> bool:
    return float(np.mean(values >= CLIP_LEVEL - 1)) >= WHOLE_FRAME


def clip_mask(rgb: NDArray[np.uint8], reduction: Reduction) -> NDArray[np.uint8]:
    """Clipping bits for one frame, from raw code values (dark is not subtracted)."""
    clipped = rgb >= CLIP_LEVEL
    mask = (
        clipped[..., 0] * np.uint8(MaskBit.RED_CLIPPED)
        | clipped[..., 1] * np.uint8(MaskBit.GREEN_CLIPPED)
        | clipped[..., 2] * np.uint8(MaskBit.BLUE_CLIPPED)
        | (reduce(rgb, reduction) >= CLIP_LEVEL) * np.uint8(MaskBit.SCALAR_CLIPPED)
    )
    return (mask | (mask > 0) * np.uint8(MaskBit.SATURATED)).astype(np.uint8)


def saturated_fraction(rgb: NDArray[np.uint8]) -> float:
    """Share of pixels with at least one clipped RGB component."""
    return float(np.mean(np.any(rgb >= CLIP_LEVEL, axis=-1)))


@dataclass(frozen=True, slots=True)
class Stability:
    mean_difference: float
    large_change_fraction: float
    max_line_difference: float
    stable: bool


def compare(a: NDArray[np.uint8], b: NDArray[np.uint8], tolerance: float) -> Stability:
    """Decide whether two consecutive frames show the same, settled picture.

    Three conditions, all required. Together they catch the frame that was being read out
    while the LED switched (lit in one part, dark in the other), which a mean alone misses.
    """
    if a.shape != b.shape:
        raise ValueError(f"frames differ in size: {a.shape} and {b.shape}")
    difference = np.abs(a.astype(np.int16) - b.astype(np.int16))
    mean_difference = float(difference.mean())
    large = float(np.mean(difference > max(25.0, 3 * tolerance)))
    signed = a.astype(np.float64) - b
    line = max(float(np.abs(signed.mean(axis=1)).max()), float(np.abs(signed.mean(axis=0)).max()))
    stable = (
        mean_difference <= tolerance
        and large <= LARGE_CHANGE_LIMIT
        and line <= max(32.0, 4 * tolerance)
    )
    return Stability(mean_difference, large, line, stable)
