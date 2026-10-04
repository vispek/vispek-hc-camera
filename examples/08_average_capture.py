# SPDX-License-Identifier: Apache-2.0
"""Average several scans into one cube, in one recording.

Hardware: a complete HC1500 in a dark box (or --simulate).
    python examples/08_average_capture.py [--simulate] [--scans 4] [-o averaged]
"""

import argparse
from pathlib import Path

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--scans", type=int, default=4)
parser.add_argument("-o", "--output", type=Path, default=Path("averaged"))
args = parser.parse_args()

settings = vispek_hc.ScanSettings(channels=(5, 9, 13), average=args.scans)
with vispek_hc.open_session(simulate=args.simulate) as session:
    session.pair_probe()  # measures how long light takes to show up in the frames
    capture = vispek_hc.record(session, args.output, settings, overwrite=True)

# Every scan is on disk as its own group; the average is formed when the cube is loaded.
print("groups on disk:", [group["group_id"] for group in capture.groups])
cube = capture.cube()
print("averaged cube from", cube.metadata["group_ids"], "->", cube.values.shape)
print("mean per band:", cube.values.reshape(-1, len(cube.bands)).mean(axis=0).round(2).tolist())
