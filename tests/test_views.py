# SPDX-License-Identifier: Apache-2.0
"""Views of a cube: stretch, palettes, composites, indices, principal components."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PIL import Image

from vispek_hc import views
from vispek_hc.cube import Cube, build_cube
from vispek_hc.errors import ConfigRejectedError, InvalidStateError
from vispek_hc.framecheck import MaskBit
from vispek_hc.models import HC1500

H, W = 24, 40


def cube_of(led_ids: list[int], layer: str = "L2", **values_by_led: Any) -> Cube:
    """A cube whose band values are given per LED (constant or an (H, W) array)."""
    leds = [HC1500.led(n) for n in led_ids]
    frames = [np.full((H, W, 3), 10, np.uint8) for _ in leds]
    cube = build_cube(frames, np.full((H, W, 3), 5, np.uint8), leds, "dominant", {})
    for index, led_id in enumerate(led_ids):
        if f"led_{led_id:02d}" in values_by_led:
            cube.values[:, :, index] = values_by_led[f"led_{led_id:02d}"]
    cube.metadata["layer"] = layer
    cube.metadata["unit"] = "relative_to_white" if layer == "L2" else "image_code_value"
    return cube


def ramp() -> np.ndarray:
    return np.tile(np.linspace(0, 1, W, dtype=np.float32), (H, 1))


ALL = list(range(1, 18))


# --- stretch ---------------------------------------------------------------------------


def test_stretch_maps_the_1st_to_99th_percentile_of_valid_pixels_onto_0_to_255() -> None:
    values = np.linspace(0, 100, 1001, dtype=np.float32).reshape(1, -1)
    levels = views.stretch(values, np.ones_like(values, bool))
    assert levels.dtype == np.uint8
    assert levels[0, 0] == 0
    assert levels[0, -1] == 255
    assert levels[0, 500] == 128
    assert levels[0, 10] == 0, "1st percentile and below"
    assert levels[0, 990] == 255


def test_stretch_ignores_invalid_pixels_when_choosing_the_range() -> None:
    values = np.array([[0.0, 0.5, 1.0, 1000.0]], np.float32)
    valid = np.array([[True, True, True, False]])
    levels = views.stretch(values, valid, low=None, high=None)
    assert levels[0, 3] == 255
    assert levels[0, 1] == pytest.approx(128, abs=3)


def test_fixed_range_and_gamma() -> None:
    values = np.array([[0.0, 0.25, 0.5, 2.0]], np.float32)
    valid = np.ones_like(values, bool)
    assert views.stretch(values, valid, low=0, high=1).tolist() == [[0, 64, 128, 255]]
    assert views.stretch(values, valid, low=0, high=1, gamma=2).tolist() == [[0, 128, 180, 255]]


def test_flat_data_falls_back_to_the_range_of_its_unit() -> None:
    values = np.full((2, 2), 0.5, np.float32)
    valid = np.ones_like(values, bool)
    assert views.stretch(values, valid, fallback=(0.0, 1.0))[0, 0] == 128
    assert views.stretch(values, ~valid, fallback=(0.0, 1.0))[0, 0] == 128, "no valid pixel at all"


def test_not_a_number_is_drawn_at_the_bottom_of_the_range() -> None:
    values = np.array([[np.nan, 1.0]], np.float32)
    assert views.stretch(values, np.array([[False, True]]), low=0, high=1).tolist() == [[0, 255]]


@pytest.mark.parametrize("options", [{"gamma": 0.05}, {"gamma": 6}, {"low": 1, "high": 1}])
def test_bad_display_settings_are_refused(options: dict[str, float]) -> None:
    values = np.zeros((2, 2), np.float32)
    with pytest.raises(ConfigRejectedError):
        views.stretch(values, np.ones_like(values, bool), **options)


# --- palettes --------------------------------------------------------------------------


def test_colour_tables_are_256_rgb_entries_built_in() -> None:
    for name in ("gray", "viridis", "spectral"):
        assert views.LUTS[name].shape == (256, 3)
        assert views.LUTS[name].dtype == np.uint8
    assert views.LUTS["gray"][128].tolist() == [128, 128, 128]
    assert views.LUTS["viridis"][0].tolist() == [68, 1, 84]
    assert views.LUTS["viridis"][255].tolist() == [253, 231, 37]
    assert views.CLASS_COLOURS.shape == (20, 3)


def test_class_palette_cycles_through_its_colours() -> None:
    levels = np.array([[0, 1, 20, 21]], np.uint8)
    image = views.colourise(levels, "classes")
    assert image[0, 0].tolist() == image[0, 2].tolist()
    assert image[0, 1].tolist() == image[0, 3].tolist()
    assert image[0, 0].tolist() != image[0, 1].tolist()


def test_unknown_palette_is_refused() -> None:
    with pytest.raises(ConfigRejectedError):
        views.colourise(np.zeros((1, 1), np.uint8), "rainbow")


# --- single bands and composites -------------------------------------------------------


def test_single_band_view_is_grey_and_stretched(tmp_path: Path) -> None:
    cube = cube_of(ALL, led_13=ramp())
    image = views.render(cube, "band:led_13")
    assert image.shape == (H, W, 3)
    assert image[0, 0].tolist() == [0, 0, 0]
    assert image[0, -1].tolist() == [255, 255, 255]
    coloured = views.render(cube, "band:led_13", palette="viridis")
    assert coloured[0, 0].tolist() == [68, 1, 84]
    path = views.save_png(image, tmp_path / "view.png")
    with Image.open(path) as stored:
        assert stored.size == (W, H)


@pytest.mark.parametrize(
    ("mode", "bands"),
    [
        ("rgb", ("led_09", "led_07", "led_05")),
        ("cir", ("led_13", "led_10", "led_07")),
        ("uv", ("led_04", "led_03", "led_02")),
    ],
)
def test_composites_put_the_documented_bands_into_red_green_blue(
    mode: str, bands: tuple[str, str, str]
) -> None:
    for channel, band in enumerate(bands):
        image = views.render(cube_of(ALL, **{band: ramp()}), mode, low=0, high=1)
        other = [c for c in range(3) if c != channel]
        assert image[0, -1, channel] == 255
        assert image[0, 0, channel] == 0
        assert np.all(image[..., other] == image[0, 0, other[0]]), "the other channels are flat"


def test_each_band_of_a_composite_is_stretched_on_its_own() -> None:
    cube = cube_of(ALL, led_09=ramp() * 0.1, led_07=ramp(), led_05=ramp() * 5)
    image = views.render(cube, "rgb")
    assert image[0, -1].tolist() == [255, 255, 255]
    assert image[0, 0].tolist() == [0, 0, 0]


def test_stretch_can_be_taken_from_a_region_only() -> None:
    values = ramp()
    values[:, 30:] = 50.0  # something very bright outside the white board
    cube = cube_of(ALL, led_13=values)
    everywhere = views.render(cube, "band:led_13")
    inside = views.render(cube, "band:led_13", roi_px=(0, 0, 30, H))
    assert everywhere[0, 15, 0] < 10
    assert inside[0, 15, 0] > 100


def test_stretch_defaults_to_the_white_region_of_the_calibration() -> None:
    values = ramp()
    values[:, 30:] = 50.0
    cube = cube_of(ALL, led_13=values)
    cube.metadata["calibration"] = {"white_roi_px": {"x0": 0, "y0": 0, "x1": 30, "y1": H}}
    by_default = views.render(cube, "band:led_13")
    assert np.array_equal(by_default, views.render(cube, "band:led_13", roi_px=(0, 0, 30, H)))
    assert by_default[0, 15, 0] > 100
    explicit = views.render(cube, "band:led_13", roi_px=(0, 0, W, H))
    assert explicit[0, 15, 0] < 10, "a region that is given wins"
    cube.metadata["calibration"] = {"white_roi_px": None}
    assert views.render(cube, "band:led_13")[0, 15, 0] < 10


def test_percentiles_are_the_1st_and_the_99th_not_the_extremes() -> None:
    values = np.linspace(0, 1, 1001, dtype=np.float32).reshape(1, -1)
    values[0, 0], values[0, -1] = -500.0, 500.0  # one wild pixel at each end
    levels = views.stretch(values, np.ones_like(values, bool))
    assert levels[0, 500] in (127, 128), "the wild pixels do not set the range"
    assert levels[0, 15] > 0
    assert levels[0, 5] == 0
    assert levels[0, 985] < 255
    assert levels[0, 995] == 255


def test_fixed_range_given_to_a_view_replaces_the_percentiles(tmp_path: Path) -> None:
    cube = cube_of(ALL, led_13=ramp())
    image = views.render(cube, "band:led_13", low=0.0, high=2.0)
    assert image[0, -1, 0] == 128, "1.0 on a 0-2 scale"
    half = views.render(cube, "band:led_13", low=0.0, high=2.0, gamma=2.0)
    assert half[0, -1, 0] == 180, "255 * 0.5 ** (1 / 2)"
    path = views.save_png(image, str(tmp_path / "view.png"))
    with Image.open(path) as stored:
        assert np.array_equal(np.asarray(stored), image)


def test_a_missing_band_is_an_error_not_a_substitute() -> None:
    with pytest.raises(ConfigRejectedError) as caught:
        views.render(cube_of([5, 7, 9]), "cir")
    assert caught.value.details["band_id"] == "led_13"
    with pytest.raises(ConfigRejectedError):
        views.render(cube_of([5, 7, 9]), "band:led_17")


def test_unknown_view_is_refused() -> None:
    with pytest.raises(ConfigRejectedError, match="unknown view"):
        views.render(cube_of(ALL), "thermal")


# --- raw data --------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["ndvi", "ndwi", "pca", "heat"])
def test_views_that_compare_bands_need_calibrated_data(mode: str) -> None:
    raw = cube_of(ALL, layer="L1", led_12=ramp() * 200)
    with pytest.raises(InvalidStateError) as caught:
        views.render(raw, mode)
    assert "--allow-uncalibrated" in (caught.value.hint or "")
    image = views.render(raw, mode, allow_uncalibrated=True)
    banner_red = (image[:12, :, 0] == 160) & (image[:12, :, 1] == 0)
    assert banner_red.mean() > 0.3, "rendered anyway, and marked as not comparable"


def test_raw_data_is_marked_with_a_banner() -> None:
    raw = views.render(cube_of(ALL, layer="L1", led_09=ramp() * 200), "rgb")
    calibrated = views.render(cube_of(ALL, led_09=ramp()), "rgb")
    banner_red = (raw[:12, :, 0] == 160) & (raw[:12, :, 1] == 0)
    assert banner_red.mean() > 0.3, "a red strip across the top"
    assert not ((calibrated[:12, :, 0] == 160) & (calibrated[:12, :, 1] == 0)).any()
    assert views.BANNER.isascii()


def test_a_program_that_marks_raw_data_itself_can_leave_the_banner_out() -> None:
    raw = cube_of(ALL, layer="L1", led_09=ramp() * 200)
    plain = views.render(raw, "rgb", banner=False)
    assert not ((plain[:12, :, 0] == 160) & (plain[:12, :, 1] == 0)).any()
    marked = views.render(raw, "rgb")
    assert plain.shape == marked.shape
    assert (plain[15:] == marked[15:]).all(), "the picture below the strip is the same"
    assert (plain != marked).any()


def test_view_range_says_what_the_ends_of_the_colour_table_stand_for() -> None:
    cube = cube_of(ALL, led_09=ramp(), led_12=0.8, led_10=0.2)
    low, high = views.view_range(cube, "band:led_09")  # type: ignore[misc]
    assert (low, high) == pytest.approx((0.01, 0.99), abs=0.02), "1st to 99th percentile"
    assert views.view_range(cube, "band:led_09", low=0.2, high=0.6) == (0.2, 0.6)
    assert views.view_range(cube, "band:led_09", high=2.0) == (pytest.approx(low), 2.0)
    assert views.view_range(cube, "ndvi") == (-1.0, 1.0), "an index has a fixed scale"
    assert views.view_range(cube, "ndvi", low=0.0, high=0.5) == (0.0, 0.5)
    assert views.view_range(cube, "rgb") is None, "three bands have no single scale"
    assert views.view_range(cube, "pca") is None
    flat = views.view_range(cube, "band:led_12")
    assert flat == (0.0, 1.0), "no spread: the range of the unit"
    with pytest.raises(ConfigRejectedError):
        views.view_range(cube, "band:led_09", low=0.9, high=0.1)
    with pytest.raises(ConfigRejectedError):
        views.view_range(cube, "nonsense")
    raw = cube_of(ALL, layer="L1", led_12=ramp() * 200)
    with pytest.raises(InvalidStateError):
        views.view_range(raw, "ndvi")
    assert views.view_range(raw, "ndvi", allow_uncalibrated=True) == (-1.0, 1.0)


def test_view_range_and_render_agree() -> None:
    cube = cube_of(ALL, led_09=ramp() * 0.5 + 0.2)
    low, high = views.view_range(cube, "band:led_09")  # type: ignore[misc]
    automatic = views.render(cube, "band:led_09")
    given = views.render(cube, "band:led_09", low=low, high=high)
    assert (automatic == given).all()


def test_raw_single_band_falls_back_to_the_code_value_range_when_flat() -> None:
    raw = cube_of(ALL, layer="L1", led_13=128.0)
    assert views.render(raw, "band:led_13")[-1, 0].tolist() == [128, 128, 128]


# --- indices ---------------------------------------------------------------------------


def test_ndvi_is_804_minus_667_over_their_sum() -> None:
    cube = cube_of(ALL, led_12=0.6, led_10=0.2)
    values, mask = views.index_of(cube, *views.INDICES["ndvi"])
    assert values[0, 0] == pytest.approx(0.5)
    assert np.all(mask == 0)
    assert views.INDICES["ndwi"] == ("led_13", "led_16")


def test_index_with_a_vanishing_denominator_is_invalid() -> None:
    cube = cube_of(ALL, led_12=0.0, led_10=0.0)
    values, mask = views.index_of(cube, "led_12", "led_10")
    assert np.isnan(values[0, 0])
    assert mask[0, 0] == MaskBit.SMALL_DENOMINATOR


def test_index_denominator_is_tested_against_one_millionth() -> None:
    cube = cube_of(ALL, led_12=np.float32(3e-6), led_10=np.float32(-1e-6))
    values, mask = views.index_of(cube, "led_12", "led_10")
    assert values[0, 0] == pytest.approx(2.0), "|a + b| = 2e-6 is enough"
    assert mask[0, 0] == 0
    cube = cube_of(ALL, led_12=np.float32(1.4e-6), led_10=np.float32(-1e-6))
    values, mask = views.index_of(cube, "led_12", "led_10")
    assert np.isnan(values[0, 0]), "|a + b| = 4e-7 is not"
    assert mask[0, 0] == MaskBit.SMALL_DENOMINATOR


def test_index_view_draws_pixels_without_a_value_in_grey() -> None:
    cube = cube_of(ALL, led_12=0.6, led_10=0.2)
    cube.values[0, 0, 11] = cube.values[0, 0, 9] = 0.0  # no denominator
    cube.mask[0, 1, 11] = MaskBit.SATURATED
    image = views.render(cube, "ndvi")
    assert image[0, 0].tolist() == image[0, 1].tolist() == list(views.NEUTRAL)
    assert image[5, 5].tolist() != list(views.NEUTRAL)


def test_index_scale_stays_minus_one_to_one_whatever_the_data() -> None:
    # Values between 0.2 and 0.3 only: a percentile stretch would spread them over the
    # whole colour table; the fixed scale keeps them near its middle.
    cube = cube_of(ALL, led_12=0.6 + 0.1 * ramp(), led_10=np.float32(0.4))
    image = views.render(cube, "ndvi")
    expected = views.LUTS["spectral"][int(np.floor(255 * (0.2 + 1) / 2 + 0.5))]
    assert image[0, 0].tolist() == expected.tolist()
    assert np.abs(image[0, -1].astype(int) - image[0, 0].astype(int)).max() < 40
    stretched = views.render(cube, "ndvi", low=0.2, high=0.2 + 7 / 85)
    assert stretched[0, 0].tolist() == views.LUTS["spectral"][0].tolist()


def test_index_mask_is_the_union_of_its_two_bands() -> None:
    cube = cube_of(ALL, led_12=0.6, led_10=0.2)
    cube.mask[0, 0, 11] = MaskBit.SATURATED
    cube.mask[0, 1, 9] = MaskBit.OUT_OF_RANGE
    _, mask = views.index_of(cube, "led_12", "led_10")
    assert mask[0, 0] == MaskBit.SATURATED
    assert mask[0, 1] == MaskBit.OUT_OF_RANGE


def test_index_view_uses_a_fixed_minus_one_to_one_scale_and_a_colour_table() -> None:
    cube = cube_of(ALL, led_12=ramp(), led_10=np.float32(0.5))
    image = views.render(cube, "ndvi")
    left = (0.0 - 0.5) / 0.5  # -1
    assert left == -1
    assert image[0, 0].tolist() == views.LUTS["spectral"][0].tolist()
    zero_column = int(np.argmin(np.abs(ramp()[0] - 0.5)))
    assert abs(int(image[0, zero_column, 0]) - int(views.LUTS["spectral"][128][0])) <= 12


# --- heat ------------------------------------------------------------------------------


def test_heat_is_the_mean_over_the_valid_bands() -> None:
    cube = cube_of([5, 7, 9], led_05=0.2, led_07=0.4, led_09=0.9)
    cube.mask[0, 0, 2] = MaskBit.SATURATED  # leaves 0.2 and 0.4
    cube.mask[0, 1, 1:] = MaskBit.SATURATED  # leaves one band: not enough
    image = views.render(cube, "heat", low=0, high=1, palette="gray")
    assert image[5, 5, 0] == 128, "mean of 0.2, 0.4, 0.9 is 0.5"
    assert image[0, 0, 0] == 77, "mean of 0.2 and 0.4 is 0.3"
    coloured = views.render(cube, "heat", low=0, high=1)
    assert coloured[0, 1].tolist() == list(views.NEUTRAL), "fewer than two valid bands: no value"
    assert coloured[0, 0].tolist() == views.LUTS["spectral"][77].tolist()


# --- principal components --------------------------------------------------------------


def structured_cube() -> Cube:
    rng = np.random.default_rng(1)
    strong = rng.normal(0, 1, (H, W)).astype(np.float32)
    weak = rng.normal(0, 0.1, (H, W)).astype(np.float32)
    return cube_of([5, 7, 9, 13], led_05=0.5 + 0.2 * strong, led_07=0.5 + 0.1 * strong,
                   led_09=0.5 - 0.2 * strong + weak, led_13=0.5 + weak)  # fmt: skip


def test_principal_components_are_centred_and_ordered() -> None:
    scores = views.principal_components(structured_cube())
    assert scores.shape == (H, W, 3)
    flat = scores.reshape(-1, 3)
    assert np.allclose(flat.mean(axis=0), 0, atol=1e-5)
    variances = flat.var(axis=0)
    assert variances[0] > variances[1] > variances[2]


@pytest.mark.parametrize("seed", range(8))
@pytest.mark.parametrize("flipped", [False, True])
def test_each_component_points_the_way_of_its_largest_loading(seed: int, flipped: bool) -> None:
    # One band carries most of the first component, with either sign of the others. The
    # rule (largest loading positive) then says: scores rise with that band, whichever
    # way the decomposition happened to come out.
    noise = np.random.default_rng(seed)
    strong = noise.normal(0, 1, (H, W)).astype(np.float32)
    weak = noise.normal(0, 0.05, (H, W, 3)).astype(np.float32)
    sign = -1.0 if flipped else 1.0
    cube = cube_of(
        [5, 7, 9, 13],
        led_05=0.5 + sign * 0.3 * strong,
        led_07=0.5 - sign * 0.1 * strong + weak[..., 0],
        led_09=0.5 - sign * 0.1 * strong + weak[..., 1],
        led_13=0.5 + weak[..., 2],
    )
    first = views.principal_components(cube)[..., 0].ravel()
    assert np.corrcoef(first, cube.values[..., 0].ravel())[0, 1] > 0.9


def test_pixels_invalid_in_any_band_get_no_component_scores() -> None:
    cube = structured_cube()
    cube.mask[3, 4, 1] = MaskBit.SATURATED
    scores = views.principal_components(cube)
    assert np.isnan(scores[3, 4]).all()
    assert np.isfinite(scores[3, 5]).all()
    image = views.render(cube, "pca")
    assert image.shape == (H, W, 3)


def test_principal_components_need_enough_bands() -> None:
    with pytest.raises(InvalidStateError):
        views.principal_components(cube_of([5, 7]))


@pytest.mark.parametrize("left", [0, 1, 2])
def test_a_scan_that_clipped_almost_everywhere_has_no_components(left: int) -> None:
    # Seen on the device: PWM chosen for a dimmer state of the lamp board left two pixels
    # that were valid in every band. Fewer pixels than components cannot be decomposed.
    cube = structured_cube()
    cube.mask[:, :, 0] = MaskBit.SATURATED
    cube.mask.reshape(-1, cube.mask.shape[2])[:left, 0] = 0
    for call in (lambda: views.principal_components(cube), lambda: views.render(cube, "pca")):
        with pytest.raises(InvalidStateError) as caught:
            call()
        assert caught.value.details == {"valid_pixels": left, "pixels": H * W, "bands": 4}
        assert "saturated" in (caught.value.hint or "")


def test_three_valid_pixels_are_enough_for_three_components() -> None:
    cube = structured_cube()
    cube.mask[:, :, 0] = MaskBit.SATURATED
    cube.mask.reshape(-1, cube.mask.shape[2])[:3, 0] = 0
    scores = views.principal_components(cube)
    assert np.isfinite(scores).all(axis=2).sum() == 3


# --- quality overlay -------------------------------------------------------------------


def test_overlay_marks_problem_pixels_and_leaves_the_rest_transparent() -> None:
    mask = np.zeros((4, 4), np.uint8)
    mask[0, 0] = MaskBit.RED_CLIPPED | MaskBit.SATURATED
    mask[0, 2] = MaskBit.SCALAR_CLIPPED | MaskBit.RED_CLIPPED | MaskBit.SATURATED
    mask[2, 0] = MaskBit.SMALL_DENOMINATOR
    mask[2, 2] = MaskBit.OUT_OF_RANGE  # kept in statistics: not marked
    overlay = views.quality_overlay(mask)
    assert overlay.shape == (4, 4, 4)
    assert overlay[0, 0].tolist() == [255, 0, 0, 160]
    assert overlay[0, 2].tolist() == [255, 255, 255, 160], "scalar clipping wins"
    assert overlay[2, 0].tolist() == [128, 128, 128, 160]
    assert overlay[2, 2, 3] == 0
    assert overlay[1, 1, 3] == 0
