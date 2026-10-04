# SPDX-License-Identifier: Apache-2.0
"""Spectra of points and regions, and their CSV form."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import numpy as np
import pytest

from vispek_hc.cube import Cube, build_cube
from vispek_hc.errors import ConfigRejectedError
from vispek_hc.framecheck import MaskBit
from vispek_hc.models import HC1500
from vispek_hc.spectra import CSV_COLUMNS, Region, spectrum, write_csv

H, W = 10, 12


def cube() -> Cube:
    leds = [HC1500.led(9), HC1500.led(5)]  # deliberately not in wavelength order
    frames = [np.full((H, W, 3), 10, np.uint8) for _ in leds]
    made = build_cube(frames, np.full((H, W, 3), 5, np.uint8), leds, "dominant",
                      {"capture_id": "abc", "group_ids": ["g0001", "g0002"]})  # fmt: skip
    made.values[:, :, 0] = np.arange(H * W, dtype=np.float32).reshape(H, W)
    made.values[:, :, 1] = 0.5
    return made


def test_point_is_exactly_one_pixel() -> None:
    (red,) = [b for b in spectrum(cube(), Region("p", {"point": [3, 2]})) if b.band_id == "led_09"]
    assert (red.count, red.total, red.valid_fraction) == (1, 1, 1.0)
    assert red.mean == red.median == red.min == red.max == 2 * W + 3
    assert red.std == 0.0


def test_rectangle_is_half_open_x_then_y() -> None:
    red = spectrum(cube(), Region("r", {"rect": [1, 2, 3, 2]}))[1]
    expected = np.array([[25, 26, 27], [37, 38, 39]], float)  # rows 2-3, columns 1-3
    assert red.band_id == "led_09"
    assert red.total == 6
    assert red.mean == expected.mean()
    assert red.std == pytest.approx(expected.std(ddof=0))
    assert red.median == np.median(expected)
    assert (red.min, red.max) == (25, 39)


def test_rectangle_is_clipped_to_the_picture() -> None:
    corner = spectrum(cube(), Region("r", {"rect": [-5, -5, 7, 7]}))[1]
    assert corner.total == 4, "columns 0-1, rows 0-1"


def test_polygon_contains_the_pixels_whose_centre_is_inside() -> None:
    triangle = Region("t", {"polygon": [[0, 0], [4, 0], [0, 4]]})
    selected = triangle.pixels(H, W)
    assert selected.sum() == 6
    assert selected[0, :3].tolist() == [True, True, True]
    assert not selected[0, 3], "centre (3.5, 0.5) lies outside x + y < 4"
    assert selected[2, 0]
    assert not selected[2, 1]


def test_bands_come_in_wavelength_order() -> None:
    rows = spectrum(cube(), Region("p", {"point": [0, 0]}))
    assert [(row.band_id, row.center_nm) for row in rows] == [("led_05", 455), ("led_09", 630)]


def test_invalid_pixels_are_left_out_of_the_statistics() -> None:
    made = cube()
    made.mask[0, 0, 0] = MaskBit.SATURATED
    made.values[0, 1, 0] = np.nan
    made.mask[0, 2, 0] = MaskBit.OUT_OF_RANGE  # stays in
    red = spectrum(made, Region("r", {"rect": [0, 0, 4, 1]}))[1]
    assert (red.count, red.total) == (2, 4)
    assert red.valid_fraction == 0.5
    assert red.mean == 2.5, "pixels 2 and 3"


def test_band_without_a_valid_pixel_has_undefined_statistics() -> None:
    made = cube()
    made.mask[:, :, 0] = MaskBit.SMALL_DENOMINATOR
    red = spectrum(made, Region("p", {"point": [0, 0]}))[1]
    assert (red.count, red.valid_fraction) == (0, 0.0)
    assert red.mean is red.std is red.median is red.min is red.max is None


@pytest.mark.parametrize("geometry", [{"point": [99, 0]}, {"rect": [20, 20, 2, 2]}])
def test_regions_outside_the_picture_are_refused(geometry: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ConfigRejectedError, match="contains no pixel"):
        spectrum(cube(), Region("bad", geometry))


@pytest.mark.parametrize(
    "geometry",
    [
        {"circle": [1, 1, 1]},
        {},
        {"point": [1, 1], "rect": [0, 0, 2, 2]},
        {"point": [1.5, 1]},
        {"point": [1]},
        {"point": [True, 1]},
        {"rect": [0, 0, 2.5, 2]},
        {"rect": [0, 0, 0, 2]},
        {"rect": [0, 0, 2, -1]},
        {"rect": [0, 0, 2]},
        {"polygon": []},
        {"polygon": [[0, 0], [2, 2]]},
        {"polygon": [[0, 0], [2, 2], [2]]},
        {"polygon": [[0, 0], [2, 2], ["a", 1]]},
        {"polygon": "0,0 2,2 2,0"},
    ],
)
def test_malformed_regions_are_refused_when_they_are_made(geometry: dict) -> None:  # type: ignore[type-arg]
    with pytest.raises(ConfigRejectedError, match="is not a point"):
        Region("bad", geometry)


def test_polygon_corners_may_lie_between_pixels() -> None:
    region = Region("triangle", {"polygon": [[0.0, 0.0], [2.5, 0.0], [0.0, 2.5]]})
    inside = region.pixels(4, 4)
    assert inside[0, 0] and inside[0, 1] and inside[1, 0]
    assert not inside[1, 1], "its centre (1.5, 1.5) is outside"


def test_csv_has_the_documented_columns_and_leaves_undefined_values_empty(tmp_path: Path) -> None:
    made = cube()
    made.mask[:, :, 1] = MaskBit.SMALL_DENOMINATOR
    regions = [Region("board", {"rect": [0, 0, 2, 1]}), Region("pad", {"point": [3, 2]})]
    path = write_csv(tmp_path / "spectra.csv", made, regions)
    with path.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert tuple(rows[0]) == CSV_COLUMNS
    assert len(rows) == 4
    blue, red = rows[0], rows[1]
    assert (red["roi_id"], red["name"], red["band_id"], red["center_nm"]) == (
        "roi_01", "board", "led_09", "630",
    )  # fmt: skip
    assert json.loads(red["geometry_json"]) == {"rect": [0, 0, 2, 1]}
    assert (red["capture_id"], red["group_id"]) == ("abc", "g0001;g0002")
    assert (red["layer"], red["unit"], red["signal_domain"]) == (
        "L1", "image_code_value", "dominant_channel_v1",
    )  # fmt: skip
    assert (red["count"], red["total"], red["valid_fraction"]) == ("2", "2", "1")
    assert (red["mean"], red["std"], red["median"], red["min"], red["max"]) == (
        "0.5", "0.5", "0.5", "0", "1",
    )  # fmt: skip
    assert (blue["count"], blue["mean"], blue["max"]) == ("0", "", "")
    assert rows[2]["roi_id"] == "roi_02"


def test_csv_takes_1_to_32_regions(tmp_path: Path) -> None:
    point = Region("p", {"point": [0, 0]})
    with pytest.raises(ConfigRejectedError):
        write_csv(tmp_path / "s.csv", cube(), [])
    with pytest.raises(ConfigRejectedError):
        write_csv(tmp_path / "s.csv", cube(), [point] * 33)
    write_csv(tmp_path / "s.csv", cube(), [point] * 32)


def test_numpy_integers_are_whole_pixels_too() -> None:
    y, x = np.argwhere(np.ones((3, 3), bool))[4]  # numpy integers, as index arithmetic gives
    point = spectrum(cube(), Region("p", {"point": [x, y]}))
    assert point[0].total == 1
    box = spectrum(cube(), Region("b", {"rect": [np.int64(0), np.int64(0), np.int32(2), 3]}))
    assert box[0].total == 6
