# SPDX-License-Identifier: Apache-2.0
"""Find a PWM per channel that makes the white board bright but not clipped.

Hardware: a complete HC1500 looking at the white board (or --simulate).
    python examples/09_auto_pwm.py [--simulate]
"""

import argparse

import vispek_hc

parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
parser.add_argument("--simulate", action="store_true")
args = parser.parse_args()

# Start far too bright on purpose; the search halves, then bisects (at most 10 tries per
# channel) until the 99th percentile of the white board lies between 150 and 235.
settings = vispek_hc.ScanSettings(channels=(5, 7, 9, 13), pwm=800, role="white")
with vispek_hc.open_session(simulate=args.simulate) as session:
    session.pair_probe()
    notes: list[str] = []
    chosen = vispek_hc.auto_pwm(session, settings, notes=notes)

for led_id, pwm in chosen.items():
    led = vispek_hc.HC1500.led(led_id)
    print(
        f"LED {led_id:2d} ({led.nm} nm): PWM {pwm}  (the model's first guess is {led.starting_pwm})"
    )
for note in notes:  # channels that never got into range say so here
    print("note:", note)
print("stored in the unit's profile for later scans (a simulated unit is not written to disk)")
