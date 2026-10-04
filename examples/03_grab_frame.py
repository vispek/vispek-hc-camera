# SPDX-License-Identifier: Apache-2.0
"""Take one frame from the camera and save it as PNG.

Hardware: the camera on USB3 and the light source on USB1 (or --simulate).
    python examples/03_grab_frame.py [--simulate] [-o frame.png]
"""

import argparse
from pathlib import Path

from vispek_hc import open_session, save_png, video

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
parser.add_argument("-o", "--output", type=Path, default=Path("frame.png"))
args = parser.parse_args()

with open_session(simulate=args.simulate) as session:
    session.light.on(6, 50)  # 520 nm, low power: something to see
    try:
        frame = session.frames.wait_new(4)  # frames that arrive after this call
    finally:
        session.light.off(6)  # every ON gets its OFF, whatever happens in between
    rgb = video.decode(frame)  # (height, width, 3) uint8, raw code values

save_png(rgb, args.output)
print(f"frame {frame.seq}: {frame.width}x{frame.height} {frame.pixel_format} -> {args.output}")
print("mean code value per channel (R, G, B):", rgb.reshape(-1, 3).mean(axis=0).round(1).tolist())
