# White-board checks, and a camera that remembers the previous picture

**Date:** 2026-10-02. **Software:** 0.1.0.dev0 (commit `a7b380c` for the checks; the
change this record led to for the last table). **Computer:** macOS on Apple silicon.
**Device:** one HC1500 (`hc1500-1`) in a dark box. **Target:** a sheet of white A4 office
paper filling the picture, about 30 cm from the lens: not a reflectance standard (see
"What the paper is not"). Only LEDs 5-17 were lit.

## The four white-board checks

`VISPEK_HC_WHITE_BOARD=1 ... pytest -m hardware tests/hardware -k "white_reference or
scalar_noise or pixel_path"`: **4 passed**. Records:
[2026-10-02-hc1500-1-white-board-records.json](2026-10-02-hc1500-1-white-board-records.json).

| | Result |
|---|---|
| PWM found per LED (5-17) | 96, 896, 1023, 592, 144, 144, 144, 592, 592, 600, 600, 1023, 1023 |
| White signal (strongest channel, dark subtracted) | 115-174 code values, except LED 7 (57) and LED 17 (62), which stay below the wanted 150-235 at full power |
| Clipped pixels, pixels too dark to divide by | none in any band |
| The paper divided by itself (second scan) | 1.0 in every band |
| Strongest camera channel | as the model's table for LEDs 5-12; **blue, not red, for LEDs 13-17** (blue 2-5 % above red at these PWM values) |
| Pixel paths | uncompressed and JPEG: black 5, 5, 5; LED 9 peaks at 103, 158, 210, 255 for PWM 72, 144, 288, 576 |

Noise from frame to frame, per pixel, relative to the signal:

| LED | PWM | strongest channel: signal, noise, one code value | luma: signal, noise, one code value |
|---|---|---|---|
| 5 (455 nm) | 96 | 133, 0.96 %, 0.75 % | 31, 0.89 %, 3.2 % |
| 7 (560 nm) | 1023 | 58, 11 %, 1.7 % | 33, 18 %, 3.1 % |
| 9 (630 nm) | 144 | 139, 0.86 %, 0.72 % | 51, 0.92 %, 2.0 % |
| 13 (850 nm) | 592 | 169, 0.56 %, 0.59 % | 139, 0.34 %, 0.72 % |

## The camera remembers the previous picture

Comparing two scans of the same paper showed LED 9 at 1.18-1.24 of its white reference
while the other twelve bands were at 1.000. Watching the frames one by one:

| What the camera saw before | LED 9 at PWM 144, mean R / G / B |
|---|---|
| darkness for 2 s | 141 / 19 / 18, and unchanged for 4 s |
| LED 8, then LED 9 at once | 118 / 0 / 0, and unchanged for 5 s |
| LED 6, then LED 9 at once | 118 / 0 / 0 |
| LED 10, then LED 9 at once | 141 / 19 / 18 |

The two states differ by 23 code values in every colour (20 units of luma on the
camera's own scale), cut off at 0. LEDs 5, 9, 10 and 11 show both states; LEDs 6-8 and
12-17 gave one value whatever came before. Switching backlight compensation off changed
nothing. The LEDs themselves are steady: LED 9 gave 140.8 in 35 frames in a row.

How long the dark pause between LED 8 and LED 9 has to be:

| Dark for about | LED 9 came out as 141 / 19 / 18 |
|---|---|
| 30 ms (the old pause of 20 ms) | 0 of 5 |
| 75-80 ms | 4 of 6 |
| 175-195 ms | 6 of 6 |
| 325-330 ms | 6 of 6 |

The whole sequence LED 5 to 17, four times over: with about 30 ms of darkness between
LEDs, LED 9 was always in the lower state and LEDs 10 and 11 changed state from run to
run; with 200 ms of darkness every band gave the same values in all four runs.

## Conclusions

1. **Before every channel the camera must see one whole frame of darkness.** Its
   processing carries a state over from one picture to the next, and that state moves
   the values of some bands by 23 code values (15-20 % of the signal). After a dark frame
   the state is always the same, and it is the state the dark frame itself was taken in,
   so subtracting the dark frame is right. The pause between channels was 20 ms; it is
   now one frame period plus the exposure plus 50 ms for the sensor's read-out, rounded
   up to 10 ms: **170 ms at 10 fps**. `inter_channel_ms` can ask for more, never for less.
   This corrects conclusion 3 of the [earlier record](2026-10-02-acceptance-leds-5-17.md),
   which kept 20 ms because it did not change reply loss.
2. **The strongest camera channel stays the default value of a band** (`reduction`
   `dominant`). Its noise relative to the signal is about the same as luma's, but for
   the visible LEDs its signal is 3-4 times larger, so one code value is 0.7 % of it
   instead of 2-3 %.
3. **LED 7 and LED 17 are weak at this distance**: full power gives 57 and 62 code
   values, and LED 7's pixels then carry 11 % noise. Their bands work, with more noise.
4. **The model's PWM table is about four times too low for paper at 30 cm.** The values
   above are stored in this unit's profile by the PWM search; the table stays a first
   guess.
   *Found later the same day:* the table is not too low. That morning's lock had seen
   light and left the camera at its low gain; locked in darkness, as every lock is now,
   the table fits ([2026-10-02-hidden-camera-gain.md](2026-10-02-hidden-camera-gain.md)).
5. **The camera's values are not linear in the light**: doubling the PWM of LED 9 gave
   98, 153, 205 above black. Relative values are an index, as the documents say.
6. LEDs 13-17: the table says red is their strongest channel, this unit says blue by a
   few per cent. A calibration records its own choice, so calibrated values are not
   affected. The table is not changed on the evidence of one unit and a sheet of paper.

## After the change

A white reference and three scans of the same paper, recorded through the viewer with
the dark pause of 170 ms (captures record `dark_pause_ms`). Mean of the whole picture
relative to the white reference:

| Band | Scan 1 | Scan 2 | Scan 3 |
|---|---|---|---|
| 455 nm | 1.0000 | 0.9999 | 1.0000 |
| 520 nm | 1.0000 | 0.9999 | 0.9999 |
| 560 nm | 0.9996 | 0.9989 | 0.9985 |
| 610 nm | 0.9994 | 0.9987 | 0.9984 |
| 630 nm | 1.0001 | 1.0003 | 1.0001 |
| 667 nm | 1.0004 | 1.0004 | 1.0005 |
| 700 nm | 1.0005 | 1.0010 | 1.0013 |
| 804 nm | 1.0002 | 1.0002 | 1.0002 |
| 850 nm | 1.0003 | 1.0003 | 1.0005 |
| 890 nm | 1.0004 | 1.0006 | 1.0005 |
| 933 nm | 1.0010 | 1.0012 | 1.0016 |
| 973 nm | 1.0012 | 1.0018 | 1.0021 |
| 1050 nm | 1.0053 | 1.0077 | 1.0092 |

A scan of 13 channels now takes 14.7-15.8 s (12.3 s before). LED 17 at full power
drifts upwards by about 0.3 % per scan.

## What the paper is not

Office paper is bright and even enough to try the whole chain, and relative values
against it are repeatable. It is not a reference: its reflectance is not known and not
flat over 455-1050 nm, a single sheet lets the background through, and its optical
brighteners fluoresce under ultraviolet, which rules it out for LEDs 1-4. Values
"relative to white" from this paper are relative to this paper.

## Still open

Why the camera has two states, and whether Linux and Windows show the same. The white
reference on a real reflectance standard. The UV LEDs. The order of LEDs 1-2 and 13-17.
