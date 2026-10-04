#!/usr/bin/env python3
"""Light one visible LED of the Vispek HC1500 at low brightness, take one frame, save a PNG.

    python led_and_frame.py --simulate            # simulated device, writes frame.png
    python led_and_frame.py -o out/red.png        # real hardware

Written from AGENTS.md of vispek-hc-camera only ("Using it from Python").
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import vispek_hc

# LED 9 is the 630 nm (red, visible) LED. LEDs 1-4 are ultraviolet and are never used here.
LED = 9
LED_WAVELENGTH_NM = 630
# PWM 36 of 1023 (about 3.5 % duty): the low value AGENTS.md itself uses for LED 9.
PWM = 36
PWM_MAX = 1023
# Take the newest of this many frames arriving after the LED was switched on,
# so the frame was exposed while the LED was lit.
FRAMES_TO_WAIT = 4


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            f"Switch LED {LED} ({LED_WAVELENGTH_NM} nm) on at PWM {PWM}/{PWM_MAX}, "
            "take one camera frame, switch the LED off and save the frame as a PNG."
        )
    )
    parser.add_argument(
        "--simulate",
        action="store_true",
        help="use the simulated HC1500 instead of real hardware",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="frame.png",
        help="path of the PNG to write (default: frame.png)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # AGENTS.md: "drop simulate with hardware" - so the keyword is only passed when asked for.
    session_kwargs = {"simulate": True} if args.simulate else {}

    try:
        # Leaving the session switches every LED off (second line of defence).
        with vispek_hc.open_session(**session_kwargs) as session:
            session.light.on(LED, PWM)
            try:
                frame = session.frames.wait_new(FRAMES_TO_WAIT)
            finally:
                # Runs whether or not the frame arrived.
                session.light.off(LED)
            rgb = vispek_hc.video.decode(frame)  # (H, W, 3) uint8 raw code values

        height, width = rgb.shape[:2]

        output = Path(args.output)
        if output.parent != Path("."):
            output.parent.mkdir(parents=True, exist_ok=True)
        vispek_hc.save_png(rgb, str(output))
    except vispek_hc.VispekHCError as err:
        print(f"error {err.code}: {err.message}", file=sys.stderr)
        if err.hint:
            print(f"hint: {err.hint}", file=sys.stderr)
        return 1

    print(
        f"LED {LED} ({LED_WAVELENGTH_NM} nm) at PWM {PWM}/{PWM_MAX}, "
        f"frame {width}x{height} px, saved to {output}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
