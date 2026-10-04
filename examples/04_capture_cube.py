# SPDX-License-Identifier: Apache-2.0
"""Record one multispectral scan and look at the cube.

Hardware: a complete HC1500 in a dark box (or --simulate).
    python examples/04_capture_cube.py [--simulate] [-o first-scan]
"""

import argparse
from pathlib import Path

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
parser.add_argument("-o", "--output", type=Path, default=Path("first-scan"))
args = parser.parse_args()

# One call: open, lock the camera parameters, scan LEDs 5-17, save, close.
capture = vispek_hc.capture(args.output, simulate=args.simulate, overwrite=True)
cube = capture.cube()

print(f"capture {capture.capture_id} ({capture.status}) in {capture.path}")
print("values:", cube.values.shape, cube.values.dtype, "- axes Y, X, band; raw code values")
print("valid pixels:", f"{cube.valid.mean():.1%}")
for band in cube.bands:
    mean = cube.values[:, :, cube.band_index(band.band_id)].mean()
    print(f"  {band.band_id} {band.center_nm:5d} nm  {band.dominant_channel}  mean {mean:6.1f}")
vispek_hc.save_png(vispek_hc.render(cube, "rgb"), capture.path / "rgb.png")
print("wrote", capture.path / "rgb.png", "(uncalibrated: see 05_reflectance.py)")
