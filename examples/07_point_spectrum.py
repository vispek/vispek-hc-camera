# SPDX-License-Identifier: Apache-2.0
"""Read the spectrum of a point and of a box.

Hardware: none (a scene recorded on the simulated device).
    python examples/07_point_spectrum.py [-o work]
"""

import argparse
from pathlib import Path

import vispek_hc
from vispek_hc.spectra import write_csv

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))

# Pixel coordinates: x is the column, y the row, origin top left. The simulated scene
# says where its patches are; with your own capture, give your own coordinates.
x, y, w, h = vispek_hc.samples.PATCHES["leaf"]
point = vispek_hc.Region("leaf centre", {"point": [x + w // 2, y + h // 2]})
box = vispek_hc.Region("leaf", {"rect": [x, y, w, h]})

print(f"{'band':8} {'nm':>5} {'point':>8} {'box mean':>9} {'box std':>8} {'valid':>6}")
for at_point, in_box in zip(
    vispek_hc.spectrum(cube, point), vispek_hc.spectrum(cube, box), strict=True
):
    value = "-" if at_point.mean is None else f"{at_point.mean:.4f}"
    mean = "-" if in_box.mean is None else f"{in_box.mean:.4f}"
    std = "-" if in_box.std is None else f"{in_box.std:.4f}"
    print(f"{in_box.band_id:8} {in_box.center_nm:5d} {value:>8} {mean:>9} {std:>8} "
          f"{in_box.valid_fraction:6.0%}")  # fmt: skip
print("wrote", write_csv(args.output / "spectra.csv", cube, [point, box]))
