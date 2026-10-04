# SPDX-License-Identifier: Apache-2.0
"""Frame checks: scalar reduction, darkness, saturation, stability between frames."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image

from vispek_hc import framecheck
from vispek_hc.framecheck import MaskBit


def flat(rgb: tuple[int, int, int], shape: tuple[int, int] = (20, 30)) -> np.ndarray:
    return np.full((*shape, 3), rgb, np.uint8)


# --- reducing RGB to one value per pixel -----------------------------------------------


def test_dominant_reduction_takes_the_named_channel() -> None:
    rgb = flat((10, 20, 30))
    assert np.all(framecheck.reduce(rgb, "R") == 10)
    assert np.all(framecheck.reduce(rgb, "G") == 20)
    assert np.all(framecheck.reduce(rgb, "B") == 30)
    assert framecheck.reduce(rgb, "B").dtype == np.uint8


@pytest.mark.parametrize(
    ("rgb", "luma"),
    [
        ((0, 0, 0), 0),
        ((255, 255, 255), 255),
        ((5, 5, 5), 5),
        ((255, 0, 0), 76),
        ((0, 255, 0), 150),
        ((0, 0, 255), 29),
        ((18, 21, 139), 34),
        ((182, 95, 154), 128),
    ],
)
def test_luma_reduction_fixed_values(rgb: tuple[int, int, int], luma: int) -> None:
    assert int(framecheck.reduce(flat(rgb, (1, 1)), "luma")[0, 0]) == luma


def test_luma_reduction_equals_pillow_l_conversion() -> None:
    rng = np.random.default_rng(7)
    rgb = rng.integers(0, 256, (64, 64, 3), dtype=np.uint8)
    expected = np.asarray(Image.fromarray(rgb, "RGB").convert("L"))
    assert np.array_equal(framecheck.reduce(rgb, "luma"), expected)


# --- darkness and overexposure ---------------------------------------------------------


def test_frame_at_black_level_is_nearly_black() -> None:
    assert framecheck.nearly_black(framecheck.reduce(flat((5, 5, 5)), "R"), black_level=5)
    assert framecheck.nearly_black(framecheck.reduce(flat((6, 6, 6)), "R"), black_level=5)


def test_absolute_darkness_threshold_would_miss_this_camera() -> None:
    # The camera clamps darkness to 5: "<= 1" never fires, "<= black + 1" does.
    values = framecheck.reduce(flat((5, 5, 5)), "R")
    assert not framecheck.nearly_black(values, black_level=0)
    assert framecheck.nearly_black(values, black_level=5)


def test_a_lit_frame_is_not_nearly_black() -> None:
    assert not framecheck.nearly_black(framecheck.reduce(flat((40, 6, 6)), "R"), black_level=5)


def test_nearly_black_tolerates_half_a_percent_of_bright_pixels() -> None:
    values = np.full((100, 100), 5, np.uint8)
    values.flat[:50] = 200  # exactly 0.5 %
    assert framecheck.nearly_black(values, black_level=5)
    values.flat[:51] = 200
    assert not framecheck.nearly_black(values, black_level=5)


def test_nearly_overexposed_needs_almost_every_pixel_at_254_or_more() -> None:
    values = np.full((100, 100), 254, np.uint8)
    assert framecheck.nearly_overexposed(values)
    values.flat[:51] = 253
    assert not framecheck.nearly_overexposed(values)


# --- clipping --------------------------------------------------------------------------


def test_clip_mask_marks_each_clipped_channel_and_the_scalar() -> None:
    rgb = np.array(
        [[[255, 10, 10], [10, 255, 10], [10, 10, 255], [254, 254, 254], [255, 255, 255]]], np.uint8
    )
    mask = framecheck.clip_mask(rgb, "G")
    red, green, blue, scalar, saturated = (
        MaskBit.RED_CLIPPED,
        MaskBit.GREEN_CLIPPED,
        MaskBit.BLUE_CLIPPED,
        MaskBit.SCALAR_CLIPPED,
        MaskBit.SATURATED,
    )
    assert mask.dtype == np.uint8
    assert mask.tolist() == [
        [
            red | saturated,
            green | scalar | saturated,
            blue | saturated,
            0,
            red | green | blue | scalar | saturated,
        ]
    ]


def test_mask_bits_have_the_documented_values() -> None:
    assert [bit.value for bit in MaskBit] == [1, 2, 4, 8, 16, 32, 64, 128]
    assert MaskBit.INVALID | MaskBit.SATURATED | MaskBit.SMALL_DENOMINATOR == 7


def test_saturated_fraction_counts_pixels_with_any_clipped_channel() -> None:
    rgb = flat((10, 10, 10), (10, 10))
    rgb[0, :5, 0] = 255
    rgb[1, :5, 2] = 255
    assert framecheck.saturated_fraction(rgb) == pytest.approx(0.10)
    assert framecheck.saturated_fraction(flat((254, 254, 254))) == 0.0


# --- stability between two consecutive frames ------------------------------------------


def test_identical_frames_are_stable() -> None:
    a = np.full((40, 60), 100, np.uint8)
    result = framecheck.compare(a, a.copy(), tolerance=8)
    assert result.stable
    assert (result.mean_difference, result.large_change_fraction, result.max_line_difference) == (
        0.0,
        0.0,
        0.0,
    )


def test_small_uniform_noise_is_stable() -> None:
    a = np.full((40, 60), 100, np.uint8)
    result = framecheck.compare(a, a + 3, tolerance=8)
    assert result.stable
    assert result.mean_difference == pytest.approx(3.0)


def test_mean_difference_above_tolerance_is_unstable() -> None:
    a = np.full((40, 60), 100, np.uint8)
    assert not framecheck.compare(a, a + 9, tolerance=8).stable


def test_crossing_frame_is_caught_by_the_line_difference() -> None:
    # Rolling shutter: the LED came on while the frame was read out. Top dark, bottom lit.
    # Only 12 % of the rows changed, so the mean difference alone would let it pass.
    lit = np.full((100, 60), 60, np.uint8)
    crossing = lit.copy()
    crossing[:12] = 5
    result = framecheck.compare(crossing, lit, tolerance=8)
    assert result.mean_difference == pytest.approx(0.12 * 55)
    assert result.mean_difference <= 8
    assert result.max_line_difference == pytest.approx(55.0)
    assert not result.stable


def test_many_strongly_changed_pixels_are_unstable_even_with_a_small_mean() -> None:
    a = np.full((100, 100), 100, np.uint8)
    b = a.copy()
    # 11 % of the pixels jump by 30 (> max(25, 3 * 8)), scattered so no row or column stands out.
    rng = np.random.default_rng(3)
    b.flat[rng.choice(a.size, 1100, replace=False)] += 30
    result = framecheck.compare(a, b, tolerance=8)
    assert result.mean_difference < 8
    assert result.max_line_difference <= 32
    assert result.large_change_fraction == pytest.approx(0.11)
    assert not result.stable


def test_thresholds_scale_with_a_wide_tolerance() -> None:
    a = np.full((100, 100), 100, np.uint8)
    b = a.copy()
    b[:10] += 100  # one tenth of the rows jumps by 100
    assert not framecheck.compare(a, b, tolerance=8).stable
    # tolerance 30: mean 10 <= 30, large (> 90) fraction 0.10 <= 0.10, line 100 <= 120
    assert framecheck.compare(a, b, tolerance=30).stable


def one_line_changed(amount: int, axis: int) -> tuple[np.ndarray, np.ndarray]:
    a = np.full((100, 100), 100, np.uint8)
    b = a.copy()
    if axis == 0:
        b[40] += amount
    else:
        b[:, 40] += amount
    return a, b


@pytest.mark.parametrize("axis", [0, 1])
def test_line_difference_limit_is_32_code_values_for_rows_and_columns(axis: int) -> None:
    assert framecheck.compare(*one_line_changed(32, axis), tolerance=1).stable
    assert not framecheck.compare(*one_line_changed(33, axis), tolerance=1).stable


def test_line_difference_limit_grows_to_four_times_a_wide_tolerance() -> None:
    assert framecheck.compare(*one_line_changed(80, 0), tolerance=20).stable
    assert not framecheck.compare(*one_line_changed(81, 0), tolerance=20).stable


@pytest.mark.parametrize(("jump", "stable"), [(30, True), (31, False)])
def test_a_large_change_is_more_than_three_times_the_tolerance(jump: int, stable: bool) -> None:
    a = np.full((100, 100), 100, np.uint8)
    b = a.copy()
    b.flat[np.random.default_rng(3).choice(a.size, 1100, replace=False)] += jump
    assert framecheck.compare(a, b, tolerance=10).stable is stable


def test_comparison_is_symmetric_and_handles_darkening() -> None:
    a = np.full((40, 60), 100, np.uint8)
    b = np.full((40, 60), 90, np.uint8)
    assert framecheck.compare(a, b, tolerance=8).mean_difference == pytest.approx(10.0)
    assert framecheck.compare(b, a, tolerance=8).mean_difference == pytest.approx(10.0)


def test_frames_of_different_size_cannot_be_compared() -> None:
    with pytest.raises(ValueError, match="size"):
        framecheck.compare(np.zeros((4, 4), np.uint8), np.zeros((4, 5), np.uint8), tolerance=8)
