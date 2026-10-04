# Can an agent work from AGENTS.md alone?

**Date:** 2026-10-01. **Software:** 0.1.0.dev0, before the M3 commit. **Hardware:** none.

**Question.** Milestone M3 asks that an agent which has not seen the specification, and
reads only `AGENTS.md`, can write a script that lights one LED and takes one frame.

**Method.** A fresh agent (Claude, general-purpose, no earlier context) got the prompt in
[prompt.md](prompt.md). It was allowed to read `AGENTS.md` and nothing else, and not to
run anything. It reported reading that one file and running no command. Its script is
[led_and_frame.py](led_and_frame.py), unchanged.

**Result: pass.** Run by hand afterwards:

```
$ python led_and_frame.py --simulate -o out/red.png
LED 9 (630 nm) at PWM 36/1023, frame 64x36 px, saved to out/red.png        (exit 0)
$ python led_and_frame.py -o out/real.png          # no device attached
error DEVICE_NOT_FOUND: no light-source serial port found
hint: plug the light source's USB1 port in, or name the port with --port   (exit 1)
```

The saved picture is 64 x 36 with mean R, G, B of 180, 18, 20: the simulated white board
under LED 9 (175, 13, 15 above the black level of 5). The LED is switched off in a
`finally` block and again by leaving the session. The run against a real device is still
open, like every hardware check.

## What the agent had to guess

It marked twelve guesses. All were right; these were the ones the document should not
have left open, and what changed in `AGENTS.md` because of them:

| Guess or gap | Change |
|---|---|
| Which LEDs are visible? Only LED 9 was named | "Using it from Python" now says LEDs 5-11 are visible and 12-17 near infrared, and points to the table |
| Does `open_session()` without arguments open the real device? | Stated, with the error it raises when there is none |
| What do `wait_new` and `off` do when they fail? | Stated: `FRAME_INCOMPLETE` on timeout, `LED_SHUTDOWN_UNCONFIRMED` when an OFF is not confirmed |
| How to turn an error into an exit code | `error.exit_code` is mentioned; `hint` may be `None` |
| What the Python path needs on the computer | A line on ffmpeg and on unlocked camera parameters for single frames |

Not changed, because the linked documents answer them and `AGENTS.md` is meant to stay
short: the full signatures (`docs/api.md`), a sensible PWM per LED (`docs/hardware.md`),
and whether `save_png` creates folders (it does not; the agent created the folder
itself).
