# Command line

`vispek-hc COMMAND [options]`, or `python -m vispek_hc`. `vispek-hc COMMAND --help` lists
every option; this page says what each command is for and what it prints.

## The contract

- **`--json`** prints exactly one JSON object on stdout (schema `vispek-hc.cli/1`,
  [`schemas/cli-envelope-1.schema.json`](../schemas/cli-envelope-1.schema.json)):

  ```json
  {"schema": "vispek-hc.cli/1", "command": "record", "version": "0.1.0",
   "ok": true, "data": {}, "warnings": [], "exit_code": 0}
  ```

  On failure `ok` is false and `error` (`code`, `message`, `hint`, `details`) takes the
  place of `data`. Logs and progress go to stderr, one JSON object per line. Within
  `cli/1`, keys are only ever added. The one exception to all this is `--help`, which
  always prints usage text.
- **Exit codes** come from the error's family: 0 success, 1 a bug, 2 usage, 3 the
  computer lacks something, 4 the device cannot be found or opened, 5 the request is
  refused, 6 talking to the hardware failed, 7 finished only in part or not good enough,
  130 cancelled. Every code is in [errors.md](errors.md).
- **Nothing ever prompts.** When more than one device fits and none was named, the
  command fails with `AMBIGUOUS_DEVICE` and lists the candidates in `details`.
- **Ctrl-C** during a recording asks it to end in good order: the LED goes off, files are
  closed, and what was recorded stays on disk. A second Ctrl-C interrupts at once.

Options every command accepts, before or after the command name:

| Option | |
|---|---|
| `--json` | print the envelope |
| `--simulate` | use the simulated device; no hardware is touched |
| `--dry-run` | print the plan without touching the device (`check`, `led`, `record`, `reference white`, `tune`) |
| `--unit LABEL` | which device profile to use (default `hc1500-1`) |
| `--port PORT` | serial port of the light source |
| `--camera NAME` | camera name (macOS, Windows) or device node (Linux) |
| `-v`, `-vv` | more log output on stderr |

`vispek-hc --version` prints the version (`data`: `version` with `--json`).

Paths in `data` are the ones you gave on the command line.

## check

Diagnoses the computer and, if one is attached, the device: Python and platform, ffmpeg,
the camera-control helper, serial ports and cameras found, free disk space. With a device
it also locks the camera parameters, runs the pairing probe (LED 6 at its usual scan PWM for 0.3 s)
and measures the black level.

`check` **exits 0 even when it finds problems**. Read `ready` (`true`, `false`, or `null`
when `--dry-run` kept it from looking at the device) and `problems` (each with `code`,
`message`, `hint`, `details`).

Keys of `data`: `version`, `python`, `platform`, `model`, `unit`, `led_table`, `home`,
`disk_free_bytes`, `ffmpeg`, `controls`, `device`, `serial`, `cameras`, `problems`,
`ready`.

`device` holds, when a device was reached: `serial`, `camera`, `controls` (`locked`,
`awb_off`, `exposure_manual`, `exposure_time_abs`), `pair` (`paired`, `rise_dn`,
`latency_ms`, `fps`), `black_level` (three numbers; 5, 5, 5 is right) and `ack`
(`commands`, `retried`, `loss_rate`, `delay_ms_p50`, `delay_ms_p99`).

## devices

Lists the serial ports and cameras that could be an HC1500. `--pair` also runs the
pairing probe.

Keys of `data`: `serial`, `cameras`.

With `--pair` also `pair`.

## led

```
vispek-hc led on LED PWM [--hold SECONDS] [--allow-uv]
vispek-hc led on --all [--allow-uv]
vispek-hc led off LED | --all
```

`led on` keeps the serial port and blocks until Ctrl-C or until `--hold` runs out, then
switches everything off. LEDs 1-4 need `--allow-uv` and a `--hold` of at most 10 s.
`--all` lights every permitted LED at its usual PWM and warns about the power limit of
the USB1 port.

Keys of `data`: `action`, `leds`, `all_off`.

For `on` also `hold_s` and `ended_by` (`hold` or `interrupt`). `all_off` is true only
when the device confirmed every OFF.

## record

```
vispek-hc record -o DIR [--mode scan|single|dark|video] [--channels 5-17] [--allow-uv]
                 [--pwm N | --pwm-file FILE] [--average N] [--cycles N] [--min-on auto|MS]
                 [--size 1280x720] [--fps 10] [--source uncompressed|mjpeg] [--fast]
                 [--reduction dominant|luma] [--unlocked] [--overwrite] [--no-preview]
                 [--setup-label TEXT] [--enclosure darkbox|open|unknown]
```

- `scan` (default): one dark frame and one frame per channel, into a
  [capture folder](data-format.md).
- `single`: one frame as it is. `dark`: one frame with every LED off (cover the lens for
  the legacy definition). `video`: `--cycles` frames in a row.
- `--cycles 0` records until Ctrl-C. `--average N` scans N times per output.
- `--pwm-file`: a JSON object such as `{"5": 24, "6": 224}`. PWM comes from the option,
  else from the unit's profile, else from the model's table.
- `--overwrite` replaces a capture folder. A folder holding anything else is refused.
- `--channels`, PWM, `--min-on`, `--average`, `--reduction` and `--unlocked` apply to
  `scan` only.
- `--unlocked` scans although white balance and exposure cannot be confirmed fixed.
- `--fast` records MJPEG at 60 fps: quicker, but its RGB values depend on the decoder.

Keys of `data`: `capture`, `capture_id`, `mode`, `role`, `status`, `groups_done`,
`outputs`, `channels`, `preview`.

`outputs` is the number of cubes the capture holds; `preview` is `preview.png` when the
red, green and blue bands were recorded.

### record --dry-run

Checks the plan (channels, UV permission, PWM values, the output folder) and prints it.
No device is opened.

Keys of `data`: `dry_run`, `mode`, `role`, `channels`, `pwm_by_led`, `min_on_ms`,
`estimated_seconds`.

## reference white

`vispek-hc reference white -o DIR [--auto-pwm] [--white-roi X,Y,W,H]` plus the scan
options of `record`. Scans the white board. A channel that shows no light is tried again
(3 attempts) and then fails the scan. `--auto-pwm` first searches, per channel, a PWM
that puts the board at 150-235 without clipping, and stores it in the unit's profile.

Keys of `data`: `capture`, `capture_id`, `mode`, `role`, `status`, `groups_done`,
`outputs`, `channels`, `auto_pwm`.

## tune

`vispek-hc tune -o DIR`: learns this unit on the white board. Measures the pipeline
latency again, searches the PWM of every channel, records one white scan and stores PWM
values and on-times in the unit's profile. White references made before a `tune` no
longer match scans made after it; record a new one.

Keys of `data`: `capture`, `capture_id`, `mode`, `role`, `status`, `groups_done`,
`outputs`, `channels`, `pwm_by_led`, `min_on_ms_by_led`, `saved_to_profile`.

## calibrate

`vispek-hc calibrate --white DIR [DIR ...] -o CALIBRATION [--white-roi X,Y,W,H]`: makes a
[calibration](calibration.md) from one or more white-reference captures.

Keys of `data`: `calibration`, `calibration_id`, `bands`, `white_roi_px`,
`dominant_channel_by_led`, `quality`.

## reflectance

`vispek-hc reflectance CAPTURE --calib CALIBRATION -o cube.npz [--force] [--legacy DARK_CAPTURE]`:
relative reflectance as a [`cube.npz`](data-format.md#cubenpz).

Keys of `data`: `cube`, `layer`, `unit`, `bands`, `median_by_band`, `valid_fraction`.

## view

`vispek-hc view CAPTURE -o out.png [--calib CALIBRATION] [--mode MODE] [--low L --high H]
[--gamma G] [--palette auto|gray|spectral|viridis|classes] [--allow-uncalibrated]`

| Mode | Picture |
|---|---|
| `rgb` (default) | 630 / 560 / 455 nm |
| `cir` | 850 / 667 / 560 nm |
| `uv` | 400 / 370 / 275 nm |
| `band:led_13` | one band |
| `ndvi` | (804 - 667) / (804 + 667), on a fixed -1..1 scale |
| `ndwi` | (850 - 973) / (850 + 973): the 970 nm water band, not the green/NIR or NIR/SWIR indices of the same name |
| `pca` | the first three principal components as red, green, blue |
| `heat` | mean over the valid bands |

`ndvi`, `ndwi`, `pca` and `heat` compare bands and need `--calib`; on raw data they would
only show that the LEDs were driven at different power. `--allow-uncalibrated` renders
them anyway under a banner.

Keys of `data`: `image`, `mode`, `layer`, `size`.

## spectrum

`vispek-hc spectrum CAPTURE --xy X,Y [--wh W,H] [--calib CALIBRATION] [--csv FILE]`: the
spectrum of one pixel, or of the box of `W,H` pixels whose top-left corner is `X,Y`.

Keys of `data`: `geometry`, `layer`, `unit`, `csv`, `bands`.

Each entry of `bands`: `band_id`, `center_nm`, `count` (valid pixels), `total`,
`valid_fraction`, `mean`, `std`, `median`, `min`, `max` (null when no pixel is valid).

## export

`vispek-hc export CAPTURE --format envi|tiff -o DIR [--calib CALIBRATION]`

Keys of `data`: `directory`, `format`, `files`, `layer`.

## repair

`vispek-hc repair CAPTURE`: rebuilds `capture.json` from `events.jsonl` and the frame
files, after a crash or a damaged file. The result is always `partial`; the file the
first repair replaces is kept as `capture.json.bak`.

Keys of `data`: `capture`, `capture_id`, `mode`, `role`, `status`, `groups_done`,
`outputs`, `channels`.

## demo

`vispek-hc demo [-o DIR]`: offline, on the simulated device. Scans an invented scene (a
white board with a leaf-like, a red and a grey patch) and a white reference into
`DIR/captures/`, computes relative reflectance, and writes `rgb.png`, `cir.png`,
`ndvi.png` and `spectra.csv` (the three patches). Then it checks itself: the simulated
light is uneven but falls on sample and white board alike, and the board reflects 1.0,
so every pixel of every band must come out as the scene that was put in, within 0.01.
It replaces simulated captures left in `DIR/captures/` by an earlier run, and nothing
else. It is the first picture and a check of the installation in
one. The spectra are plausible shapes, not measurements, and no real device is involved.

Keys of `data`: `passed`, `result`, `simulated`, `output`, `files`, `captures`,
`tolerance`, `checks`.

## setup

`vispek-hc setup [--dry-run]`: what this computer needs once before camera parameters can
be locked. On macOS it compiles the camera-control helper `uvc-util` from the source
files that come with the package (checked against their checksums first) into
`VISPEK_HC_HOME/bin/`, where every later command finds it. It needs the Xcode command
line tools (`xcode-select --install`) and downloads nothing. `--dry-run` prints the
compiler call without running it. On Linux it reports whether `v4l2-ctl` is installed;
on Windows there is nothing to prepare yet.

Keys of `data`: `platform`, `helper`, `built`, `command`.
