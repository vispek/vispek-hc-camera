# The HC1500

Facts about the device that the software is built on. Each is marked by where it comes
from: **data sheet** (Vispek's technical data sheet and protocol sheet), **observed**
(measured on demonstration data recorded on 2026-09-16), **measured** (by this software
on a device, 2026-10-02; see [acceptance/](acceptance/README.md)), or **to be
confirmed** (waiting for the hardware team).

## The unit

| | |
|---|---|
| Size, weight, housing | 60 x 60 x 20.5 mm, about 300 g, anodised aluminium (data sheet) |
| Power | 6 W working, 1 W standby (data sheet) |
| Operating range | -30 to +70 °C, up to 80 % relative humidity (data sheet) |
| USB1, Type-C | light-source power 5 V 0.5 A, and **the serial port** that switches the LEDs |
| USB2, Type-C | light-source power 5 V 2 A, meant for the UV LEDs (data sheet). **They light without it** (measured). The software cannot detect whether it is connected |
| USB3 | the camera, 5 V 0.5 A. **It powers the light source as well**: with USB1 and USB2 unplugged, an LED that was lit stayed lit at the same brightness (measured) |

Neither the serial chip nor the camera has a serial number. A unit is therefore known to
the software by a label you give it (`--unit`, default `hc1500-1`); the USB location is
recorded as evidence only, because it changes with the port you plug into.

## The light source

17 LED channels, switched one at a time over the serial port
([protocol.md](protocol.md)). Brightness is PWM at 16 kHz with 1024 steps (0-1023).
Centre wavelengths are stable to 1-3 nm (data sheet).

| LED | Wavelength | Strongest camera channel | Starting PWM | Notes |
|---|---|---|---|---|
| 1 | 255 nm | B | 458 | **UV-C.** Looks the same as LED 2 to the camera; their order is to be confirmed |
| 2 | 275 nm | B | 300 | **UV-C** |
| 3 | 370 nm | B | 74 | UV-A |
| 4 | 400 nm | B | 74 | violet |
| 5 | 455 nm | B | 24 | |
| 6 | 520 nm | G | 224 | |
| 7 | 560 nm | G | 600 | |
| 8 | 610 nm | R | 74 | |
| 9 | 630 nm | R | 36 | |
| 10 | 667 nm | R | 18 | |
| 11 | 700 nm | R | 36 | |
| 12 | 804 nm | R | 74 | |
| 13 | 850 nm | R | 74 | LEDs 13-17 look alike to the camera; their order is to be confirmed |
| 14 | 890 nm | R | 75 | |
| 15 | 933 nm | R | 75 | |
| 16 | 973 nm | R | 150 | |
| 17 | 1050 nm | R | 300 | |

LED *n* is the *n*-th entry of the data sheet's ascending wavelength list. This table is
`vispek_hc.HC1500` (`led_table.version` `hc1500-nominal-1`, `verified: false` until the
hardware team signs it off). Every capture records the table version it was made with.

- **Wavelengths are nominal LED centre wavelengths**, not what the sensor responds to.
  Bands are identified by `band_id` (`led_01` ... `led_17`), never by wavelength.
- The strongest channel and the starting PWM values were observed on one white board at
  one distance. A white reference decides the channel again for your setup, and
  `reference white --auto-pwm` or `tune` find PWM values for it. The PWM a channel needs
  differs by a factor of about 50 between channels; there is no PWM that suits all.
- The sensor responds from 300 nm. What LEDs 1 and 2 show is very likely fluorescence of
  the sample, not reflected UV (observed: bare metal reads 0 in both, paper reads close
  to the white board). To be confirmed with a filter.
- LED output drifts with temperature and time: one hour and 45 minutes after its white
  reference, the same sample read 0.977-0.99 of it (observed).

## The camera

| | |
|---|---|
| Interface | USB video class (UVC 1.1), VID:PID `0c45:6370`, name `LRCP 1080P-60FPS` (observed) |
| Sensor | colour, 1928 x 1088, 2.9 µm pixels, 1/2.8", responds 300-1100 nm (data sheet) |
| Output | MJPEG or YUY2, both with 4:2:2 chroma: only luma has full horizontal resolution |
| Modes the software uses | 640x480, 1280x720, 1920x1080; default **1280x720 at 10 fps, uncompressed** |
| Lens | manual focus; 2.5 mm, 130° by default. The UVC `focus-abs` read-back is not the lens position |
| Shutter | rolling |

Observed behaviour that shapes the software:

- **The dark frame is a constant 5** in all three colours (measured: every pixel
  identical): the black-level clamp of the camera's processing. On the uncompressed path
  that is Y = 20 in limited range (16-235), which the software stretches to 0-255. It carries no per-pixel information. "Nearly black" is therefore
  judged against 5, not 0.
- The output has passed gamma and a colour matrix. Under red light the green channel
  falls below the black level. **Values are not linear.**
- **The camera remembers the previous picture.** Its processing carries a state from one
  frame to the next that shifts some bands by 23 code values: LED 9 reads 118 when it is
  lit straight after LED 8, and 141 after darkness (measured; why is not known). After
  one whole frame of darkness the state is always the same, so every channel of a scan
  is preceded by a dark pause of one frame period plus exposure plus 50 ms
  ([acceptance record](acceptance/2026-10-02-white-board-and-camera-state.md)).
- **The camera keeps a gain it does not report.** Automatic exposure sets an internal
  gain that stays where it was when automatic exposure is switched off; the `gain`
  control reads 0 throughout. Locked in darkness, every value is 4.4 times what it is
  locked under bright light, and only the lock in darkness is repeatable (measured:
  twelve locks within 0.2 code values). The software therefore locks in darkness only
  ([acceptance record](acceptance/2026-10-02-hidden-camera-gain.md)).
- Opening or reopening the video stream can reset the camera's parameters to automatic,
  so they are locked on the running stream and read back, before every scan.
- Uncompressed 720p needs most of what USB 2.0 carries, which limits it to about 10 fps
  (10 fps measured on macOS). With the measured delay of the picture and the dark pause
  before every channel, a channel takes about 1.1 s: 15 s for LEDs 5-17 (measured),
  about 19 s for all 17. MJPEG at 60 fps (`--fast`) is quicker but its RGB values depend
  on the decoder.
- 1080p "at 60 fps" delivered about 5.6 fps on macOS (observed). The software measures
  the frame rate from arrival times and reports it.

Camera parameters of the demonstration data, which are the starting profile: manual
exposure, `exposure-time-abs` 157 (units of 100 µs, so 15.7 ms), gain 0, automatic white
balance off at 4600 K, gamma 100, saturation 66, contrast 32, brightness 0. Sharpness is
set to 0 instead of the camera's 3: sharpening invents signal at edges.

## A scan is a sequence, not a moment

A cube is 17 frames taken one after another. Sample and camera must not move for the
duration.

After an LED is switched on, the first frame that shows it was being read out while the
light changed: its upper part is dark and its lower part lit. The next frame is stable.
Measured through ffmpeg on macOS at 10 fps: the half-lit frame arrives about 350 ms
after the device's reply and the first whole frame about 450 ms after it; switching off
looks the same. (The demonstration data, recorded with other software, showed about
180 ms and 270 ms.) The scan therefore waits until light can have reached a
whole frame, and then accepts a frame only once it agrees with the one before
([architecture.md](architecture.md), "Scan").

| Physical fact | What the software does about it |
|---|---|
| PWM at 16 kHz against a rolling shutter gives row-to-row ripple of about 62.5 µs / exposure time | `check` and every scan warn when `exposure-time-abs` is below 63 (ripple above 1 %) |
| The serial reply is sometimes lost (observed: 4 of 68 commands; measured: 3 of 59 while the camera was streaming, none of 28 without it; each answered on the resend). Replies take 7-9 ms (measured) | Three attempts of 200 ms per command; a late reply is purged before the next command |
| USB1 supplies 0.5 A for the serial chip and the LEDs | One LED at a time. `led on --all` warns about the power limit |
| Strong vignetting of the 130° lens | A white region (`--white-roi`) says where a calibration has a reference behind it |
| The USB connection can drop | The stream is reopened; a scan in progress fails its group and keeps its frames |
