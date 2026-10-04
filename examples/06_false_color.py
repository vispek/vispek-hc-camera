# SPDX-License-Identifier: Apache-2.0
"""Render colour composites and an index picture from a calibrated cube.

Hardware: none (a scene recorded on the simulated device).
    python examples/06_false_color.py [-o work]
"""

import argparse
from pathlib import Path

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
calibration = vispek_hc.make_calibration([white])
cube = vispek_hc.apply_calibration(sample, calibration)

# rgb: 630 / 560 / 455 nm. cir: 850 / 667 / 560 nm. ndvi: (804 - 667) / (804 + 667).
# The stretch is taken inside the white region when the calibration has one.
for mode in ("rgb", "cir", "ndvi", "band:led_13"):
    image = vispek_hc.render(cube, mode, roi_px=calibration.roi_px)
    path = vispek_hc.save_png(image, args.output / f"{mode.replace(':', '_')}.png")
    print("wrote", path)

# An index on raw data is refused: it would only show how hard each LED was driven.
try:
    vispek_hc.render(sample.cube(), "ndvi")
except vispek_hc.VispekHCError as error:
    print(f"as expected: [{error.code}] {error.message}")
