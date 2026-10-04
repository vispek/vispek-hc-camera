# First contact with a real HC1500

**Date:** 2026-10-02. **Software:** 0.1.0.dev0 (commit `360f3bc` plus the changes this
record led to). **Computer:** macOS on Apple silicon, ffmpeg 9.0, uvc-util built from
`tools/uvc-util`. **Device:** one HC1500 in a dark box, a small circuit board about 30 cm
in front of the lens, USB1 and USB3 connected. Only LED 6 (520 nm) was lit.

Not the acceptance run of the [runbook](hardware-runbook.md): single checks, made to get
the device talking. No scan was recorded.

## What was measured

| | Result |
|---|---|
| Devices found | one serial port (`/dev/cu.usbserial-*`), camera `LRCP 1080P-60FPS`; no ambiguity |
| Baud rate | 115200, found by the first probe |
| Replies | ON `3d 01 01 00 29 0d 0a`, OFF `3d 02 01 00 94 0d 0a`: as the protocol sheet says |
| Reply delay | 7-9 ms (median 7.4-7.8 ms over three runs) |
| Reply loss | 0 of 28 commands with the camera idle; 2 of 37 and 1 of 22 with the camera streaming. Every lost reply was answered on the resend |
| All off on leaving | confirmed for all 17 LEDs |
| Frames | 1280 x 720 `yuyv422` through ffmpeg (AVFoundation), 10.03 fps measured |
| Parameter lock | white balance off, exposure manual, `exposure-time-abs` 157 read back as 157; every requested value held |
| Dark frame, uncompressed path | every pixel `Y = 20`, no variation at all, locked or not |
| Dark frame, JPEG path (`--source mjpeg`, re-encoded on macOS) | every pixel 5, 5, 5 |
| LED 6 at PWM 50, dark circuit board | mean of the picture +3.4 code values in green (full-range numbers) |
| LED 6 at PWM 224 | median 31, 76, 33 (full-range numbers); some pixels clip |
| ON reply to first frame showing light | 0.357 s and 0.354 s; that frame is lit in its lower half only |
| ON reply to first whole lit frame | 0.461 s and 0.446 s |
| OFF reply to first whole dark frame | 0.442 s and 0.418 s |

The frame-by-frame series behind the last four rows is
[2026-10-02-hc1500-led6-series.json](2026-10-02-hc1500-led6-series.json): mean and median
per frame, with the times of the ON and OFF replies (clock seconds).

## Conclusions

1. **The uncompressed frames are limited range.** Black is 20 there and 5 through the
   JPEG path; (20 - 16) x 255 / 219 = 4.66. The conversion default changed from full
   to limited range. Whether the camera or macOS produces limited range is not known;
   Linux and Windows will show.
2. **The black level is 5 and carries no noise**, as the specification says: a clamp.
3. **Light takes about 350 ms to show, 450 ms to fill a frame**, twice what the
   demonstration data showed. The scan measures this per unit with the pairing probe, so
   only the value used when no probe has run was wrong: 300 ms became 500 ms.
4. **The pairing probe was too faint for a dark sample.** At PWM 50 the picture rose by
   3.4 code values against a threshold of 2; in an earlier run with room light in the
   box it did not pair at all. It now uses the PWM the unit scans LED 6 with, and waits
   for the picture to settle before taking its baseline.
5. **Without camera access macOS gives no frames and says nothing.** A command started
   by an application that cannot show the permission question waits and fails with
   `FRAME_INCOMPLETE`. The hint of that error now names the camera permission first.
6. **Reply loss looks tied to the camera streaming** (0 of 28 without, 3 of 59 with).
   Too few commands to conclude; the reply-loss check of the runbook measures it.
   **It did, and this was wrong:** the share is the same without the camera
   ([acceptance run](2026-10-02-acceptance-leds-5-17.md)).

## Still open

Done since, in the [acceptance run](2026-10-02-acceptance-leds-5-17.md): `check` after
these changes, a scan, both pixel paths with clipping, reply loss. Open: the white
reference and scalar noise, which need the white board.
