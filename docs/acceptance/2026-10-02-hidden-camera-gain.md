# The camera keeps a gain it does not report

**Date:** 2026-10-02, afternoon. **Software:** 0.1.0.dev0 (commit `9a348cc`), through the
viewer's HTTP interface and `uvc-util`. **Device:** one HC1500 (`hc1500-1`), in its dark
box, lid closed. **In view:** what the operator had left in the box, unchanged during
the test. Only LEDs 5, 6, 8 and 9 were lit, one at a time.

## The question

Since the unplug test everything was about four times brighter than in the morning at
the same PWM and the same camera parameters read back (see
[2026-10-02-uv-leds.md](2026-10-02-uv-leds.md), point 6). USB2 had been unplugged and the
camera cable pulled twice in between, so the cause could be either.

## What was done

1. **USB2 plugged in again**, nothing else changed: LED 9 at PWM 144 still clipped
   (98 % of the pixels). USB2 is not the cause.
2. **Noise of the two white references** (same paper, centre 300 x 200 pixels, adjacent
   pixel difference / sqrt 2, the LED's own channel):

   | | LED 5 | LED 9 | LED 10 | LED 13 |
   |---|---|---|---|---|
   | morning: signal, noise (DN) | 159, 0.51 | 157, 0.45 | 175, 0.45 | 183, 0.56 |
   | afternoon: signal, noise (DN) | 209, 0.88 | 184, 0.97 | 202, 0.96 | 191, 0.92 |

   About twice the noise at a similar signal: what four times the gain on a quarter of
   the light gives. The recorded camera controls of the two captures are identical.
3. **Locking under different pictures.** The camera was put back to automatic exposure
   (`auto-exposure-mode` 8), shown a picture for some seconds, and locked again (manual,
   exposure 157, gain 0, as always). Then LED 9 was read, mean red of the frame:

   | The camera saw, while in automatic mode | LED 9 at PWM 36 | LED 9 at PWM 144 |
   |---|---|---|
   | darkness, 0.5 to 10 s (twelve times) | 167.5 - 167.7 | 254.9 (clipped) |
   | LED 5 at PWM 1023 (picture clipped), eight times | 37.7 - 38.1 (three times), 47.4 - 48.0 (five times) | 151 - 162 |
   | LED 6 at PWM 1023 (picture not clipped), three times | 49.2, 49.8, 51.7 | 143 - 164 |
   | LED 8 at PWM 1023 | 72.0 | 163.8 |
   | LED 9 at PWM 144 (red only: dim for the exposure meter), twice | 167.7, 204.7 | clipped |

   The morning's value, 141 at PWM 144, is the low state.
4. **In the locked state**, writing `gain` 100 raised the reading (247.8 at PWM 36) and
   writing 0 brought it back exactly; writing the exposure again, or
   `backlight-compensation`, changed nothing.

## Conclusions

1. **The camera has an internal gain that the automatic exposure sets and that stays
   where it was when automatic exposure is switched off.** It is not the `gain` control
   (that one works on top of it and reads 0 throughout) and no control reads it back.
   Between its ends lies a factor of about 4.4.
2. **The morning's captures were locked while the camera saw light** (low gain); since
   the camera was plugged in again, every lock happened in the closed dark box (highest
   gain). Neither USB2 nor the light source is involved.
3. **A lock in darkness is repeatable** (twelve locks within 0.2 DN, after as little as
   0.5 s of darkness). A lock under light depends on the picture and is not: the same
   bright picture gave two different states.
4. **Two captures are only comparable if no lock in another state lies between them.**
   The binding of a calibration cannot see this: every recorded setting is the same.
   Until the lock procedure makes the state certain, connect and lock with the lid
   closed and every LED off, and record the white reference again after any lock that
   may have seen light.
5. The read-out noise is about twice as high in the high-gain state at the same signal.
   In exchange LEDs 7 and 17, too weak in the low-gain state (about 60 DN at full
   power), reach about 180 and 225 DN.

## Also seen

While USB2 was being plugged in, the camera stream ended and was reopened, one serial
command failed, and afterwards the camera was in automatic exposure although the
session still called it locked. A reopened stream must be followed by a new lock; this
is not done yet.

## The new lock, on the device

Software at commit `07e4e26`, through the viewer. LED 9 at PWM 36, mean red of the frame:

| Step | Reading |
|---|---|
| The camera left in the low state by the session before; connect | 167.8 |
| The camera forced into the low state with `uvc-util`, behind the viewer's back | 66.9 |
| "Lock again" (2.3 s) | 167.8 |
| "Lock again" while LED 9 was lit: the LED was switched off first | 167.9 |

Not tried: the refusal with the lid open (it needs a hand on the lid).

## Open

- Done the same day: the lock is taken in darkness only (every LED off, automatic
  exposure for a second, the picture at most 12 in every channel), refused otherwise
  (`GAIN_NOT_CONFIRMED`), recorded in every capture (`camera_gain_state`) and bound to
  calibrations; a reopened stream is locked anew. See the changelog, "Defaults".
- Ask the hardware side whether the camera has a control for this gain (a vendor
  extension unit), which would allow the low-noise state on purpose.
