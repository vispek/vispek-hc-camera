# Acceptance records

What was checked, when, on what, and with which result. A change to an acquisition
default, or to how a scalar, a mask bit or a relative value is defined, needs a record
here and a "Defaults" entry in the changelog.

| Record | What it shows |
|---|---|
| [2026-10-02-first-device.md](2026-10-02-first-device.md) | First contact with a real HC1500: protocol, frames, lock, black level, timing. Three defaults changed |
| [2026-10-02-acceptance-leds-5-17.md](2026-10-02-acceptance-leds-5-17.md) | The acceptance run without the white board: 12 checks passed, a scan of LEDs 5-17 works, about 5 % of replies are lost whatever the timing, both pixel paths give black 5 and clip at 255. No default changed |
| [2026-10-02-white-board-and-camera-state.md](2026-10-02-white-board-and-camera-state.md) | The white-board checks on a sheet of paper: 4 passed. The camera carries a state over from the previous picture (LED 9: 118 or 141); the pause between channels became a whole dark frame, 170 ms. `dominant` confirmed as the default value of a band |
| [2026-10-02-kill-test.md](2026-10-02-kill-test.md) | A killed process leaves its LED lit until `led off --all`; SIGTERM switches it off; a background job of a shell script ignores SIGINT |
| [2026-10-02-uv-leds.md](2026-10-02-uv-leds.md) | The four UV LEDs light; a scan of all 17 channels takes about 19 s. On office paper the UV bands show the paper's fluorescence, not reflected ultraviolet |
| [2026-10-02-unplug-test.md](2026-10-02-unplug-test.md) | Camera cable pulled during a scan: LED off, confirmed, recording kept as partial. USB1 pulled, with USB2 connected or not: **the LED stays lit** (the camera cable powers it) and cannot be switched off until USB1 is back |
| [agent-m3/](agent-m3/README.md) | An agent that had only `AGENTS.md` and the documents it links to wrote a working "light an LED and take a frame" script |

## Where the hardware acceptance stands

One HC1500 on macOS (Apple silicon). The checks are in `tests/hardware/`, run as
[hardware-runbook.md](hardware-runbook.md) says, and rehearsed on the simulated device.
Before the first public release every row is to be done:

| | Check | State |
|---|---|---|
| Devices | `check` finds port and camera; the pairing probe pairs; the black level reads 5, 5, 5 | done |
| Light source | one visible LED switches on and off; reply delay and loss rate; the baud rate | done |
| Camera | parameters lock and read back; what `exposure-time-abs` the camera settles on | done |
| Pixel paths | dark frame and a clipped LED frame through `uncompressed` and `mjpeg`: both give black 5 and clip at 255. Decides full or limited range for the YUV conversion | done: limited range for uncompressed frames |
| Scan | LEDs 5-17; the selected frames are settled and lit | done |
| Timing | reply loss against the pause between commands (10 / 20 / 50 / 100 ms) and the reply timeout (100 / 200 / 300 / 600 ms) | done: the timeout stays; the pause became a whole dark frame for another reason (the camera's state) |
| White reference | the white board divided by itself; PWM per channel; camera channel per LED | done on white paper; **open** on a reflectance standard |
| Scalar | noise of the dominant-channel value against luma on the board; confirms or reverts the default `reduction` | done: `dominant` stays |
| LED table | order of LEDs 1-2 and 13-17, signed off by the hardware team; then `verified: true` | **open** |
| UV | the UV LEDs light and scan | done; they also light with USB2 unplugged |
| Brightness against the state of USB2 | every LED was several times brighter after USB2 was unplugged and the camera cable replugged | done: not USB2; the camera keeps a gain from the moment of the lock, [2026-10-02-hidden-camera-gain.md](2026-10-02-hidden-camera-gain.md). **Open:** a lock procedure that makes that state certain |
| UV | what LEDs 1 and 2 show (fluorescence or reflection), with a filter and a target that does not fluoresce | **open** (on office paper: fluorescence) |
| Killing the process | as the runbook lists | done: [2026-10-02-kill-test.md](2026-10-02-kill-test.md) |
| Unplugging | as the runbook lists | done, with USB2 connected and without: [2026-10-02-unplug-test.md](2026-10-02-unplug-test.md) |
| Linux, Windows | the same run | **open** |

Each becomes a dated file in this folder: the command lines, the `--json` output, the
unit, the software version, and the conclusion.
