# Troubleshooting

Start with `vispek-hc check --json`: it names what is missing (`data.problems`, each with
a `hint`). The same commands run against the simulated device with `--simulate`, which
tells you whether a problem is in the software or at the hardware.

Every error has a `code`; the table of all codes is [errors.md](errors.md).

## An LED stays lit

Unplug **every** cable of the device: USB1, USB2 and the camera cable. Pulling USB1 only
removes the way to send OFF, and the light source goes on drawing power through the
other cables (measured: an LED stayed lit with only the camera cable connected). Then
find out why: a process that was killed (`kill -9`, a
crashed terminal, power loss) cannot send OFF. A process that a shell script started in
the background ignores Ctrl-C's signal (SIGINT); end it with `kill` (SIGTERM), which
switches the LED off first. `vispek-hc led off --all` switches
everything off again and reports whether the device confirmed it. If the device did not
confirm (`LED_SHUTDOWN_UNCONFIRMED`), do not trust that the LED is off. UV-C is
invisible. See [SAFETY.md](../SAFETY.md).

## No serial port is found

`DEVICE_NOT_FOUND: no light-source serial port found`

- The serial port is on the **USB1** socket of the device. USB2 only carries power.
- Linux: are you in the `dialout` group? Has `brltty` taken the adapter? Windows: is the
  CH341 driver installed? See [platforms.md](platforms.md).
- Name the port yourself: `--port /dev/cu.usbserial-1420`, `--port COM5`.

## More than one port or camera fits

`AMBIGUOUS_DEVICE`: `details.candidates` lists them. Pass `--port` or `--camera`. One
HC1500 at a time is supported.

## The port is busy

`PORT_BUSY`: another program has the serial port open, often an earlier `vispek-hc led on`
that is still holding its LED, or a serial monitor. End it.

## The device does not answer

`NO_PROTOCOL_REPLY` (no baud rate gave a valid reply) or `LED_NO_ACK` (a command was
not answered three times):

- Is it the right port? Other CH341 adapters look the same.
- Is the cable a data cable? Does the port vanish and come back (`dmesg`, Console)?
- Lighting many LEDs at once can pull the USB1 supply down; the serial chip then
  disconnects. Use one LED at a time.

After `LED_NO_ACK` the state of that LED is unknown. The software sends an OFF before it
reports the error and says whether that was confirmed (`details.off_confirmed`).

## A UV channel is refused

`UV_NOT_ALLOWED`: LEDs 1-4 need `--allow-uv` (`allow_uv=True`). Read
[SAFETY.md](../SAFETY.md) first. The UV LEDs also need the **USB2** power socket; the
software cannot see whether it is connected.

## No frames arrive

`FRAME_INCOMPLETE`, `PERMISSION_DENIED` or `BACKEND_MISSING`:

- `ffmpeg` must be installed and on the `PATH` (`vispek-hc check` shows what it found).
- macOS: allow your terminal to use the camera (System Settings, Privacy & Security,
  Camera) and run the command again. Windows: allow desktop apps to use the camera.
- Is another program showing the camera? Only one can read it.
- `details.ffmpeg` of the error holds the last lines ffmpeg printed.

## The camera parameters cannot be locked

- `CONTROLS_UNAVAILABLE`: no way to set camera parameters on this computer. macOS: run
  `vispek-hc setup`. Linux: install `v4l-utils`. Windows: not built yet; scan with
  `--unlocked` and read what that means in [platforms.md](platforms.md).
- `AWB_NOT_CONFIRMED`, `EXPOSURE_NOT_CONFIRMED`: the camera did not keep white balance
  off or exposure manual. Reopening the stream resets this camera; run the command again.
  If it persists, `--unlocked` scans anyway, and the result is not comparable.
- `GAIN_NOT_CONFIRMED`: the camera saw light when its parameters were locked. It keeps
  the gain its automatic exposure had at that moment (up to 4.4 times apart, read back
  by no control), and only the gain it reaches in darkness is repeatable. Close the lid
  of the dark box and run the command again; every LED is switched off before the lock.
  Without a dark box, `--unlocked` scans anyway: the capture says
  `camera_gain_state: undetermined` and no calibration applies to it without `--force`.
- `CAMERA_CHANGED`: a parameter moved during a scan. The frames are kept, the scan is
  marked failed. Scan again.

## A scan fails with TIMING_UNSTABLE

- `frames did not settle`: two frames in a row never agreed. Is the sample or the camera
  moving? Is light from outside changing (open box, flickering room light)? A larger
  `stability_tolerance` hides the symptom and lets half-lit frames through; do not raise
  it to get past this.
- `LED n gave no light on the white reference`: the channel's own colour did not rise
  above the dark frame. For LEDs 1-4: is USB2 connected? Otherwise: is the lens covered,
  is the board in view, is the PWM far too low?

`details.led_id` names the channel. For "no light", up to three of the rejected frames
are kept in `g0001/rejected/` of the capture; look at them. Frames that did not settle
are not kept; `events.jsonl` has their differences (`timing_history`, `frame_rejected`).

## A recording ended early

- `RECORDING_PARTIAL`: something failed after at least one output was finished.
  `details.cause` is the code of what failed, `details.outputs` how many outputs are
  usable. They load normally.
- `STORAGE_LOW`: the disk is, or became, full.
- `CANCELLED`: you stopped it. What was recorded is on disk with `status: partial`.
- After a crash, `capture.json` may be missing or cut short: `vispek-hc repair DIR`.

## Warnings after a scan

- `clipped pixels under LED n`: more than 1 % of the frame is at 255. Lower that
  channel's PWM (`reference white --auto-pwm` finds one), or the clipped pixels are
  invalid in every result.
- `no light seen under LED n`: on a sample this is a warning, not an error: a black
  sample, a dead LED and a missing USB2 supply look the same.
- `exposure ... is short enough for the 16 kHz LED dimming to show as horizontal ripple`:
  below 6.3 ms of exposure the PWM of the LEDs shows as stripes of more than 1 %.
  `check` says the same.

## The calibration is refused

- `CALIBRATION_QUALITY` when making it: a band has too many saturated pixels, or too few
  pixels bright enough to divide by. `details.bands` shows which. Lower the PWM, or
  restrict the judgement to the board with `--white-roi`.
- `CALIBRATION_MISMATCH` when applying it: `details.differing` lists the settings in
  which the sample and the white reference differ. Scan both with the same settings.
  `exposure`, `gain` and `camera_controls_values` in that list also mean "unknown": a scan
  made with `--unlocked` where the camera cannot be read. `--force` applies the
  calibration anyway; the result is then not comparable.
- A white reference recorded before `tune` or `--auto-pwm` no longer matches scans made
  after it, because PWM values and timing changed. Record it again.
- `dark_pause_ms` in that list: the white reference was recorded with another pause
  before each channel, or before that pause was recorded at all. The readings are then
  not comparable (the camera carries state from the frame before). Record it again.

## An index picture is refused

`INVALID_STATE: the 'ndvi' view needs calibrated data`: pass `--calib`. On raw data an
index only shows that two LEDs were driven at different power.

## The numbers look wrong

- Relative values in the 255 and 275 nm bands can be negative or near zero on materials
  that do not fluoresce. That is expected ([calibration.md](calibration.md)).
- A white reference drifts with LED temperature. A reference more than 24 h away from
  the sample produces a warning; an hour already costs 1-2 %.
- The values are 8-bit code values after the camera's processing, not radiance. Do not
  compare scans made with different exposure, gain or PWM.
- **Everything is several times brighter or darker than last time, with the same
  settings.** The camera keeps an internal gain from the moment its parameters were
  locked: up to 4.4 times more if it saw darkness then than if it saw a bright picture,
  and no control reads it back
  ([the measurement](acceptance/2026-10-02-hidden-camera-gain.md)). Since then every lock
  is taken in darkness and refused otherwise, and captures record `camera_gain_state`.
  Captures and calibrations made before that carry no such record: a calibration is
  not applied to them, or from them, without `--force`. Record the white reference again.

## `vispek-hc demo` says FAIL

`demo` needs no device: it runs on the simulated one and compares the result with the
scene it simulated. A failure is a broken installation or a bug, not a hardware problem.
Report it with the `--json` output of `demo` and of `check`.

## Reporting a problem

Attach the output of `vispek-hc check --json`, the command you ran with `--json`, and for
a recording problem its `capture.json` and `events.jsonl`. Check them for names you do
not want to share ([data-format.md](data-format.md), "What a capture folder reveals").
A problem that can light a UV LED without permission, or leave one lit, is reported
privately: [SECURITY.md](../SECURITY.md).
