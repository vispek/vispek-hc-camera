# Data formats

Four formats, each with a JSON Schema in [`schemas/`](../schemas/):

| What | Identifier | Schema |
|---|---|---|
| a capture folder (`capture.json`) | `vispek-hc.capture/1` | `capture-1.schema.json` |
| a calibration folder (`calibration.json`) | `vispek-hc.calibration/1` | `calibration-1.schema.json` |
| a cube file (`cube.npz`, its `metadata_json`) | `vispek-hc.cube/1` | `cube-metadata-1.schema.json` |
| what a command prints with `--json` | `vispek-hc.cli/1` | `cli-envelope-1.schema.json` |

**Stability.** File formats are more stable than code. A reader of `/1` accepts every `/1`
file: fields are only ever added, and unknown fields are ignored. A change that breaks
readers becomes `/2`. An unknown major version is refused with `SCHEMA_UNSUPPORTED`.

## The cube in memory

`vispek_hc.Cube`, axis order always **Y, X, band**:

| Field | Type | What it is |
|---|---|---|
| `rgb` | uint8 (H, W, B, 3) | the frames as they came from the camera: the data |
| `dark_rgb` | uint8 (H, W, 3) | the dark frame of the same scan |
| `values` | float32 (H, W, B) | one number per pixel and band (see below) |
| `mask` | uint8 (H, W, B) | quality bits |
| `bands` | list of `Band` | `band_id` (`led_05`), `name`, `center_nm`, `dominant_channel` |
| `metadata` | dict | `layer`, `unit`, `signal_domain`, `reduction`, where it came from |

`values` is derived from `rgb` by `reduction`:

- `dominant` (default, `signal_domain: dominant_channel_v1`): the camera channel that
  responds most strongly to that LED. On the white board it reaches 132-184 code values,
  so one code value is 0.5-0.8 % of the signal.
- `luma` (`luma_bt601_v1`): integer BT.601 luma, `(19595 R + 38470 G + 7471 B + 32768) >> 16`.
  Kept for comparison with older data; its white signal is only 33-128 code values.

Layers: `L1` is raw code values (`unit: image_code_value`, dark not subtracted). `L2` is
relative to a white reference (`unit: relative_to_white`); see
[calibration.md](calibration.md).

Mask bits:

| Bit | Name | Meaning |
|---|---|---|
| 1 | `INVALID` | missing or not a number |
| 2 | `SATURATED` | a colour channel or the scalar is clipped (>= 255, raw, before dark subtraction) |
| 4 | `SMALL_DENOMINATOR` | the white reference is too dark there to divide by |
| 8 | `OUT_OF_RANGE` | relative value outside [0, 1]; the value is kept |
| 16 / 32 / 64 | `RED_` / `GREEN_` / `BLUE_CLIPPED` | which colour channel clipped |
| 128 | `SCALAR_CLIPPED` | the band's scalar clipped |

A pixel counts in statistics when its value is finite and `(mask & 7) == 0`
(`Cube.valid`).

## A capture folder

```
my-scan/
├── capture.json            what was recorded and how
├── events.jsonl            what happened, one JSON object per line
├── g0001/                  one scan ("group")
│   ├── dark.png            every LED off
│   ├── led05_455nm.png     one frame per channel
│   ├── led06_520nm.png
│   └── rejected/           white reference only: up to three frames that showed no light
├── g0002/                  further scans (--average, --cycles)
└── preview.png             written by `record` for people; nothing reads it back
```

A capture holds the **source frames only**. Cubes, averages and reflectance are computed
when a capture is loaded and are never stored in it. Frames are lossless PNG on the
uncompressed path, or the camera's own JPEG bytes (`.jpg`) on the MJPEG path. The nm in
a file name is for people; the LED number is what counts.

Modes other than `scan`: `single` and `dark` hold one `frame.png`; `video` holds
`frames/000001.png ...` and `frames.jsonl`, one frame entry per line.

### capture.json

```json
{
  "schema": "vispek-hc.capture/1",
  "capture_id": "4f00536c-2747-439b-b1aa-a50044d4e68c",
  "software": {"name": "vispek-hc", "version": "0.1.0", "decoder": {"library": "Pillow", "...": "..."}},
  "model": "HC1500",
  "unit_id": "hc1500-1",
  "led_table": {"version": "hc1500-nominal-1", "verified": false},
  "signal_domain": "dominant_channel_v1",
  "reduction": "dominant",
  "recording": {"mode": "scan", "role": null, "cycles": 1, "average": 1,
                "started_at_utc": "...", "finished_at_utc": "...",
                "status": "complete", "groups_done": 1},
  "device": {"camera": {"name": "...", "backend": "ffmpeg", "source": "uncompressed"},
             "serial": {"port": "...", "baud": 115200},
             "controls": {"backend": "uvc-util", "device_id": "..."}, "simulated": false},
  "config": {"channels": [5, 9], "pwm_by_led": {"5": 24, "9": 36},
             "min_on_ms": {"source": "auto", "by_led": {"5": 310, "9": 310}},
             "actual_size": [1280, 720], "actual_fps": 10.0, "pixel_format": "yuyv422",
             "exposure": {"requested": 157, "effective": 157}, "gain": 0,
             "controls_locked": true, "camera_controls": {"...": "..."}, "...": "..."},
  "groups": [
    {"group_id": "g0001", "status": "complete", "failure": null,
     "dark": {"file": "dark.png", "sha256": "...", "median_rgb": [5, 5, 5], "...": "..."},
     "frames": [
       {"led_id": 5, "nm": 455, "pwm": 24, "min_on_ms": 310,
        "file": "led05_455nm.png", "sha256": "...", "bytes": 412345, "encoding": "png",
        "width": 1280, "height": 720, "host_frame_counter": 26,
        "received_at_utc": "...", "received_mono_ns": 3114033333,
        "on_ack_at_utc": "...", "selected_at_utc": "...",
        "frame_check": {"attempts": 1, "observed_frames": 2, "on_ack_to_selected_ms": 465.0,
                        "mean_difference": 0.0, "large_change_fraction": 0.0,
                        "max_line_difference": 0.0, "rgb_saturated_fraction": 0.0,
                        "response_dn": 139.0, "suggested_min_on_ms": 350}}
     ]}
  ],
  "notes": ""
}
```

- `recording.status` is `partial` while recording and after anything that ended it early;
  only a recording that finished is `complete`. `recording.role` is `white` for a white
  reference, `dark` for a dark frame.
- A group is `complete`, `partial` (interrupted) or `failed` (with `failure`: `code`,
  `message`, `led_id`). **Only complete groups become cubes**; `average` of them at a
  time form one output.
- `config` holds every scan setting as it was used, the camera parameters read back
  before (`camera_controls`) and after (`camera_controls_after`) the scan, and
  `controls_locked`: whether white balance and exposure were confirmed fixed and the gain
  the camera keeps was settled. `camera_gain_state` is `dark` (the lock was taken in
  darkness: the one repeatable state) or `undetermined`; `lock_dark_level` is the picture
  the camera saw just before that lock. Captures without the key are older than it.
- Times are ISO 8601 with milliseconds and `+00:00`. `received_*` is when the frame
  reached the computer; `on_ack_at_utc` is when the device confirmed the ON.
- `frame_check` says how the frame was chosen: how many frames were looked at, how far it
  differed from the one before, how much of it clipped, and how far the LED's own channel
  rose above the dark frame (`response_dn`).

### events.jsonl

One line per event, appended and flushed as it happens: `recording_started` (carries the
whole document as it stood, so the capture can be rebuilt), `led_command` (bytes sent and
received, attempts, duration), `frame_selected`, `frame_rejected`, `timing_history`,
`channel_timing`, `saturation_warning`, `no_led_response`,
`camera_control_verification`, `group_finished`.

### Crash safety

Each frame is written to a temporary file and renamed. `capture.json` is rewritten the
same way after every frame, with `status: partial`. After a crash, everything recorded up
to the last frame is readable. If `capture.json` itself is lost or damaged,
`vispek-hc repair DIR` rebuilds it from `events.jsonl` and the frame files (and keeps the
old file as `capture.json.bak`).

### Reading a capture safely

`load_capture` refuses frame names that are not plain file names inside the capture,
group folders other than `g0001`-style names, links that lead out of the folder, and
pictures larger than 4096 x 4096. `load_capture(DIR, verify=True)` checks every frame
against its recorded sha256.

### What a capture folder reveals

Relative file names only. No user name, no host name, no environment variables, no
absolute paths. It does record the serial port's name (for example
`/dev/cu.usbserial-1420`), the camera's name and USB location, the unit label and setup
label you chose, and `notes`, which is your own text. Check those before you share a
capture.

## cube.npz

Written by `Cube.save` and `vispek-hc reflectance`, with `numpy.savez_compressed`; it is
readable with `allow_pickle=False`:

| Key | Content |
|---|---|
| `schema` | `"vispek-hc.cube/1"` |
| `rgb`, `dark_rgb`, `values`, `mask` | as in the table above |
| `band_ids` | `led_05`, ... |
| `wavelengths_nm` | float32, nominal |
| `dominant_channel` | `R` / `G` / `B` per band |
| `axes` | `"YXB"` |
| `layer`, `unit`, `signal_domain`, `reduction` | as strings |
| `metadata_json` | everything else, as one JSON string (schema `cube-metadata-1`) |

```python
import json, numpy as np

z = np.load("cube.npz", allow_pickle=False)
values = z["values"].astype(np.float32)  # (H, W, B), axes Y, X, B
valid = np.isfinite(values) & ((z["mask"] & 7) == 0)
rgb = z["rgb"]  # (H, W, B, 3) uint8 raw code values
band_ids, nm = z["band_ids"].tolist(), z["wavelengths_nm"]
meta = json.loads(str(z["metadata_json"]))
```

At 1280 x 720 with 17 bands a compressed cube is roughly 40 MB.

## Calibration folder

`calibration.json` (settings the white reference is bound to, white region, quality per
band, the camera channel chosen per LED) and `reference.npz` (`white_rgb`,
`white_dark_rgb`, `white_mask`, `band_ids`, `wavelengths_nm`). See
[calibration.md](calibration.md).

## Export

- **ENVI** (`vispek-hc export --format envi`): `data.bsq` (float32, little-endian, band
  sequential) with `data.hdr`, `quality-mask.bsq` (uint8) with its header, and
  `metadata.json`. The header starts with `ENVI` and carries `band names`, `wavelength`
  and, when the red, green and blue bands are present, `default bands`.
- **TIFF** (`--format tiff`): `data.tif`, one float32 page per band, with the metadata as
  JSON in its `ImageDescription`; `quality-mask.tif` beside it.
- **CSV** (`vispek-hc spectrum --csv`): UTF-8, one row per region and band, `.` as the
  decimal point, undefined statistics left empty. Columns: `capture_id`, `group_id`,
  `roi_id`, `name`, `geometry_json`, `layer`, `unit`, `signal_domain`, `band_id`,
  `center_nm`, `count`, `total`, `valid_fraction`, `mean`, `std` (population), `median`,
  `min`, `max`.

## Coordinates

Pixel coordinates have their origin top left; x is the column, y the row. Rectangles are
half-open, `[x, x + w) x [y, y + h)`, and clipped to the picture. A polygon contains the
pixels whose centre `(x + 0.5, y + 0.5)` lies inside it (even-odd rule). A normalised
region `x, y, w, h` (fractions of the picture, as in `--white-roi`) becomes pixels by
converting **both edges**: `x0 = floor(x * W)`, `x1 = floor((x + w) * W)`. Converting the
width instead gives a rectangle one pixel different, so the result is stored alongside
(`white_roi_px`).

## Unit profile

`units/<unit>.json` in the configuration folder (`VISPEK_HC_HOME`; see
[platforms.md](platforms.md)) holds what was learned about one device: the camera
parameters that were confirmed, the pipeline latency and frame rate measured by the
pairing probe, and the PWM values and on-times stored by `reference white --auto-pwm`
and `tune`. Delete the file to start afresh.
