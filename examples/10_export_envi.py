# SPDX-License-Identifier: Apache-2.0
"""Export a calibrated cube for other tools: ENVI and multi-page TIFF.

Hardware: none (a scene recorded on the simulated device).
    python examples/10_export_envi.py [-o work]
"""

import argparse
from pathlib import Path

import numpy as np

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("-o", "--output", type=Path, default=Path("work"))
args = parser.parse_args()

sample, white = vispek_hc.example_captures(args.output)
cube = vispek_hc.apply_calibration(sample, vispek_hc.make_calibration([white]))

files = vispek_hc.export_envi(cube, args.output / "envi")
print("ENVI:", ", ".join(path.name for path in files))
print((args.output / "envi" / "data.hdr").read_text("ascii"))

# data.bsq is plain little-endian float32, band sequential: any tool can read it.
height, width, bands = cube.values.shape
again = np.fromfile(args.output / "envi" / "data.bsq", "<f4").reshape(bands, height, width)
print("read back identical:", np.array_equal(again, cube.values.transpose(2, 0, 1), equal_nan=True))

data, mask = vispek_hc.export_tiff(cube, args.output / "tiff")
print("TIFF:", data.name, "+", mask.name)
