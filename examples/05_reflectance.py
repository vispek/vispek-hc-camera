# SPDX-License-Identifier: Apache-2.0
"""Turn a sample scan into relative reflectance with a white reference.

Hardware: none. Uses a scene recorded on the simulated device (invented spectra).
    python examples/05_reflectance.py [-o work]
"""

import argparse
from pathlib import Path

import numpy as np

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)

# The calibration holds the white frames, their dark frame and the settings they are
# bound to. Applying it checks that the sample was scanned with the same settings.
calibration = vispek_hc.make_calibration([white])
calibration.save(args.output / "calibration")
cube = vispek_hc.apply_calibration(sample, calibration)  # (S - D_S) / (W - D_W), layer L2
cube.save(args.output / "cube.npz")

print("layer", cube.metadata["layer"], "unit", cube.metadata["unit"])
for index, band in enumerate(cube.bands):
    values = cube.values[:, :, index][cube.valid[:, :, index]]
    print(f"  {band.band_id} {band.center_nm:5d} nm  median {np.median(values):.4f}")
print("wrote", args.output / "cube.npz")
