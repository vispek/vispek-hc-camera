# Acceptance run on a real HC1500, without the white board

**Date:** 2026-10-02. **Software:** 0.1.0.dev0 (commit `c57b615` plus the change to the
`visible_led` check that this record led to). **Computer:** macOS on Apple silicon,
ffmpeg 9.0, uvc-util built from `tools/uvc-util`. **Device:** one HC1500 (`hc1500-1`) in a
dark box, a small circuit board on a pale background about 30 cm in front of the lens.
Only LEDs 5-17 were lit, one at a time. No UV LED was lit.

Run as the [runbook](hardware-runbook.md) says, without `VISPEK_HC_WHITE_BOARD`. `check`
before the run: `ready: true`, black level 5, 5, 5, pairing rise 34.2 code values, latency
363.8 ms.

## Runs

| Run | Result | Records |
|---|---|---|
| 1 | 11 passed, 1 failed, 4 skipped. `test_visible_led` failed: LED 6 at PWM 50 raised the picture by 3.5 code values against a threshold of 5 | not kept |
| 2, after the check was changed | **12 passed**, 4 skipped (the white-board checks) | [2026-10-02-hc1500-1-records.json](2026-10-02-hc1500-1-records.json) |
| 3, the four white-board checks on the same scene, **no white board in view** | both pixel-path checks passed; the white reference was refused; scalar noise found too little signal | [2026-10-02-hc1500-1-pixel-paths.json](2026-10-02-hc1500-1-pixel-paths.json) |
| 4, serial port only, LED 6 at PWM 1, OFF and ON in turn | see "Reply loss" | in this file |

Run 3 says nothing about the white board. It was made to see the PWM search, the
reference gate and both pixel paths work on the device at all.

## What was measured

| | Result |
|---|---|
| Pairing | paired; rise 33.6-38.6 code values; latency 369.3, 370.4 and 380.7 ms; 9.89-10.14 fps |
| Black level | 5, 5, 5 in every run, on the uncompressed and on the JPEG path |
| LED 6 at PWM 224 | picture mean 5.0 dark, 68.0 lit, 5.0 after |
| Camera lock | all 14 parameters held as requested; none skipped |
| Exposure | asked 63, 100, 157, 200, 333: got exactly these |
| Reply delay | median 8.3-8.7 ms, largest 25.7 ms |
| Scan of LEDs 5-17 | complete in both runs: every channel selected at the first attempt after two frames, 612-658 ms after the ON reply |
| Scan time | 12.5 s and 12.2 s for 13 channels, of which about 1.3 s is locking the camera. About 0.70 s per channel. `record --dry-run` said 10.7 s for the recording; 10.9 s was measured |
| Same scene twice, 5 minutes apart | response per channel differs by at most 0.6 code values (second scan lower in 12 of 13 channels) |
| Pixel paths | uncompressed and JPEG: black 5, 5, 5 and a peak of 255 with LED 9 at PWM 72 |

## Reply loss

| Camera | Pause between commands | Reply timeout | Commands | Needed a resend |
|---|---|---|---|---|
| streaming | 10 ms | 200 ms | 120 | 4 |
| streaming | 20 ms | 200 ms | 120 | 3 |
| streaming | 50 ms | 200 ms | 120 | 8 |
| streaming | 100 ms | 200 ms | 120 | 8 |
| not opened | 10 ms | 300 ms | 120 | 4 |
| not opened | 10 ms | 600 ms | 120 | 6 |
| not opened | 10 ms | 200 ms | 400 | 21 |
| not opened | 10 ms | 100 ms | 600 | 32 |
| not opened | 200 ms | 200 ms | 200 | 3 |

(The first six rows add runs 1 and 2.) In the rows of 400 and 600 commands every command
was looked at one by one: a failed attempt received **no byte at all**, nothing arrived
late afterwards, and the first resend was answered every time. OFF and ON were hit
equally (26 and 27). In about 1,900 commands none went unanswered after three attempts.

## Conclusions

1. **A scan works on the device.** Dark frame 5, 5, 5; thirteen channels, each settled at
   the first attempt; the camera parameters were the same before and after.
2. **About one command in twenty gets no reply, and it is not the camera's doing.** The
   share is the same with the camera streaming and with the camera never opened, and with
   a timeout of 100, 200, 300 or 600 ms: the reply is missing, not late. Whether the
   device acted on such a command cannot be seen from the serial port. This corrects
   conclusion 6 of the [first record](2026-10-02-first-device.md). At one command every
   200 ms the share was 3 of 200; too few to call it lower.
3. **Reply timeout (200 ms) and the pause between channels (20 ms) stay as they are.**
   (The pause was lengthened later the same day, for another reason: see
   [the white-board record](2026-10-02-white-board-and-camera-state.md).)
   Neither changes the share of lost replies. A lost reply costs about 270 ms; two were
   lost in the scan of run 2. A shorter timeout would save about 0.1 s per lost reply and
   is not worth a default that has only been seen on one computer.
4. **The limited-range conversion holds at both ends.** Uncompressed frames give black 5
   and reach 255; so do the JPEG frames. No change.
5. **The camera takes every exposure asked for** (63 to 333, in units of 100 µs) exactly.
6. **A scan of all 17 channels will take about 15 s** on this computer (13.7 s of
   recording by the measured 0.70 s per channel, plus locking): extrapolated, not
   measured. The specification expected 10-11 s; the difference is the pipeline latency
   of 370 ms against the 180 ms it assumed.
7. **The `visible_led` check was too faint**, the same mistake as in the pairing probe: it
   now lights the LED at the PWM the unit scans it with. A check changed, not a default.
8. **The PWM search needs a scene without glints.** On the circuit board a few pixels
   clip at any useful power, the search allows none, and every channel ended at a PWM
   that gives almost no light (LED 6: 50, LED 9: 7, LED 12: 3). The white reference that
   followed was then refused (`LED 8 gave no light on the white reference`), which is
   right: this scene is not a white board. Two things to settle with the real board:
   whether it needs `--white-roi`, and that the search stores its values in the unit
   profile even when the reference that follows is refused.

No default changed.

## Still open

- With the white board in view: the white reference (board divided by itself), the PWM
  per channel, the camera channel per LED against the model's table, noise of the
  dominant channel against luma.
- By hand, as the runbook lists: the order of LEDs 1-2 and 13-17, the UV LEDs, unplugging,
  killing the process.
- Linux and Windows.
