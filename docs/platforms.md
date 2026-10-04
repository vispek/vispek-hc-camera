# Platforms

On macOS (Apple silicon) a real HC1500 passed the acceptance checks: serial port,
frames, parameter lock, scans, a white reference ([acceptance/](acceptance/README.md)).
**Nothing has been verified on Linux or Windows**: what is listed as built there is
tested against recorded tool output, not against a device. This page says what is built
and what you have to arrange yourself.

| | macOS, Apple silicon | macOS, Intel | Linux | Windows |
|---|---|---|---|---|
| Tier | 1 | 2 | 2 | 2 |
| Light source (serial) | built | built | built | built |
| Finding the serial port | built | built | built | built |
| Frames through ffmpeg | built | built | built | built |
| Finding the camera | by name | by name | by USB id, among the capture nodes | by name, not verified |
| Locking camera parameters | built (uvc-util) | built (uvc-util) | built (`v4l2-ctl`), not verified | **not built** |
| `check` reports `ready` | yes | yes | yes, not verified | never, for now |

Tier 1 is where the hardware acceptance will run. On tier 2, frames and LEDs are meant
to work; parameter locking is best effort.

Requirements everywhere: Python 3.11 or newer and `ffmpeg` on the `PATH`. The OpenCV
capture backend is not built yet, so the `[opencv]` extra installs OpenCV and nothing
uses it.

## Without parameter locking (Windows today, and wherever the control tool is missing)

A scan insists that automatic white balance is off, exposure is manual and the gain the
camera keeps was settled in darkness, and it cannot confirm any of it without a way to
read and set the camera. So on Windows, and on macOS or Linux without the control tool:

- `vispek-hc record ... --unlocked` scans anyway and records `controls_locked: false`.
- White balance or exposure may move between LEDs or between scans. Fix them with
  another tool first if you can (`v4l2-ctl` on Linux); the software still cannot see
  that you did.
- Relative reflectance from such scans needs `--force`: a scan whose camera settings
  nobody could read matches no white reference ([calibration.md](calibration.md)). Treat
  the result as a picture, not a measurement.

## macOS

```bash
brew install ffmpeg
xcode-select --install          # compiler for the camera-control helper, once
vispek-hc setup                 # compiles uvc-util into VISPEK_HC_HOME/bin/
vispek-hc check
```

- **Camera permission.** The first time frames are read, macOS asks whether your
  terminal (or editor) may use the camera. Until you allow it, no frame arrives and the
  command fails with `PERMISSION_DENIED` or `FRAME_INCOMPLETE`. Allow it under System
  Settings, Privacy & Security, Camera, then run the command again. A program that
  cannot show that question (a command started by another application, for example)
  gets no frames and no error message either: run it from a terminal that has access.
- **Camera controls.** `uvc-util` (MIT, by Jeffrey Frey, vendored unmodified under
  `tools/uvc-util/`) reads and sets UVC controls. Its source files come with the package;
  `vispek-hc setup` checks them against their checksums and compiles them into
  `VISPEK_HC_HOME/bin/uvc-util`. Nothing is downloaded and no binary is distributed. It
  is looked for in `VISPEK_HC_UVC_UTIL`, there, in `tools/uvc-util/build/` of a source
  checkout (`tools/uvc-util/build.sh`), and on the `PATH`. Without it `check` reports
  `CONTROLS_UNAVAILABLE` and scans need `--unlocked`.
- **Serial port.** `/dev/cu.usbserial-XXXX` or `/dev/cu.wchusbserial-XXXX`; the suffix
  follows the USB port you plug into. Recent macOS has a CH341 driver built in.
- macOS converts uncompressed frames before applications see them, so they are not
  guaranteed to be the camera's own bytes. They arrive in limited range (measured: black
  is Y = 20) and are converted accordingly. `check` measures the black level (it must be
  5) to catch a wrong conversion. The MJPEG path is re-encoded once on macOS and is
  recorded as `mjpeg_reencode`.

## Linux

```bash
sudo apt install ffmpeg v4l-utils
sudo usermod -aG dialout "$USER"     # then log out and in again
uv run vispek-hc check
```

- **Serial port.** `/dev/ttyUSB0` or similar. You need to be in the `dialout` group
  (`uucp` on some distributions); otherwise opening the port fails with
  `PERMISSION_DENIED`.
- **brltty.** The braille display service claims CH341 adapters on some distributions,
  and the port then vanishes a moment after plugging in. Remove it
  (`sudo apt remove brltty`) or disable its udev rule.
- **Camera.** A UVC camera creates two device nodes, one for pictures and one for
  metadata. The software takes the picture node whose USB id is the HC1500's
  (`0c45:6370`), read from `/sys/class/video4linux`; `--camera /dev/videoN` names
  another.
- **Camera controls.** `v4l2-ctl` (package `v4l-utils`) reads and sets them, on the node
  the frames come from. The kernel renamed several controls over the years, so the
  software goes by their numbers. The exposure mode is translated: V4L2 numbers the
  modes 0-3, UVC and this software 1, 2, 4, 8. Without `v4l2-ctl`, `check` reports
  `CONTROLS_UNAVAILABLE` and scans need `--unlocked`.
- **`check`** reports whether you are in the `dialout` group and whether brltty is
  installed. `tools/99-vispek-hc.rules` is a udev rule that opens the serial port to
  every local user instead; read its comments before installing it.
- Whether the uncompressed stream is limited range on Linux as it is on macOS is not
  known. `check` measures the black level (it must be 5) and says so if the conversion
  does not fit.

## Windows

```powershell
winget install ffmpeg
uv run vispek-hc check
```

- **Serial driver.** The light source appears as a COM port once the CH341 driver from
  the chip's maker (WCH) is installed. Without it the device shows up under "Other
  devices" in Device Manager and `check` finds no port.
- **Camera privacy.** Settings, Privacy & security, Camera: "Let desktop apps access
  your camera" must be on.
- **Camera name.** Cameras are listed with `ffmpeg -list_devices true -f dshow -i dummy`
  and the HC1500 is taken by its name; `--camera` names another.
- **Camera controls are not built.** Windows applications set them through DirectShow
  (`IAMCameraControl`, `IAMVideoProcAmp`), where exposure is a power of two seconds
  (-6 is 15.6 ms). Until that is built and tried on a device, `check` reports
  `CONTROLS_UNAVAILABLE` and scans need `--unlocked`.
- Closing the console window ends the process without a chance to switch the LEDs off
  reliably. End a command with Ctrl-C instead.

## Where settings are kept

Per-device profiles (confirmed camera parameters, timing, PWM values) live in
`VISPEK_HC_HOME`, which defaults to:

| Platform | Folder |
|---|---|
| macOS | `~/Library/Application Support/vispek-hc/` |
| Linux | `$XDG_CONFIG_HOME/vispek-hc/` (`~/.config/vispek-hc/`) |
| Windows | `%APPDATA%\vispek-hc\` |

`vispek-hc check --json` prints the folder in use (`data.home`). Apart from that and a
temporary log file of ffmpeg in the system's temporary folder, nothing is written
outside the folders you name on the command line.

## Exposure is quantised

`exposure-time-abs` counts in units of 100 µs; the default 157 is 15.7 ms. A camera may
settle on a neighbouring value. The software accepts a value the camera holds steadily,
records both what was asked and what was got, and binds calibrations to what was got.
