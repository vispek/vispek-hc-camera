# Python API

Everything a program should use is importable from `vispek_hc` and listed here
(`tests/test_docs.py` keeps this page and `vispek_hc.__all__` in step). Other modules are
importable too, but only these names, the command line and the file formats are the
public surface.

Importing `vispek_hc` has no side effects. Inputs and outputs are dataclasses and numpy
arrays; axis order is always Y, X, band.

## One call

| Name | What it does |
|---|---|
| `capture(directory, settings=None, *, simulate=False, port=None, camera=None, unit="hc1500-1", allow_uv=False, overwrite=False)` | Open the device, lock the camera, scan, save, close. Returns the `Capture`. |
| `example_captures(directory)` | A sample capture and its white reference to try things on, recorded on the simulated device into `directory`. The scene is invented (`vispek_hc.samples`). |

```python
import vispek_hc

capture = vispek_hc.capture("first-scan", simulate=True)  # drop simulate with hardware
cube = capture.cube()  # values (H, W, bands) float32, mask, bands
```

## Parts

| Name | Kind | What it is |
|---|---|---|
| `open_session(*, simulate=False, port=None, baud=None, camera=None, unit=..., allow_uv=False, size=..., fps=..., source="uncompressed")` | function | Finds and opens light source, camera and camera controls. Use the result in a `with` block. |
| `Session` | class | Owns the devices of one unit: `light`, `frames`, `controls`, `unit`. Methods `lock_controls(require_dark=True, keep=False)`, `pair_probe(relearn=False)`, `black_level()`. The first lock on a stream switches every LED off, passes through automatic exposure and must see darkness (`GAIN_NOT_CONFIRMED` otherwise; `require_dark=False` locks anyway and marks the report `undetermined`; `keep=True`, used at the start of every scan, keeps a dark lock this session took on the same stream instead of taking it again). `lock_stale` (the stream was reopened since the lock) and `lock_is_current` (also asks the camera). Leaving it switches every LED off. |
| `Illuminator` | class | The light source: `on(led_id, pwm)`, `off(led_id)`, `all_off()`, `states`, `transactions`. UV LEDs need `allow_uv=True`. |
| `FfmpegSource` | class | Frames from the camera: `start()`, `stop()`, `latest()`, `wait_new(count)`. |
| `SimulatedRig` | class | A simulated HC1500 on a virtual clock (`light`, `frames`, `controls`, `device`, `clock`); `Session.from_rig(rig)` wraps it. |
| `ScanSettings` | dataclass | How to scan: `channels`, `pwm`, `min_on_ms`, `average`, `cycles`, `reduction`, `role`, `white_roi`, ... |
| `record(session, directory, settings=None, *, overwrite=False, stop=None, progress=None)` | function | Record scans into a capture folder and return the `Capture`. `stop` is a `threading.Event`; `overwrite` only ever replaces a capture folder. |
| `iter_scans(session, directory, settings=None, *, overwrite=False, stop=None, progress=None)` | function | The same as a generator: yields the number of each finished output. With `cycles=0` it scans until `stop` is set. |
| `record_frames(session, directory, mode, *, count=1, overwrite=False, stop=None)` | function | Plain frames without a scan: `"single"`, `"dark"` (every LED off; the covered dark frame of the legacy definition) or `"video"`. |
| `auto_pwm(session, settings=None, stop=None, notes=None, *, store=True)` | function | Locks the camera, then finds a per-channel PWM that puts the white board at 150-235 without clipping; stored in the unit's profile unless `store=False`. Channels that never get into range are named in `notes` (a list you pass). |
| `keep_pwm(session, pwm)` | function | Stores PWM values (LED number to value) in the unit's profile, for a caller that ran `auto_pwm(store=False)` and whose white scan was then accepted. |
| `tune(session, directory, settings=None, *, overwrite=False, stop=None, notes=None)` | function | `auto_pwm`, one white scan, and each channel's suggested minimum on-time into the unit's profile. |
| `illuminator` | module | Port discovery (`find_ports()`), limits and timing constants of the light-source driver. |
| `video` | module | `decode(frame)` to RGB, camera listing, pixel conversion. |
| `views` | module | Colour tables, `colourise`, `stretch`, `index_of`, `quality_overlay`, `view_range` (the values at the two ends of a view's colour table). |
| `HC1500` | object | The device model: LED table (`led(n)`, wavelengths, dominant channels, UV hazard), camera facts. |

## Data

| Name | Kind | What it is |
|---|---|---|
| `Capture` | dataclass | A capture folder read back: `document` (the content of `capture.json`), `groups`, `outputs`, `cube(output=0, reduction=None, dominant_by_led=None)`, `frame()`, `verify()`. |
| `load_capture(directory, verify=False)` | function | Read a capture folder; `verify` checks every frame's sha256. |
| `Cube` | dataclass | `rgb` uint8 (H, W, B, 3), `dark_rgb`, `values` float32 (H, W, B), `mask` uint8, `bands`, `metadata`; `valid`, `band_index(band_id)`, `save(path)`, `Cube.load(path)`. |
| `Band` | dataclass | `band_id` (`led_NN`), `name`, `center_nm`, `dominant_channel`. |
| `Calibration` | dataclass | A white reference package: `save(directory)`, `Calibration.load(directory)`, `document` (the content of `calibration.json`). |
| `make_calibration(captures, white_roi=None, enforce_gates=True)` | function | Build a calibration from white-reference captures made with identical settings; see [calibration.md](calibration.md). |
| `apply_calibration(capture, calibration, *, output=0, force=False, covered_dark=None, partial=False)` | function | Relative reflectance (layer L2) as a `Cube`. `partial=True` accepts a capture with bands the calibration does not cover and leaves those bands out of the result. |
| `render(cube, mode, *, low=None, high=None, gamma=1.0, palette="auto", roi_px=None, allow_uncalibrated=False, banner=True)` | function | A view as an RGB array: `rgb`, `cir`, `uv`, `ndvi`, `ndwi`, `pca`, `heat`, `band:led_NN`. Raw data gets a red strip unless `banner=False`. |
| `save_png(image, path)` | function | Write a rendered view. |
| `Region` | dataclass | A point, rectangle or polygon in pixel coordinates. |
| `spectrum(cube, region)` | function | Per-band statistics of the valid pixels of a region. |
| `write_csv(path, cube, regions)` | function | The spectra of 1 to 32 regions as CSV. |
| `MaskBit` | enum | The bits of `Cube.mask` by name (`SATURATED`, `SMALL_DENOMINATOR`, ...). |
| `principal_components(cube, count=3)` | function | Scores of the first principal components, (H, W, count). |
| `export_envi(cube, directory)` | function | ENVI Standard files: float32 data, uint8 quality mask, metadata. |
| `export_tiff(cube, directory)` | function | Multi-page float32 TIFF and a mask TIFF. |
| `VispekHCError` | exception | Base class of every error raised on purpose; see [errors.md](errors.md). |
| `__version__` | str | The installed version. |

## Mask bits

`Cube.mask` holds, per pixel and band: 1 invalid, 2 saturated, 4 denominator too small,
8 outside [0, 1] (value kept), 16 / 32 / 64 red / green / blue clipped, 128 scalar
clipped. A pixel counts in statistics when its value is finite and `(mask & 7) == 0`.
