# Calibration and what the numbers mean

## What a value is

- Pixel values are 8-bit code values after the camera's own image processing (and, on the
  MJPEG path, after JPEG). They are **not linear radiance**. Relative reflectance `R` is
  an index, not a physical reflectance.
- Nothing is normalised by exposure or PWM: the light an LED gives is not proportional
  to its PWM and drifts with temperature. `R` is comparable only between scans made with
  the same exposure, gain, per-channel PWM and timing.
- The dark frame sits at a constant 5: that is the black-level clamp of the camera's
  processing. It carries no per-pixel information and is not sensor noise.
- What the 255 and 275 nm bands show is not confirmed by the hardware side yet (very
  likely fluorescence). Materials that do not fluoresce, such as metal, can have a
  negative `R` in these two bands; that is expected.
- Wavelengths are nominal LED centre wavelengths, not the sensor's response. The order
  of LEDs 13-17 is not confirmed, and the sign of NDVI and NDWI depends on it.
- Record in a dark box or shielded from other light. The dark frame of a scan removes
  the background of that moment; `record --mode dark` with the lens covered shows how
  much light leaks in.
- A white reference can be reused only for the same device, lens, distance, PWM,
  exposure and timing. It drifts with LED temperature: 1-2 % after 1 h 45 min in the
  demonstration data.

## The formula

    R = (S - D_S) / (W - D_W)

per band and pixel, on one scalar per band (`reduction`): by default the camera channel
in which the white reference is strongest for that LED (`dominant`), or BT.601 luma
(`luma`, the legacy choice). `S` and its dark frame `D_S` come from the sample scan, `W`
and `D_W` from the white reference. No separate dark reference is needed.

- Where `W - D_W` is not above max(8 DN, 5 % of the band's median `W - D_W` inside the
  white region), the result is NaN with mask bit 4: dividing by almost nothing means
  nothing.
- Values outside [0, 1] are kept and flagged with mask bit 8.
- The masks of sample and white are merged. Pixels with bit 1, 2 or 4 are left out of
  statistics.
- With a white region (`white_roi`), only pixels inside it have a reference behind
  them. Pixels outside are still computed.
- The legacy definition (`reflectance --legacy DARK_CAPTURE`, `covered_dark=` in Python)
  exists only to compare with old data: luma, one covered dark frame for sample and
  white, a fixed 1 DN floor.

## Making a white reference

```bash
vispek-hc reference white -o white [--auto-pwm] [--white-roi 0.2,0.2,0.6,0.6]
vispek-hc calibrate --white white -o calibration
```

Several white scans can be given; they must have been made with the same settings and
are averaged. A calibration is refused (`CALIBRATION_QUALITY`) when, inside the white
region (the whole picture without one), a band has

- fewer than 95 % of its pixels both unsaturated and above the denominator floor, or
- more than 2 % saturated pixels.

Warnings, without refusal: a white signal below 40 DN (one code value is then 2.5 % or
more), a dark frame whose 99th percentile is above 12 DN (light is leaking in), white
scans more than 24 h apart, camera parameters that were not locked, a capture that was
not recorded as a white reference.

## Binding: when a calibration may be applied

`calibration.json` stores the settings of its white scans under `binding`. A sample
must match in every one of

`unit_id`, `led_table_version`, `actual_size`, `source`, `pixel_format`, `color_range`,
`exposure`, `gain`, `reduction`, `dark_pause_ms`, `camera_gain_state`, `camera_controls_values` (every control
except the focus read-back), and, for each of the sample's channels, `pwm_by_led` and
`min_on_by_led`.

`camera_gain_state` must be `dark` on both sides: the camera keeps a gain from the moment
its parameters were locked, and only a lock taken in darkness is repeatable
([measured](acceptance/2026-10-02-hidden-camera-gain.md)). A capture made before the
state was recorded, or locked under light, matches nothing.

`dark_pause_ms` is the darkness before each channel. The camera carries state from the
frame before, so a reference recorded with another pause reads some bands differently
(measured: LED 9 by 20 %). A capture or calibration made before this key existed has
none and matches only captures that have none either: record the white reference again.

The sample's channels must be among the calibration's. Anything else is
`CALIBRATION_MISMATCH`, with every differing key in `details.differing`. `--force`
(`force=True`) applies the calibration anyway and writes the keys into the result's
metadata (`calibration.binding_overridden`); such a result is not comparable.

`apply_calibration(..., partial=True)` accepts a sample with more channels than the
calibration has, for example a scan of LEDs 1-17 and a white reference of LEDs 5-17
(office paper fluoresces under UV and cannot be the reference for LEDs 1-4). The result
holds the covered bands only; the others are listed in `calibration.bands_left_out`. The
settings of the covered channels must match as always, and at least one band must be
covered. The command line does not use it.

Exposure (the value the camera settled on), gain and the camera controls must also be
known: a scan made where the camera cannot be read (`--unlocked` without a control
backend, which today means Linux and Windows) matches nothing, and its reflectance
needs `--force`.

Timing comes from the unit's profile. The pipeline latency is stored by the first
pairing probe and kept; `tune` measures it again and stores new on-times and PWM
values, so white references made before a `tune` no longer bind. The white scan that
`tune` itself records is made before those on-times are stored: record the reference
to use with `reference white` afterwards.

Three entries are evidence, not conditions: `device_id`, `setup_label` and `actual_fps`
(the frame rate as measured, which differs in its last digits from session to session).
A different `device_id` or `setup_label`, and a reference more than 24 h away from the
sample, become warnings in the result's metadata.

## What a result carries

A relative cube (layer `L2`, unit `relative_to_white`) records under
`metadata.calibration`: the calibration folder's name, `calibration_id`, the sha256 of
`calibration.json` as it is on disk, the denominator floor per band, the white region in
pixels, what was overridden, the age of the reference in seconds, and the warnings.
