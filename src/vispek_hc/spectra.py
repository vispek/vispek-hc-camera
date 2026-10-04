# SPDX-License-Identifier: Apache-2.0
"""Spectra of points and regions.

Pixel coordinates: origin top left, x is the column and y the row. Rectangles are
half-open, ``[x, x + w) x [y, y + h)``, and clipped to the picture. A polygon contains
the pixels whose centre ``(x + 0.5, y + 0.5)`` lies inside it (even-odd rule).
"""

from __future__ import annotations

import csv
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray

from vispek_hc.cube import INVALID, Cube
from vispek_hc.errors import ConfigRejectedError

MAX_REGIONS = 32
CSV_COLUMNS = (
    "capture_id", "group_id", "roi_id", "name", "geometry_json", "layer", "unit",
    "signal_domain", "band_id", "center_nm", "count", "total", "valid_fraction", "mean", "std",
    "median", "min", "max",
)  # fmt: skip


def _whole(numbers: Any, count: int) -> bool:
    return (
        isinstance(numbers, list | tuple)
        and len(numbers) == count
        and all(
            isinstance(number, int | np.integer) and not isinstance(number, bool)
            for number in numbers
        )
    )


@dataclass(frozen=True, slots=True)
class Region:
    """A point ``{"point": [x, y]}``, rectangle ``{"rect": [x, y, w, h]}`` or polygon
    ``{"polygon": [[x, y], ...]}``. Points and rectangles are whole pixels; polygon
    corners may lie anywhere."""

    name: str
    geometry: dict[str, Any]

    def __post_init__(self) -> None:
        geometry = self.geometry
        if not isinstance(geometry, dict) or len(geometry) != 1:
            good = False
        elif "point" in geometry:
            good = _whole(geometry["point"], 2)
        elif "rect" in geometry:
            good = _whole(geometry["rect"], 4) and min(geometry["rect"][2:]) > 0
        elif "polygon" in geometry:
            corners = geometry["polygon"]
            good = (
                isinstance(corners, list | tuple)
                and len(corners) >= 3
                and all(
                    isinstance(corner, list | tuple)
                    and len(corner) == 2
                    and all(isinstance(n, int | float) and not isinstance(n, bool) for n in corner)
                    for corner in corners
                )
            )
        else:
            good = False
        if not good:
            raise ConfigRejectedError(
                f"region '{self.name}' is not a point [x, y], a rect [x, y, w, h] of whole "
                "pixels, or a polygon of at least three [x, y] corners",
                details={"geometry": geometry},
            )

    def pixels(self, height: int, width: int) -> NDArray[np.bool_]:
        selected = np.zeros((height, width), bool)
        if "point" in self.geometry:
            x, y = self.geometry["point"]
            if 0 <= x < width and 0 <= y < height:
                selected[int(y), int(x)] = True
        elif "rect" in self.geometry:
            x, y, w, h = map(int, self.geometry["rect"])
            selected[max(0, y) : max(0, y + h), max(0, x) : max(0, x + w)] = True
        else:
            xs, ys = np.meshgrid(np.arange(width) + 0.5, np.arange(height) + 0.5)
            corners = self.geometry["polygon"]
            for (x1, y1), (x2, y2) in zip(corners, [*corners[1:], corners[0]], strict=True):
                if y1 == y2:
                    continue
                between = (ys >= min(y1, y2)) & (ys < max(y1, y2))
                crossing = x1 + (ys - y1) * (x2 - x1) / (y2 - y1)
                selected ^= between & (xs < crossing)
        if not selected.any():
            raise ConfigRejectedError(
                f"region '{self.name}' contains no pixel of the picture",
                details={"geometry": self.geometry},
            )
        return selected


@dataclass(frozen=True, slots=True)
class BandStatistics:
    band_id: str
    center_nm: int
    count: int  # valid pixels
    total: int  # pixels in the region
    valid_fraction: float
    mean: float | None  # None when no pixel is valid
    std: float | None  # population standard deviation (ddof = 0)
    median: float | None
    min: float | None
    max: float | None


def spectrum(cube: Cube, region: Region) -> list[BandStatistics]:
    """Statistics of the valid pixels of a region, one entry per band, in wavelength order."""
    selected = region.pixels(*cube.values.shape[:2])
    total = int(selected.sum())
    inside = cube.values[selected].astype(np.float64)  # (pixels, bands)
    valid = np.isfinite(inside) & ((cube.mask[selected] & INVALID) == 0)
    rows = []
    for index, band in sorted(enumerate(cube.bands), key=lambda item: item[1].center_nm):
        values = inside[:, index][valid[:, index]]
        numbers: list[float | None] = [None] * 5
        if values.size:
            numbers = [float(f(values)) for f in (np.mean, np.std, np.median, np.min, np.max)]
        rows.append(
            BandStatistics(
                band.band_id, band.center_nm, int(values.size), total, values.size / total, *numbers
            )
        )
    return rows


def write_csv(path: str | Path, cube: Cube, regions: Sequence[Region]) -> Path:
    """Write the spectra of up to 32 regions as CSV (UTF-8, '.' decimals, empty = undefined)."""
    path = Path(path)
    if not 1 <= len(regions) <= MAX_REGIONS:
        raise ConfigRejectedError(
            f"give 1 to {MAX_REGIONS} regions", details={"given": len(regions)}
        )
    meta = cube.metadata
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.writer(stream)
        writer.writerow(CSV_COLUMNS)
        for number, region in enumerate(regions, start=1):
            for band in spectrum(cube, region):
                statistics = [band.mean, band.std, band.median, band.min, band.max]
                writer.writerow(
                    [
                        meta.get("capture_id", ""),
                        ";".join(meta.get("group_ids", [])),
                        f"roi_{number:02d}",
                        region.name,
                        json.dumps(region.geometry),
                        meta["layer"],
                        meta["unit"],
                        meta["signal_domain"],
                        band.band_id,
                        band.center_nm,
                        band.count,
                        band.total,
                        f"{band.valid_fraction:.6g}",
                        *("" if value is None else f"{value:.6g}" for value in statistics),
                    ]
                )
    return path
