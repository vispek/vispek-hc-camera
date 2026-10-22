# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog 1.1](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/) with the 0.x rules in
[CONTRIBUTING.md](CONTRIBUTING.md). A change to an acquisition default gets its own
"Defaults" entry (old value, new value).

## [Unreleased]

## [0.1.1] - 2026-10-22

### Fixed

- Enable locked acquisition on Windows through DirectShow camera controls using
  Python's standard library. Preserve caller COM apartments, validate driver
  capabilities and convert exposure values to the camera's supported steps.
- Report the Windows control backend in `check` and `setup`. Verified on one HC1500
  for dark locking, pairing, LEDs 5-17, white-reference calibration and exports.
  Acquisition defaults and UV permission requirements are unchanged.

### Changed

- READMEs: installing from PyPI instead of "from a checkout until the first release";
  the viewer, now on PyPI, is presented again.

## [0.1.0] - 2026-10-04

The first release: SDK and command line for the Vispek HC1500. Light source over its
serial protocol with UV permission and all-off on exit; frames through ffmpeg; camera
parameters locked in darkness and read back; scans with a whole dark frame before every
channel; white references, relative reflectance, pictures, indices, spectra; ENVI, TIFF
and `cube.npz` export; a simulated device for everything; `vispek-hc setup` for the
macOS camera helper. Verified on one device on macOS (Apple silicon). Linux: built, not
verified on a device. Windows: frames and LEDs, no camera parameter lock yet.

### Added

- The package description on the package index is the README with its relative links
  turned into links to the repository, at the release's tag (`hatch_build.py`,
  `tools/pypi_readme.py`). The README in the repository keeps its relative links.
- `vispek-hc setup`: compiles the macOS camera-control helper from source files that now
  come inside the wheel (checked against their checksums; nothing is downloaded, no
  binary is distributed). An installation from a wheel could not lock the camera before:
  the helper could only be built in a source checkout.
- Windows (not verified on hardware): cameras are listed through ffmpeg's DirectShow
  listing and the HC1500 is found by name; `check` says that camera parameters cannot be
  locked there yet and points to the CH341 driver when no serial port is found.
- Linux (not verified on hardware): the camera is found by its USB id among the capture
  nodes of `/sys/class/video4linux`; camera parameters are read and locked with
  `v4l2-ctl`, by control number, on the node the frames come from; `check` reports
  `v4l2-ctl`, membership of the `dialout` group and brltty; `tools/99-vispek-hc.rules`.
- `docs/integration-guide.md`: the device for people, and coding agents, who write their
  own control software in any language: cables, the serial protocol with reference code
  (run by a test), camera formats and controls, the five behaviours of the device that
  spoil data, and how to record and calibrate a multispectral image. Self-contained.
- `apply_calibration(..., partial=True)`: a capture with bands the calibration has no
  white reference for gets the covered bands calibrated and the others left out
  (`calibration.bands_left_out`). Asked for on the device: a scan of LEDs 1-17 lost
  NDVI, NDWI, PCA and the mean view because the white reference held LEDs 5-17.
- Repository skeleton: packaging, lint, type checking, CI on Linux, macOS and Windows.
- `vispek_hc.protocol`: light-source serial frames (CRC-8, ON/OFF commands, reply
  scanning with resynchronisation, echo detection), verified against the official
  examples and the 34-frame routine.
- `vispek_hc.models`: the HC1500 LED table (version `hc1500-nominal-1`, unverified).
- `vispek_hc.errors`: one class per error code, grouped into exit-code families.
- `vispek-hc` command line with the `vispek-hc.cli/1` JSON envelope.
- `vispek_hc.illuminator`: light-source driver. Exclusive port, baud probing with OFF
  commands only, 3 attempts of 200 ms, purge of late replies, UV gate, watchdog for
  hand-lit LEDs, OFF after an unconfirmed ON, all-off on leaving and on closing,
  lock-free all-off at exit and on signals with a latch against re-lighting.
- `vispek_hc.video`: frames through an ffmpeg child process, raw YUYV converted with
  exact integer BT.601, MJPEG splitting by segment walk, frame freshness by
  arrival, automatic stream reopening.
- `vispek_hc.controls`: camera parameter lock with two read-backs; macOS backend through
  the vendored `uvc-util` (`tools/uvc-util`, MIT, unmodified).
- `vispek_hc.framecheck`: darkness relative to the black level, clipping bits, frame
  stability.
- `vispek_hc.units`, `vispek_hc.session`: per-device profiles, pairing probe, black-level
  check.
- `vispek_hc.sim`: simulated light source, rolling-shutter camera and controls on a
  virtual clock; `--simulate` on the command line.
- Commands `check`, `devices [--pair]`, `led on|off`.

- `vispek_hc.sweep`: recording. The plan (channels, UV permission, PWM values, output
  folder) is checked before the camera is touched. One dark frame and one frame per
  channel, each taken only after it settled; time floor from the measured pipeline
  latency; 3 attempts per channel; OFF in `finally`; disk writes on their own thread,
  never waited for with an LED lit; cancellation within 0.1 s; `RECORDING_PARTIAL` once an
  output is finished; automatic PWM; `tune`; modes `single`, `dark`, `video`; the one-call
  `capture()`.
- The pairing probe stores the pipeline latency once per unit and afterwards only
  reports it (`pair_probe(relearn=True)` and `tune` measure it afresh): scan timing, and
  with it the binding of white references, no longer moves with every `check`.
- `vispek_hc.cube`: `Cube`, crash-safe capture folders (`capture.json`, `events.jsonl`,
  lossless frames), `repair`, `cube.npz`, ENVI and multi-page TIFF export. `overwrite`
  replaces a capture folder and nothing else.
- `vispek_hc.calibration`, `vispek_hc.reflectance`: white-reference packages with quality
  gates (pixels bright enough to divide by, saturation) and binding (see
  `docs/calibration.md`); relative reflectance with the group's own dark frame and a
  denominator floor of max(8 DN, 5 % of the band's white signal); legacy definition kept.
- `vispek_hc.views`, `vispek_hc.spectra`: composites, indices, PCA, heat, palettes;
  spectra of points, rectangles and polygons with CSV output.
- Commands `record`, `reference white`, `calibrate`, `reflectance`, `view`, `spectrum`,
  `export`, `tune`, `repair`, `demo`.
- `schemas/` (JSON Schema 2020-12), `docs/api.md`, `docs/calibration.md`, 13 example
  scripts.

- Documentation: `docs/hardware.md`, `protocol.md`, `data-format.md`, `platforms.md`,
  `cli.md`, `troubleshooting.md`, `release-checklist.md`, acceptance records under
  `docs/acceptance/`; README with a per-platform shortest path; `AGENTS.md` with a Python
  quick start, recipes and a troubleshooting index. Tests keep the documents in step with
  the code: links, the LED table, the protocol frames, the keys every command prints, and
  the commands and snippets of the README.
- `--setup-label` and `--enclosure` on `record`, `reference white` and `tune`.
- `iter_scans`, `record_frames`, `write_csv` and `MaskBit` are public.
- Release workflow (`.github/workflows/release.yml`): a tag must equal the package
  version; tests, build and a clean install run before anything is published; release
  candidates go to TestPyPI, releases to PyPI, each from its own environment (`testpypi`,
  `pypi`), which is to be protected by required reviewers before the first tag. Not run
  yet.

- Hardware acceptance checks (`tests/hardware/`, `pytest -m hardware`) with a runbook:
  pairing, black level, one visible LED, camera lock, exposure steps, reply loss against
  command pause and timeout, a scan, a white reference, scalar noise, both pixel paths.
  They are tested and rehearsed on the simulated device, and were run on one device
  without the white-board checks (`docs/acceptance/2026-10-02-acceptance-leds-5-17.md`).

- `render(..., banner=False)` leaves the red strip off a picture of raw data, for a program
  that marks raw data in its own way; `SimFrameSource.show(reflectance)` puts another
  target in front of the simulated camera while it runs; `views.view_range` gives the
  values at the two ends of a view's colour table, for a scale beside the picture. All
  three are for programs that draw their own pages around the pictures.

### Fixed

- A white reference recorded with another pause before each channel (or before that
  pause existed) was applied to new scans without complaint, although its readings are
  not comparable (LED 9 differs by 20 %). `dark_pause_ms` is now part of the binding:
  such a pair is `CALIBRATION_MISMATCH`. **Calibrations made before this change no
  longer match new scans; record the white reference again.**
- The dark pause was planned with the period of the rounded frame rate. At 5.6 fps (the
  camera at 1080p) that is shorter than a real frame; at 2.6 fps nothing was left for
  the read-out. A rate within 2 % of a whole number still counts as that number (so two
  sessions plan the same pause); any other rate is rounded down.
- `Illuminator.close()` returned quietly when the last OFF was not confirmed, and a
  caller showed the LED as off. It now raises `LED_SHUTDOWN_UNCONFIRMED` after the port
  is released.
- `keep_pwm` is exported: the changelog and `auto_pwm` pointed to it, the package did not.
- The `pca` view crashed with an `IndexError` on a capture that had fewer pixels valid in
  every band than components (seen on the device: a scan that clipped almost everywhere
  left two). It is now refused with `INVALID_STATE`, the counts and what to do.
- Closing the light source while another thread was lighting an LED could leave that LED
  lit behind a closed port: `close()` read the LED states before the command on the wire
  had finished. It now refuses new ON commands first and waits for the one in flight.
  Found by the review of the viewer, which closes the device from another thread than
  the one that scans.

- The PWM search no longer leaves its values in the unit's profile when the white
  reference recorded with them is refused (`reference white --auto-pwm`, `tune`). With
  something else than the white board in view, the search can end at a PWM that gives
  almost no light; those values used to stay and dim every later scan. Seen on the
  device with a circuit board in view. `auto_pwm(..., store=False)` and `sweep.keep_pwm`
  are how a caller does the same.

### Differs from the specification

- `vispek-hc demo` runs on the simulated device with an invented scene and checks the
  result against that scene (every pixel of every band within 0.01), instead of
  comparing bundled real captures with the reference medians of the specification. No
  real sample pictures are distributed. The comparison with the reference medians
  (default and legacy definition, within 0.001) is kept as a maintainers' test that runs
  where the captures are present.
- The simulated camera takes a reflectance per LED, so that the simulated scene has
  spectra.
- `suggested_min_on_ms` is the selection time less two frame periods (never below the
  physical floor), rounded up to 50 ms, not the selection time itself: stored as it was,
  every `tune` would have lengthened each channel by two frames.
- Automatic minimum on-times are rounded up to 10 ms and use the frame rate rounded to a
  whole number, so that two sessions of one unit plan the same values; `actual_fps` (the
  measured rate) is recorded as evidence and is not part of the calibration binding.

### Not distributed

- Real sample captures. `vispek-hc demo` runs on the simulated device instead.

### Defaults

Changed after the camera's kept gain was found
(`docs/acceptance/2026-10-02-hidden-camera-gain.md`):

- The parameter lock: written as found -> **taken in darkness**. The first lock on a
  stream switches every LED off, puts the camera in automatic exposure for a second and
  requires the picture to be dark (every channel at most 12); only then are the
  parameters written. A lock that sees light is refused with `GAIN_NOT_CONFIRMED`
  (`require_dark=False`, or `--unlocked`, locks anyway and marks it `undetermined`).
  The lock at the start of a scan keeps a dark lock the session already took on the same
  stream; a lock asked for by hand, and any lock after the stream was reopened, passes
  through darkness again.
- Captures record `camera_gain_state` and `lock_dark_level`, and `camera_gain_state` is
  part of the calibration binding: it must be `dark` on both sides. **Calibrations and
  captures made before this change match nothing without `--force`; record the white
  reference again.**
- The model's PWM table fits the state a dark lock gives (the first white-board run
  found it about four times too low because that day's lock had seen light).

Changed after the first checks on a device (`docs/acceptance/2026-10-02-first-device.md`):

- Range of the camera's uncompressed YUV frames: full -> **limited** (luma 16-235).
  Every LED off reads Y = 20 on that path and 5 through the JPEG path; converted as
  limited range it is 5, the black level of the camera. Captures record the range.
- Pairing probe: LED 6 at PWM 50 -> LED 6 at **the PWM the unit scans it with** (224
  from the model's table). At PWM 50 a dark sample 30 cm away raised the picture by 3
  code values, too close to the threshold of 2. The probe also waits for the picture to
  settle before it takes its baseline.
- Minimum on-time when no probe has run: 300 ms -> **500 ms**. Light shows about 350 ms
  after the device's reply and the first whole frame about 450 ms after it.

Changed after the white-board checks
(`docs/acceptance/2026-10-02-white-board-and-camera-state.md`):

- Dark pause before every channel: 20 ms -> **one frame period + exposure + 50 ms,
  rounded up to 10 ms (170 ms at 10 fps)**. The camera's processing carries a state over
  from the previous picture that shifts LEDs 5, 9, 10 and 11 by 23 code values; with the
  old pause LED 9 read 118 in most scans and 141 in some, and LEDs 10 and 11 changed
  from scan to scan. After one whole dark frame every band repeats within 0.2 %.
  `inter_channel_ms` now only lengthens the pause. Captures record `dark_pause_ms`. A
  scan of LEDs 5-17 takes about 15 s instead of 12 s. **White references recorded before
  this change should be recorded again.**

### Verified on hardware

On one HC1500 on macOS (Apple silicon), 2026-10-02: the serial protocol and baud rate,
replies byte for byte, frames through ffmpeg at 1280x720 and 10 fps, locking of the
camera parameters with uvc-util (every exposure asked for is held exactly), the black
level, the delay from ON to light in the frames, **a scan of LEDs 5-17** (every channel
settled at the first attempt; about 0.7 s per channel), and both pixel paths (black 5,
clipping at 255). About 5 % of commands get no reply, with or without the camera and
whatever the pause or timeout; the resend is answered. The reply timeout (200 ms) was
kept; the pause between channels was lengthened later the same day (see Defaults). Without camera access macOS gives no
frames and no message; the error now says so.

With a sheet of white paper as the target: the PWM search, a white reference, relative
values of the same paper (1.000 in every band, three scans in a row), and the noise of
the strongest channel against luma (the default `reduction` stays `dominant`).

The four UV LEDs light (with `--allow-uv`, in a closed box) and a scan of all 17 channels
runs in about 19 s; on office paper their bands show the paper's fluorescence. A killed
process leaves its LED lit until `led off --all`; SIGTERM switches it off first.

Pulling the camera cable during a scan switches the LED off (confirmed) and keeps what
was recorded as a partial capture. Pulling USB1 leaves the LED lit until USB1 is back,
with USB2 connected or not: the camera cable powers the light source too. SAFETY.md and
the hint of `LED_SHUTDOWN_UNCONFIRMED` now say to unplug every cable, not just the two of
the light source.

Not verified yet: a white reference on a real reflectance standard, what the UV bands
show on a target that does not fluoresce, the order of LEDs 1-2 and 13-17, Linux and
Windows.

The brightness difference seen after the unplug test is the camera, not USB2: it keeps
the gain its automatic exposure had chosen when the parameters were locked (a factor of
up to 4.4, read back by no control). A lock in darkness is repeatable; a lock under
light is not. The lock procedure does not yet make that state certain
(`docs/acceptance/2026-10-02-hidden-camera-gain.md`).
