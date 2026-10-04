# SPDX-License-Identifier: Apache-2.0
"""Blink one visible LED three times.

Hardware: the light source on USB1 (or --simulate). LEDs 1-4 are ultraviolet and are
refused unless you open the session with allow_uv=True; read SAFETY.md first.
    python examples/02_blink_led.py [--simulate] [--led 9] [--pwm 36]
"""

import argparse

from vispek_hc import Illuminator, SimulatedRig

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
parser.add_argument("--led", type=int, default=9)
parser.add_argument("--pwm", type=int, default=36)
args = parser.parse_args()

if args.simulate:
    light = SimulatedRig().light
    light.connect(baud=115200)
else:
    light = Illuminator.open()  # finds the port, settles the baud rate, switches all off

with light:  # leaving this block switches every LED off, whatever happens
    for _ in range(3):
        acknowledged = light.on(args.led, args.pwm)
        print(f"LED {args.led} on, acknowledged after {acknowledged.duration_s * 1000:.0f} ms")
        light.clock.sleep(0.3)
        light.off(args.led)
        light.clock.sleep(0.3)
print("all LEDs off")
