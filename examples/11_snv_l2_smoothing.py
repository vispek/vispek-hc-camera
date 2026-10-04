# SPDX-License-Identifier: Apache-2.0
"""Per-pixel spectrum operators: SNV, L2 normalisation, moving average along wavelength.

Hardware: none (a scene recorded on the simulated device). Operators are plain functions on
the (H, W, bands) array; chain them in the order you need.
    python examples/11_snv_l2_smoothing.py [-o work]
"""

import argparse
from pathlib import Path

import numpy as np

import vispek_hc


def snv(values: np.ndarray, min_std: float = 1e-6) -> np.ndarray:
    """Standard normal variate: each spectrum minus its mean, over its standard deviation."""
    mean = np.nanmean(values, axis=2, keepdims=True)
    std = np.nanstd(values, axis=2, keepdims=True)
    return np.where(std > min_std, (values - mean) / np.maximum(std, min_std), np.nan)


def l2_normalise(values: np.ndarray) -> np.ndarray:
    norm = np.sqrt(np.nansum(values**2, axis=2, keepdims=True))
    return np.where(norm > 0, values / np.maximum(norm, 1e-12), np.nan)


def moving_average(values: np.ndarray, width: int = 3) -> np.ndarray:
    """Mean over `width` neighbouring bands (bands must be in wavelength order)."""
    if width % 2 != 1:
        raise ValueError("the window must be an odd number of bands")
    padded = np.pad(values, ((0, 0), (0, 0), (width // 2, width // 2)), mode="edge")
    windows = np.lib.stride_tricks.sliding_window_view(padded, width, axis=2)
    return windows.mean(axis=-1)


parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))
values = np.where(cube.valid, cube.values, np.nan)  # invalid pixels drop out as NaN
left, top, width, height = vispek_hc.samples.PATCHES["leaf"]  # a pixel with a spectrum
y, x = top + height // 2, left + width // 2

for name, result in (
    ("relative reflectance", values),
    ("moving average", moving_average(values)),
    ("L2 normalised", l2_normalise(values)),
    ("SNV", snv(values)),
):
    print(f"{name:22}", [round(float(value), 3) for value in result[y, x]])
