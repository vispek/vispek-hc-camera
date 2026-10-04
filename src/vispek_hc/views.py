# SPDX-License-Identifier: Apache-2.0
"""Pictures of a cube: single bands, colour composites, indices, principal components.

A view is for looking, not for measuring: it is stretched per band between the 1st and
99th percentile of the valid pixels unless a fixed range is given. Indices and anything
that compares bands need calibrated (L2) data; on raw data they would only show that the
LEDs were driven at different power.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image, ImageDraw

from vispek_hc.cube import Cube
from vispek_hc.errors import ConfigRejectedError, InvalidStateError
from vispek_hc.framecheck import MaskBit

Palette = Literal["auto", "gray", "spectral", "viridis", "classes"]
COMPOSITES = {
    "rgb": ("led_09", "led_07", "led_05"),  # 630 / 560 / 455 nm
    "cir": ("led_13", "led_10", "led_07"),  # 850 / 667 / 560 nm
    "uv": ("led_04", "led_03", "led_02"),  # 400 / 370 / 275 nm
}
INDICES = {
    "ndvi": ("led_12", "led_10"),  # (804 - 667) / (804 + 667)
    # (850 - 973) / (850 + 973): the 970 nm water absorption band. Not the green/NIR or
    # NIR/SWIR indices that also go by "NDWI".
    "ndwi": ("led_13", "led_16"),
}
NEEDS_CALIBRATION = {"ndvi", "ndwi", "pca", "heat"}
BANNER = "UNCALIBRATED - not comparable"
EPSILON = 1e-6
NEUTRAL = (128, 128, 128)  # pixels without a value, in views drawn through a colour table


def _ramp(anchors: list[tuple[int, int, int]]) -> NDArray[np.uint8]:
    """A 256-entry colour table through evenly spaced anchor colours."""
    stops = np.linspace(0, 255, len(anchors))
    table = [np.interp(np.arange(256), stops, [a[c] for a in anchors]) for c in range(3)]
    return np.stack(table, axis=1).round().astype(np.uint8)


# Anchor colours: viridis from matplotlib (CC0), "Spectral" from ColorBrewer 2.0
# (Apache-2.0), the class colours from Tableau / D3 "category20". See
# THIRD-PARTY-NOTICES.md.
LUTS: dict[str, NDArray[np.uint8]] = {
    "gray": _ramp([(0, 0, 0), (255, 255, 255)]),
    "viridis": _ramp(
        [
            (68, 1, 84),
            (71, 44, 122),
            (59, 81, 139),
            (44, 113, 142),
            (33, 144, 141),
            (39, 173, 129),
            (92, 200, 99),
            (170, 220, 50),
            (253, 231, 37),
        ]
    ),
    "spectral": _ramp(
        [
            (94, 79, 162),
            (50, 136, 189),
            (102, 194, 165),
            (171, 221, 164),
            (230, 245, 152),
            (254, 224, 139),
            (253, 174, 97),
            (244, 109, 67),
            (213, 62, 79),
            (158, 1, 66),
        ]
    ),
}
CLASS_COLOURS = np.array(
    [(31, 119, 180), (255, 127, 14), (44, 160, 44), (214, 39, 40), (148, 103, 189),
     (140, 86, 75), (227, 119, 194), (127, 127, 127), (188, 189, 34), (23, 190, 207),
     (174, 199, 232), (255, 187, 120), (152, 223, 138), (255, 152, 150), (197, 176, 213),
     (196, 156, 148), (247, 182, 210), (199, 199, 199), (219, 219, 141), (158, 218, 229)],
    np.uint8,
)  # fmt: skip


def stretch(
    values: NDArray[np.float32],
    valid: NDArray[np.bool_],
    *,
    low: float | None = None,
    high: float | None = None,
    gamma: float = 1.0,
    fallback: tuple[float, float] = (0.0, 1.0),
) -> NDArray[np.uint8]:
    """Map values to 0-255: ``255 * clip((v - low) / (high - low), 0, 1) ** (1 / gamma)``.

    Without a fixed range the 1st and 99th percentile of the valid pixels are used, or
    ``fallback`` when those do not span a range.
    """
    if not 0.1 <= gamma <= 5:
        raise ConfigRejectedError(f"gamma {gamma} is outside 0.1-5", details={"gamma": gamma})
    low, high = _limits(values, valid, low, high, fallback)
    scaled = np.clip((np.nan_to_num(values, nan=low) - low) / (high - low), 0, 1)
    return np.asarray(np.floor(255 * scaled ** (1 / gamma) + 0.5), np.uint8)


def _limits(
    values: NDArray[np.float32],
    valid: NDArray[np.bool_],
    low: float | None,
    high: float | None,
    fallback: tuple[float, float],
) -> tuple[float, float]:
    """The display range: what was given, else the 1st and 99th percentile of the valid
    pixels, else ``fallback`` when those do not span a range."""
    if low is None or high is None:
        chosen = values[valid]
        spread = np.percentile(chosen, [1, 99]) if chosen.size else fallback
        auto_low, auto_high = spread if spread[1] > spread[0] else fallback
        low = auto_low if low is None else low
        high = auto_high if high is None else high
    if high <= low:
        raise ConfigRejectedError("the display range is empty", details={"low": low, "high": high})
    return float(low), float(high)


def _unit_range(cube: Cube) -> tuple[float, float]:
    return (0.0, 1.0) if cube.metadata["unit"] == "relative_to_white" else (0.0, 255.0)


def _region(cube: Cube, roi_px: tuple[int, int, int, int] | None) -> NDArray[np.bool_]:
    inside = np.zeros(cube.values.shape[:2], bool)
    if roi_px is None:
        inside[:] = True
    else:
        inside[roi_px[1] : roi_px[3], roi_px[0] : roi_px[2]] = True
    return inside


def index_of(cube: Cube, first: str, second: str) -> tuple[NDArray[np.float32], NDArray[np.uint8]]:
    """Normalised difference ``(a - b) / (a + b)`` of two bands, with its mask."""
    a = cube.values[:, :, cube.band_index(first)]
    b = cube.values[:, :, cube.band_index(second)]
    mask = cube.mask[:, :, cube.band_index(first)] | cube.mask[:, :, cube.band_index(second)]
    total = a + b
    usable = np.abs(total) > EPSILON
    result = np.full(a.shape, np.nan, np.float32)
    np.divide(a - b, total, out=result, where=usable & np.isfinite(total))
    mask = mask | (~usable * np.uint8(MaskBit.SMALL_DENOMINATOR))
    return result, mask.astype(np.uint8)


def principal_components(cube: Cube, count: int = 3) -> NDArray[np.float32]:
    """Scores of the first principal components, shape (H, W, count).

    Fitted on the pixels that are valid in every band, mean removed, not scaled. Each
    component's sign is chosen so that its largest loading is positive. Other pixels are NaN.
    """
    height, width, bands = cube.values.shape
    usable = cube.valid.all(axis=2)
    spectra = cube.values[usable].astype(np.float64)
    if len(spectra) < count or bands < count:
        raise InvalidStateError(
            f"principal components need {count} bands and {count} pixels that are valid in "
            f"every band; this capture has {bands} bands and {len(spectra)} such pixels",
            hint="a band that is saturated or masked everywhere leaves none: look at the "
            "single bands, lower the PWM and scan again",
            details={"valid_pixels": len(spectra), "pixels": height * width, "bands": bands},
        )
    centred = spectra - spectra.mean(axis=0)
    _, _, loadings = np.linalg.svd(centred, full_matrices=False)
    loadings = loadings[:count]
    largest = loadings[np.arange(count), np.abs(loadings).argmax(axis=1)]
    loadings *= np.sign(largest)[:, None]
    scores = np.full((height, width, count), np.nan, np.float32)
    scores[usable] = centred @ loadings.T
    return scores


def render(
    cube: Cube,
    mode: str,
    *,
    low: float | None = None,
    high: float | None = None,
    gamma: float = 1.0,
    palette: Palette = "auto",
    roi_px: tuple[int, int, int, int] | None = None,
    allow_uncalibrated: bool = False,
    banner: bool = True,
) -> NDArray[np.uint8]:
    """Render one view as an RGB picture (H, W, 3).

    ``mode`` is ``band:led_NN``, ``rgb``, ``cir``, ``uv``, ``ndvi``, ``ndwi``, ``pca`` or
    ``heat``. Bands are always named explicitly; a missing band is an error, never a
    substitute. The stretch is taken inside ``roi_px``; without one, inside the white
    region of the calibration the cube was made with, if that had one. In index, heat and
    component views, pixels without a value are drawn grey, not at an end of the scale.
    A picture of raw data carries a red strip that says so; ``banner=False`` leaves it
    out, for a program that marks raw data in its own way.
    """
    calibrated = _check_layer(cube, mode, allow_uncalibrated)
    inside, limits = _stretch_region(cube, roi_px), _unit_range(cube)
    cube_valid = cube.valid  # computed once: it looks at every value of the cube

    def scaled(
        values: NDArray[np.float32],
        valid: NDArray[np.bool_],
        fallback: tuple[float, float] = limits,
        fixed: tuple[float | None, float | None] = (None, None),
    ) -> NDArray[np.uint8]:
        chosen = fixed if low is None and high is None else (low, high)
        return stretch(
            values, valid & inside, low=chosen[0], high=chosen[1], gamma=gamma, fallback=fallback
        )

    if mode in COMPOSITES:
        picks = [cube.band_index(band_id) for band_id in COMPOSITES[mode]]
        image = np.stack([scaled(cube.values[:, :, i], cube_valid[:, :, i]) for i in picks], axis=2)
    elif mode == "pca":
        scores = principal_components(cube)
        layers = [scores[:, :, i] for i in range(3)]
        image = np.stack([scaled(v, np.isfinite(v), fallback=(-1.0, 1.0)) for v in layers], axis=2)
        image[~np.isfinite(scores[:, :, 0])] = NEUTRAL
    else:
        values, valid, fixed, default = _scalar(cube, mode, cube_valid)
        levels = scaled(values, valid, fixed=fixed)
        image = colourise(levels, default if palette == "auto" else palette)
        if not mode.startswith("band:"):
            image[~valid] = NEUTRAL
    return _with_banner(image) if banner and not calibrated else image


def _check_layer(cube: Cube, mode: str, allow_uncalibrated: bool) -> bool:
    """Whether the cube is calibrated; a view that needs that refuses raw data."""
    calibrated = bool(cube.metadata["layer"] == "L2")
    if mode in NEEDS_CALIBRATION and not calibrated and not allow_uncalibrated:
        raise InvalidStateError(
            f"the '{mode}' view needs calibrated data",
            hint="give a calibration (--calib). On raw data this view only shows that the "
            "LEDs were driven at different power; --allow-uncalibrated renders it anyway",
        )
    return calibrated


def _stretch_region(cube: Cube, roi_px: tuple[int, int, int, int] | None) -> NDArray[np.bool_]:
    """Where the display range is taken: the given region, else the white region of the
    calibration the cube was made with, else the whole picture."""
    if roi_px is None and (stored := (cube.metadata.get("calibration") or {}).get("white_roi_px")):
        roi_px = (stored["x0"], stored["y0"], stored["x1"], stored["y1"])
    return _region(cube, roi_px)


def _scalar(
    cube: Cube, mode: str, cube_valid: NDArray[np.bool_]
) -> tuple[NDArray[np.float32], NDArray[np.bool_], tuple[float | None, float | None], str]:
    """A view drawn through a colour table: its values, its valid pixels, its fixed range
    (if it has one) and its usual colour table."""
    if mode.startswith("band:"):
        position = cube.band_index(mode.removeprefix("band:"))
        return cube.values[:, :, position], cube_valid[:, :, position], (None, None), "gray"
    if mode in INDICES:
        values, mask = index_of(cube, *INDICES[mode])
        return values, np.isfinite(values) & ((mask & 7) == 0), (-1.0, 1.0), "spectral"
    if mode == "heat":
        counted = cube_valid.sum(axis=2)
        summed = np.where(cube_valid, cube.values, 0).sum(axis=2)
        mean = np.where(counted >= 2, summed / np.maximum(counted, 1), np.nan)
        return mean.astype(np.float32), counted >= 2, (None, None), "spectral"
    raise ConfigRejectedError(
        f"unknown view '{mode}'",
        hint="use rgb, cir, uv, ndvi, ndwi, pca, heat or band:led_NN",
    )


def view_range(
    cube: Cube,
    mode: str,
    *,
    low: float | None = None,
    high: float | None = None,
    roi_px: tuple[int, int, int, int] | None = None,
    allow_uncalibrated: bool = False,
) -> tuple[float, float] | None:
    """The values :func:`render` maps onto the two ends of a view's colour table, for a
    scale beside the picture. ``None`` for views made of three bands (``rgb``, ``cir``,
    ``uv``, ``pca``), which have no single scale."""
    _check_layer(cube, mode, allow_uncalibrated)
    if mode in COMPOSITES or mode == "pca":
        return None
    values, valid, fixed, _ = _scalar(cube, mode, cube.valid)
    chosen = fixed if low is None and high is None else (low, high)
    return _limits(
        values, valid & _stretch_region(cube, roi_px), chosen[0], chosen[1], _unit_range(cube)
    )


def colourise(levels: NDArray[np.uint8], palette: str) -> NDArray[np.uint8]:
    """Apply a colour table to 0-255 levels; ``classes`` cycles through 20 distinct colours."""
    if palette == "classes":
        return np.asarray(CLASS_COLOURS[levels % len(CLASS_COLOURS)])
    if palette not in LUTS:
        raise ConfigRejectedError(
            f"unknown palette '{palette}'", details={"available": [*LUTS, "classes"]}
        )
    return np.asarray(LUTS[palette][levels])


def _with_banner(image: NDArray[np.uint8]) -> NDArray[np.uint8]:
    picture = Image.fromarray(image)
    draw = ImageDraw.Draw(picture)
    height = min(14, picture.height)
    draw.rectangle((0, 0, picture.width, height), fill=(160, 0, 0))
    draw.text((3, 1), BANNER, fill=(255, 255, 255))
    return np.asarray(picture)


def quality_overlay(mask: NDArray[np.uint8]) -> NDArray[np.uint8]:
    """An RGBA layer that marks clipped and invalid pixels, to draw over a view.

    Scalar clipping wins (white), then the clipped colour channel (red, green, blue);
    pixels invalid for another reason are grey. Marked on a checker pattern so the picture
    stays visible, and kept apart from the picture itself.
    """
    overlay = np.zeros((*mask.shape, 4), np.uint8)
    rules = [
        (MaskBit.INVALID | MaskBit.SMALL_DENOMINATOR, (128, 128, 128)),
        (MaskBit.BLUE_CLIPPED, (0, 0, 255)),
        (MaskBit.GREEN_CLIPPED, (0, 255, 0)),
        (MaskBit.RED_CLIPPED, (255, 0, 0)),
        (MaskBit.SCALAR_CLIPPED, (255, 255, 255)),
    ]
    rows, columns = np.indices(mask.shape)
    checker = (rows + columns) % 2 == 0
    for bits, colour in rules:
        marked = ((mask & bits) != 0) & checker
        overlay[marked] = (*colour, 160)
    return overlay


def save_png(image: NDArray[np.uint8], path: str | Path) -> Path:
    path = Path(path)
    Image.fromarray(image).save(path, "PNG")
    return path
