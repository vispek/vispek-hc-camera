# Pulling the cables

**Date:** 2026-10-02. **Software:** 0.1.0.dev0 (commit `24729d7`). **Computer:** macOS on
Apple silicon; camera and light source reach it through one USB hub. **Device:** one
HC1500 (`hc1500-1`) in a dark box, **USB2 connected** (it had been needed for the UV
test before). Only LEDs 5-17 were lit. A person pulled the cables when a script asked.

## What was done and seen

| | What happened |
|---|---|
| **Camera and light source lost together** during a scan (the first try pulled both), LED 8 lit at that moment | The OFF for LED 8 could not be sent. The scan ended at once with `LED_SHUTDOWN_UNCONFIRMED`; on leaving, all 17 LEDs were reported unconfirmed. The capture is `partial`; its 14 finished scans load normally. After plugging in again, `led off --all` confirmed every LED off |
| **Camera cable (USB3) pulled** during a scan, LED 13 lit | No frame for 3 s; the channel was given up, **the OFF for LED 13 was sent and confirmed**, the group is `failed` with `FRAME_INCOMPLETE`, and the recording ended with `RECORDING_PARTIAL` (4 usable scans). Afterwards the computer saw the serial port and no camera |
| **Light-source cable (USB1) pulled** with LED 6 lit by hand, USB2 still connected | The serial port vanished. **The LED stayed lit**: the picture did not change for the 15 s it was watched, nor after USB1 was plugged in again. An OFF sent meanwhile reported `LED_SHUTDOWN_UNCONFIRMED`. With USB1 back, `all_off` switched the LED off (picture back at 5) |
| **The same with USB2 unplugged first**: only the camera cable left | **The LED stayed lit just the same**, at unchanged brightness (mean green 171.7 before and after), for the 15 s watched and after USB1 was back; `all_off` then switched it off |

## Conclusions

1. **Pulling USB1 does not switch an LED off**, whether USB2 is connected or not, and
   takes away the only way to switch it off. The device does not switch itself off when
   the serial connection is lost, at least not within 15 s, and the light source goes on
   drawing power through the camera cable. [SAFETY.md](../../SAFETY.md) said to unplug
   both cables of the light source; **that was not enough, and it now says every cable,
   the camera cable too.** The software's hint for an unconfirmed shutdown says the same.
   The time limit of a UV LED lit by hand cannot help either: its OFF has no way to the
   device.
2. Losing the camera during a scan is handled as intended: the LED goes off, confirmed,
   and what was recorded is kept and marked.
3. Losing the light source is reported as what it is: shutdown unconfirmed, never "off".

## Still open

Whether an LED goes out when every cable is pulled was not watched: without the camera
cable there is no picture to see it in.
