# Hardware acceptance: how to run it

Sixteen checks on a real HC1500; expect several minutes. **No ultraviolet LED is
switched on**: only LEDs 5-17, one at a time. LEDs 12-17 are near infrared and
invisible, so seeing nothing does not mean nothing is lit. Each LED is lit at the PWM a
scan uses for it (the reply-loss checks: LED 6 at PWM 1). Two go higher: the PWM search of the white-reference check may take any of LEDs
5-17 up to full power for a few frames, and the pixel-path check raises LED 9 step by
step until the picture clips (at most a few frames per step). Read
[SAFETY.md](../../SAFETY.md) first.

Run on one device so far, without the white-board checks:
[2026-10-02-acceptance-leds-5-17.md](2026-10-02-acceptance-leds-5-17.md). A rehearsal on
the simulated device runs in CI, so the checks themselves are known to work.

## Set up

1. The device in its dark box, lid closed. USB1 (serial and LED power) and USB3 (camera)
   connected. USB2 is not needed: no UV LED is used. Leaving it unplugged is **not** a
   safeguard, though: the UV LEDs light without it (measured). What keeps them dark is
   that the software is not given `--allow-uv`. Whatever the state of USB2, keep it the
   same for a white reference and the scans that use it: the brightness of every LED was
   several times higher in one state than in the other on the first device, and why is
   not settled.
2. Something that reflects in view; for the white-board checks, the white board at the
   working distance.
3. On macOS: `tools/uvc-util/build.sh` once, and camera access allowed for your
   terminal. Elsewhere: see [platforms.md](../platforms.md); without parameter locking
   the camera checks fail, which is the finding. If your `uvc-util` lives in the `bin`
   folder of your usual configuration folder, point `VISPEK_HC_UVC_UTIL` at it: the run
   uses a configuration folder of its own.
4. `uv run vispek-hc check`. Fix what it reports first. Keep its `--json` output; it goes
   into the record. (`check` stores what it learns in your usual unit profile; the test
   run below does not touch that profile.)

## Run

To see the whole thing work first, without a device:

```bash
VISPEK_HC_ACCEPTANCE_SIMULATE=1 VISPEK_HC_WHITE_BOARD=1 uv run pytest -m hardware tests/hardware -v
```

Then on the device (do not `export` the switch above: set it on that one line only):

```bash
export VISPEK_HC_ACCEPTANCE_DIR=acceptance-runs
export VISPEK_HC_WHITE_BOARD=1          # only with the white board in view
# export VISPEK_HC_PORT=/dev/cu.usbserial-1420   VISPEK_HC_CAMERA="..."   if check asked for them
uv run pytest -m hardware tests/hardware -v -x
```

The first line pytest prints says `REAL DEVICE` or `REHEARSAL on the simulated device`;
`VISPEK_HC_ACCEPTANCE_SIMULATE` takes `1`, `0` or nothing, and anything else stops the
run before a device is opened. `-x` stops at the first failure: later checks build on
earlier ones.

Each run gets a folder of its own inside `VISPEK_HC_ACCEPTANCE_DIR`, named by its start
time: `records.json` (every number it measured, and whether it was simulated), the
captures it recorded, and its own unit profile. Your normal unit profile is not touched.
The tests have no time limit; stop a run with Ctrl-C, which switches the LEDs off.

## What each check decides

| Test | Passes when | What its record is for |
|---|---|---|
| `test_pairing` | the camera sees LED 6 switched by this port | pipeline latency and measured frame rate: the basis of all scan timing |
| `test_black_level` | every LED off reads 5, 5, 5 | wrong: light leaks in, or the YUV range conversion does not fit |
| `test_visible_led` | LED 6 on (at its scan PWM) is seen, off is dark again | reply delay |
| `test_camera_lock` | white balance stays off and exposure manual | which values the camera changed |
| `test_exposure_steps` | always (it measures) | which exposure the camera settles on for 63, 100, 157, 200, 333 |
| `test_reply_loss_*` | every command is answered within three attempts | share of lost replies for pauses of 10-100 ms and timeouts of 200 / 300 / 600 ms: sets `inter_channel_ms` and the reply timeout. (The driver never pauses less than 10 ms, so "0 ms" cannot be measured without changing it) |
| `test_scan` | LEDs 5-17 each give a settled, lit frame | seconds per scan, attempts and time to a settled frame per channel |
| `test_white_reference` | the board divided by itself is 1 within 5 % in every band | PWM per channel found by the search; the camera channel per LED against the model's table |
| `test_scalar_noise` | there is signal | noise of the dominant-channel value against luma: confirms or reverts the default `reduction` |
| `test_pixel_path_*` | black is 5 and enough power reaches 255, uncompressed and MJPEG | decides full or limited range for the conversion |

## Not covered by the tests: do by hand and write down

- **Order of the LEDs.** LEDs 1-2 and 13-17 look alike to the camera. The hardware team
  signs the table off; then `verified` becomes true in `models.py`.
- **UV LEDs**, last, with shielding in place, USB2 connected and `--allow-uv`:
  `vispek-hc led on 3 74 --hold 2 --allow-uv`, then a scan of `--channels 1-17`. What
  LEDs 1 and 2 show (fluorescence or reflection) needs a filter.
- **Unplugging**, with a visible LED only: pull USB3 during `vispek-hc record -o
  unplug-test` (the scan fails its group, every LED goes off). Then light LED 6 and pull
  USB1: does the LED go out by itself? (On the first device it did not, with USB2
  connected or not: [2026-10-02-unplug-test.md](2026-10-02-unplug-test.md).)
- **Killing the process**, again with USB2 unplugged and **never with LEDs 1-4**: start
  `vispek-hc led on 6 50`, `kill -9` it from another terminal. The LED stays on. Run
  `vispek-hc led off --all` and confirm that the LED goes out and that the command
  reports it off. (A killed process takes the UV time limit with it, which is why this
  is not to be tried with a UV LED.)
- **Other platforms**: the same run on Linux and Windows.

## Record

Copy `records.json` and the `check --json` output into this folder as
`<date>-<unit>-records.json` and `<date>-<unit>-check.json`, and add a line to
[README.md](README.md) with the conclusion. A default that changes because of a record
(`inter_channel_ms`, the reply timeout, `reduction`, the range conversion, the PWM table)
gets a "Defaults" entry in the changelog, old value and new.
