# vispek-hc

SDK and command line for the **Vispek HC1500** multispectral camera: one USB camera plus
17 switchable LED bands from 255 nm to 1050 nm. A capture is 17 frames taken one after
another, one LED at a time.

[中文说明](README.zh-CN.md)

> [!WARNING]
> **UV hazard.** LEDs 1 and 2 (255 nm, 275 nm) emit **UV-C**, which injures eyes and skin.
> LEDs 3 and 4 (370 nm, 400 nm) are UV-A / violet. Never look at the light source; work
> inside a dark box or behind shielding. This software never turns LEDs 1-4 on unless you
> pass `--allow-uv` / `allow_uv=True`. The UV LEDs are powered through the second cable
> (USB2). If the program is killed, an LED stays lit: unplug **every** cable of the device,
> the camera cable too, to be sure. Read [SAFETY.md](SAFETY.md) before connecting hardware.

## Status

Version 0.1, pre-alpha. **Checked on one real HC1500 so far:** serial
protocol, frames, parameter lock, black level, timing, a scan of LEDs 5-17 and a white
reference on white paper, and a scan of all 17 channels including the UV LEDs (see
[docs/acceptance/](docs/acceptance/README.md)). A real reflectance standard has not been
tried yet. Everything below runs today on the simulated device that ships with the package
(`--simulate`). Camera parameters can be locked on macOS (verified on the device) and on
Linux (built, not verified), not yet on Windows; see [docs/platforms.md](docs/platforms.md).

## Install

Python 3.11 or newer, and `ffmpeg` on the `PATH` to read the camera.

```bash
python3 -m venv hc-env && source hc-env/bin/activate
pip install vispek-hc
vispek-hc setup                      # macOS: compiles the camera-control helper, once
vispek-hc check --simulate           # the whole stack on the simulated device
```

For the command alone, `uv tool install vispek-hc` or `pipx install vispek-hc` also work.
A plain `pip install` into the system Python is refused on current Linux and macOS
(PEP 668). To work on the code, clone this repository and run `uv sync`.

## A first picture, without hardware

```bash
uv run vispek-hc demo -o demo
```

That scans an invented scene on the simulated device, makes a white reference, computes
relative reflectance, writes `rgb.png`, `cir.png`, `ndvi.png` and `spectra.csv` into
`demo/`, and checks the result against the scene it simulated (`PASS`). The same, step
by step:

```bash
uv run vispek-hc record -o sample --simulate              # dark frame + LEDs 5-17
uv run vispek-hc reference white -o white --simulate      # the white board, same settings
uv run vispek-hc calibrate --white white -o calibration
uv run vispek-hc reflectance sample --calib calibration -o cube.npz
uv run vispek-hc view sample --calib calibration --mode ndvi -o ndvi.png
```

No real sample pictures are distributed with the software.

## With a device

```bash
uv run vispek-hc check               # what is missing, with a hint for each problem
uv run vispek-hc record -o first     # about 10 s; macOS asks for camera access the first time
uv run vispek-hc view first -o first.png
```

When nothing is missing, `check` also lights LED 6 (520 nm, visible) for
0.3 s to confirm that the serial port and the camera belong to the same device;
`devices --pair` runs that probe on its own. The camera is found by itself on all three
systems; `--camera` names another. Add `--json` to any command for one machine-readable
object on stdout ([docs/cli.md](docs/cli.md)).

| | Shortest path |
|---|---|
| **macOS** | `brew install ffmpeg`, then `vispek-hc setup` (compiles the camera-control helper; needs `xcode-select --install`) to lock camera parameters; `check`; `record`. Without the helper, `record --unlocked` |
| **Linux** | `apt install ffmpeg v4l-utils`; `sudo usermod -aG dialout $USER` and log in again; `check`; `record -o first` (built, not verified on a device yet) |
| **Windows** | `winget install ffmpeg`; install the CH341 serial driver; allow desktop apps to use the camera; `check`; `record -o first --unlocked` (parameter locking is not built yet) |

Details and what `--unlocked` costs you: [docs/platforms.md](docs/platforms.md).

## What the data looks like

A capture is a folder of lossless frames with `capture.json`. A cube has the raw frames
and one number per pixel and band:

```python
import json, numpy as np

z = np.load("cube.npz", allow_pickle=False)  # written by 'vispek-hc reflectance'
values = z["values"].astype(np.float32)  # (H, W, B), axes Y, X, B
valid = np.isfinite(values) & ((z["mask"] & 7) == 0)
rgb = z["rgb"]  # (H, W, B, 3) uint8 raw code values
band_ids, nm = z["band_ids"].tolist(), z["wavelengths_nm"]
meta = json.loads(str(z["metadata_json"]))
```

Or in Python from the start:

```python
import vispek_hc

capture = vispek_hc.capture("first-scan", simulate=True)  # drop simulate with hardware
cube = capture.cube()  # .values (H, W, bands), .rgb, .mask, .bands
```

**What the numbers are.** Pixel values are 8-bit code values after the camera's own image
processing. They are not linear radiance, relative reflectance is an index and not a
physical reflectance, and band wavelengths are nominal LED centre wavelengths, not a
sensor response. Read [docs/calibration.md](docs/calibration.md) before you compare two
scans.

## A viewer

[`vispek-hc-viewer`](https://github.com/vispek/vispek-hc-viewer) is a separate package
built on this one: a page served to your own computer with the live picture, LED
switches, scan and white-reference buttons, band pictures, indices and spectra. It
records ordinary capture folders, so the command line and the viewer read each other's
data. `pip install vispek-hc-viewer`.

## Documentation

- [AGENTS.md](AGENTS.md): for coding agents, and the quickest overview for people
- [docs/cli.md](docs/cli.md), [docs/api.md](docs/api.md): commands and Python names
- [docs/data-format.md](docs/data-format.md), [docs/calibration.md](docs/calibration.md):
  files and numbers
- [docs/integration-guide.md](docs/integration-guide.md): **writing your own control
  software, in any language, without this package**: cables, the serial protocol with
  reference code, camera settings, what the device does that spoils data, how to record
  and calibrate. Self-contained; hand it to your coding agent
- [docs/hardware.md](docs/hardware.md), [docs/protocol.md](docs/protocol.md): the device
- [docs/platforms.md](docs/platforms.md), [docs/troubleshooting.md](docs/troubleshooting.md)
- [examples/](examples/): 13 short scripts that run offline

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Trademarks: [TRADEMARKS.md](TRADEMARKS.md).
