# SPDX-License-Identifier: Apache-2.0
"""List the serial ports and cameras that could be an HC1500.

Hardware: none needed; shows what is attached. --simulate shows the simulated device.
    python examples/01_list_devices.py [--simulate]
"""

import argparse

from vispek_hc import HC1500, illuminator, open_session, video

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
args = parser.parse_args()

if args.simulate:
    with open_session(simulate=True) as session:
        print("simulated device:", session.describe())
else:
    print("serial ports that could be the light source:", illuminator.find_ports() or "none")
    try:
        cameras = video.list_cameras()
    except Exception as error:  # ffmpeg missing: say so and go on
        cameras = []
        print("cannot list cameras:", error)
    for name in cameras:
        marker = "  <- HC1500 camera" if name == HC1500.camera_name else ""
        print(f"camera: {name}{marker}")
