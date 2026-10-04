# AGENTS.md

Guide for coding agents working in this repository. `CLAUDE.md` only points here.

## What this is

`vispek-hc` is the SDK and command line for the Vispek HC1500 multispectral camera: one
USB (UVC) camera and 17 LEDs (255-1050 nm) switched over a serial port. A "cube" is 17
frames taken one after another, one LED at a time. Pixel values are 8-bit code values
after the camera's image processing; they are **not** linear radiance, and wavelengths
are nominal LED centre wavelengths.

Status: pre-release. On one real device scans of all 17 LEDs and a white reference (on
white paper) work; a real reflectance standard has not been tried yet
([docs/acceptance/](docs/acceptance/README.md)).
Everything runs on the simulated device (`--simulate`, `simulate=True`) and on recorded
captures. Camera parameters can be locked on macOS (verified on the device) and on Linux
(built, not verified); not on Windows ([docs/platforms.md](docs/platforms.md)).

## Safety invariants (never weaken these)

1. **UV gate.** LEDs 1-4 (UV-C and UV-A) are lit only when the caller passes an explicit
   permission (`allow_uv=True` / `--allow-uv`). The check runs when a plan is made, before
   any LED is lit. Default channels are 5-17.
2. **Every ON has an OFF.** Any code path that lights an LED switches it off in a
   `finally` block. One LED at a time; the only exception is `led on --all`
   (`exclusive=False`), which warns about the USB1 power limit.
3. **All off on exit.** Leaving a session, a signal, or interpreter exit switches every
   LED off. If an OFF is not confirmed, say "unconfirmed"; never report "off".

See [SAFETY.md](SAFETY.md).

## Commands

```bash
uv sync --group dev                 # install
uv run pytest                       # tests; no hardware, no network, no subprocesses
uv run pytest -m hardware tests/hardware   # with a real HC1500: docs/acceptance/hardware-runbook.md
uv run ruff format --check . && uv run ruff check .
uv run mypy
uv run pre-commit run --all-files
uv run vispek-hc check --json            # machine-readable diagnosis of host and device
uv run vispek-hc check --simulate --json # the same against the simulated device
uv run vispek-hc record -o scan --simulate --json   # a whole scan without hardware
uv run vispek-hc demo --json             # offline tour and self-check on the simulated device
uv run python examples/05_reflectance.py # every example runs offline
uv run vispek-hc setup                   # macOS: compile the camera-control helper (uvc-util)
```

Every command takes `--json` (one envelope on stdout), `--simulate`, and `--help`. What
each prints is in [docs/cli.md](docs/cli.md).

## Using it from Python

`import vispek_hc` has no side effects. Everything public is importable from the package
and listed in [docs/api.md](docs/api.md) (a test keeps the two in step). Two levels:

```python
import vispek_hc

# One call: open the device, lock the camera, scan LEDs 5-17, save, close.
capture = vispek_hc.capture("first-scan", simulate=True)  # drop simulate with hardware
cube = capture.cube()  # .values (H, W, bands) float32, .rgb, .mask, .bands

# The parts: hold a session, light one LED, take one frame.
with vispek_hc.open_session(simulate=True) as session:  # leaving it switches all LEDs off
    session.light.on(9, 36)  # LED 9 (630 nm) at PWM 36 of 1023; LEDs 1-4 need allow_uv=True
    try:
        frame = session.frames.wait_new(4)  # the newest of 4 frames arriving from now on
    finally:
        session.light.off(9)
    rgb = vispek_hc.video.decode(frame)  # (H, W, 3) uint8 raw code values
vispek_hc.save_png(rgb, "frame.png")
```

- Without `simulate=True`, `open_session()` and `capture()` open the real device: the
  serial port and camera they find (`DEVICE_NOT_FOUND` if there is none,
  `AMBIGUOUS_DEVICE` if several fit; pass `port=` / `camera=`). Reading frames needs
  `ffmpeg` on the `PATH`.
- LEDs 5-11 (455-700 nm) are visible, 12-17 (804-1050 nm) near infrared, 1-4 ultraviolet.
  Each needs its own PWM; the table with a starting value per LED is in
  [docs/hardware.md](docs/hardware.md) and in `vispek_hc.HC1500.led(n)`.
- `open_session` does not lock the camera parameters; a scan (`capture`, `record`) does,
  and refuses to run if it cannot. Single frames are taken with whatever the camera is
  set to.
- The lock is taken **in darkness**: every LED off, the lid of the dark box closed. The
  camera keeps a gain from the moment automatic exposure is switched off (4.4 times
  between darkness and light, read back by no control), so a lock that sees light is
  refused with `GAIN_NOT_CONFIRMED`
  ([measured](docs/acceptance/2026-10-02-hidden-camera-gain.md)).
- Errors are `vispek_hc.VispekHCError` subclasses with `code`, `message`, `hint` (may be
  `None`), `details` and `exit_code`. `wait_new` raises `FRAME_INCOMPLETE` when no frame
  arrives in time; `off` raises `LED_SHUTDOWN_UNCONFIRMED` when the device does not
  confirm.

## Repository map

| Path | Responsibility | Public API |
|---|---|---|
| `src/vispek_hc/protocol.py` | serial frames: CRC, build, parse, resynchronise; no I/O | yes |
| `src/vispek_hc/models.py` | HC1500 facts: LED table, dominant channels, UV hazard | yes |
| `src/vispek_hc/errors.py` | one class per error code, exit-code families | yes |
| `src/vispek_hc/interfaces.py` | protocols shared by real and simulated devices | yes |
| `src/vispek_hc/safety.py` | UV gate, all-off at exit and on signals | yes |
| `src/vispek_hc/illuminator.py` | light-source driver (serial) | yes |
| `src/vispek_hc/video.py` | frames through ffmpeg, pixel conversion | yes |
| `src/vispek_hc/controls.py` | camera parameter lock and read-back | yes |
| `src/vispek_hc/framecheck.py` | darkness, clipping, frame-to-frame stability | yes |
| `src/vispek_hc/units.py` | per-device profiles in `VISPEK_HC_HOME` | yes |
| `src/vispek_hc/session.py` | owns the devices; pairing probe, black level | yes |
| `src/vispek_hc/sweep.py` | recording a scan, frame selection, automatic PWM | yes |
| `src/vispek_hc/cube.py` | `Cube`, capture folders, `cube.npz`, ENVI / TIFF export | yes |
| `src/vispek_hc/calibration.py` | white-reference packages | yes |
| `src/vispek_hc/reflectance.py` | relative reflectance, binding check | yes |
| `src/vispek_hc/views.py` | pictures: composites, indices, PCA | yes |
| `src/vispek_hc/spectra.py` | spectra of points and regions | yes |
| `src/vispek_hc/samples.py` | the invented scene and simulated captures for `demo` and the examples | yes |
| `src/vispek_hc/sim.py` | simulated device on a virtual clock | yes |
| `src/vispek_hc/cli.py` | `vispek-hc` command, JSON envelope `vispek-hc.cli/1` | CLI only |
| `schemas/` | JSON Schemas of capture.json, calibration.json, cube metadata, CLI envelope | - |
| `examples/` | 13 short scripts, each runnable offline | - |
| `tools/uvc-util/` | vendored macOS helper (MIT), unmodified; do not edit | no |
| `tests/` | unit tests; `tests/conftest.py` blocks real devices | no |
| `tests/hardware/` | acceptance checks for a real device (`-m hardware`), rehearsed on the simulated one | no |
| `docs/` | reference documents (see below) | - |

The public surface is `vispek_hc.__all__` (listed in [docs/api.md](docs/api.md)), the
command line, and the file formats in `schemas/`.

## Data and CLI contracts

- Errors: `code`, `message`, `hint`, `details`; full table in [docs/errors.md](docs/errors.md).
- `--json` prints one envelope on stdout:
  `{"schema": "vispek-hc.cli/1", "command", "version", "ok", "data" | "error", "warnings", "exit_code"}`.
  Fields are only ever added within `cli/1`. Logs go to stderr. `--help` is the one
  exception: it always prints usage text.
- The CLI never prompts. Ambiguity is an error (`AMBIGUOUS_DEVICE`) that lists candidates.
- `check` is a diagnosis: it exits 0 even when it finds problems. Read `data.ready`
  (`true`, `false`, or `null` when `--dry-run` kept it from looking at the device) and
  `data.problems` (each with `code`, `message`, `hint`, `details`).
- Bands are keyed by `band_id` (`led_01` ... `led_17`), never by wavelength.
- A cube has `rgb` uint8 (H, W, B, 3), the raw data, and `values` float32 (H, W, B), one
  scalar per band chosen by `reduction` (`dominant`: the band's strongest camera channel;
  `luma`: legacy). Axis order is always Y, X, band.
- `mask` bits: 1 invalid, 2 saturated, 4 denominator too small, 8 outside [0, 1] (kept),
  16 / 32 / 64 R / G / B clipped, 128 scalar clipped. Valid means finite and `(mask & 7) == 0`.
- A capture folder holds the source frames, `capture.json` and `events.jsonl`. No derived
  data is stored (`record` adds a `preview.png` for people; nothing reads it back). Files
  of schema `/1` stay readable: fields are only added, unknown ones ignored.
- `--overwrite` / `overwrite=True` replaces a capture folder and nothing else: a folder
  holding other files is refused, never deleted.
- A plan is checked before a device is opened: channels, UV permission, PWM values, the
  output folder. `--dry-run` prints the plan of `record`, `reference white` and `tune`.
- Relative reflectance is `(S - D_S) / (W - D_W)`: 8-bit code values, an index, not a
  physical reflectance, comparable only between scans with identical settings.

## Developing without hardware

`vispek_hc.sim.SimulatedRig()` is a complete simulated HC1500 on a virtual clock: the
ordinary `Illuminator` driver talks to a simulated serial device (script lost, late or
refusing replies with `rig.device.script(...)`), and a simulated rolling-shutter camera
shows what the LEDs light. A 17-channel run takes milliseconds. `open_session(simulate=True)`
and `--simulate` on the command line use it.

Tests must not open a serial port or start a process. An autouse fixture in
`tests/conftest.py` enforces this; mark a test `@pytest.mark.hardware` to opt out (such
tests are skipped by default). Fake `serial.Serial` or `subprocess.Popen` in the test
when the code under test needs one.

## Recipes

- **Start from an example.** `examples/02_blink_led.py` (light source only),
  `03_grab_frame.py` (one frame), `04_capture_cube.py` (a scan), `05_reflectance.py`
  (white reference and calibration), `06`-`07` (pictures, spectra), `11`-`13` (operators
  on the cube as plain numpy functions). All run offline.
- **Add a view.** A composite is one entry in `views.COMPOSITES` (three `band_id`s), an
  index one entry in `views.INDICES`; anything else is a branch in `views.render`. Add
  the mode to `docs/cli.md` and a test in `tests/test_views.py`.
- **Add a command.** A parser in `cli._add_data_commands`, a handler returning
  `Outcome(data, warnings)`, an entry in `cli.HANDLERS`, a section in `docs/cli.md` with
  its "Keys of `data`" line and an entry in `CLI_SECTIONS` of `tests/test_docs.py` (which
  compares that line with the real output), and a row in the envelope test of
  `tests/test_schemas.py`.
- **Add an error.** A class in `errors.py` with a `code`, and a row in `docs/errors.md`
  (`tests/test_errors.py` keeps them identical).
- **Add a device model.** A `DeviceModel` beside `HC1500` in `models.py`: LED table with
  a version string, hazard per LED, camera ids, initial camera profile. The driver,
  `Session`, `check_plan` and `CaptureWriter` take `model=`; `Capture.cube` and the
  command line still assume `HC1500`.
- **Change a default** (PWM table, timing, tolerance, floor): `CHANGELOG.md` "Defaults"
  entry with old and new value, and a record under `docs/acceptance/`.

## Troubleshooting index

| Symptom | Where |
|---|---|
| an LED stays lit | [docs/troubleshooting.md](docs/troubleshooting.md#an-led-stays-lit) |
| no serial port, port busy, no reply | [#no-serial-port-is-found](docs/troubleshooting.md#no-serial-port-is-found) |
| no frames, camera permission | [#no-frames-arrive](docs/troubleshooting.md#no-frames-arrive) |
| camera parameters not locked | [#the-camera-parameters-cannot-be-locked](docs/troubleshooting.md#the-camera-parameters-cannot-be-locked) |
| `TIMING_UNSTABLE`, no light on the white reference | [#a-scan-fails-with-timing_unstable](docs/troubleshooting.md#a-scan-fails-with-timing_unstable) |
| a recording ended early, damaged `capture.json` | [#a-recording-ended-early](docs/troubleshooting.md#a-recording-ended-early) |
| `CALIBRATION_MISMATCH`, `CALIBRATION_QUALITY` | [#the-calibration-is-refused](docs/troubleshooting.md#the-calibration-is-refused) |

## Do not

- Light an LED outside the three safety invariants above.
- Add network calls, `shell=True`, or interactive prompts under `src/`
  (`tests/test_no_network.py`).
- Write absolute paths, user names or host names into output files.
- Change a scalar, mask or default acquisition value without a `CHANGELOG.md` "Defaults"
  entry and an acceptance record under `docs/acceptance/`.
- Introduce names or code from any other camera software (`tests/test_provenance.py`,
  [docs/provenance.md](docs/provenance.md)).
- Skip lint, types or tests.

## More

- [docs/api.md](docs/api.md) - every public Python name
- [docs/cli.md](docs/cli.md) - every command and the shape of its `data`
- [docs/data-format.md](docs/data-format.md) - cube, capture folder, `cube.npz`, export
- [docs/calibration.md](docs/calibration.md) - what the numbers mean, white references, binding
- [docs/integration-guide.md](docs/integration-guide.md) - the device for people (and agents) who write
  their own control software in any language: cables, protocol with reference code, camera
  settings, the five behaviours that spoil data, how to record and calibrate. Self-contained
- [docs/hardware.md](docs/hardware.md) - the device: LED table, camera, physical facts
- [docs/protocol.md](docs/protocol.md) - the serial protocol of the light source
- [docs/platforms.md](docs/platforms.md) - what works on macOS, Linux, Windows
- [docs/architecture.md](docs/architecture.md) - modules, concurrency, the scan, the timeout table
- [docs/errors.md](docs/errors.md) - error codes and exit codes
- [docs/troubleshooting.md](docs/troubleshooting.md) - by symptom
- [docs/provenance.md](docs/provenance.md) - what this implementation was written from
- [docs/acceptance/](docs/acceptance/README.md) - acceptance records
- [CONTRIBUTING.md](CONTRIBUTING.md) - how to send changes
