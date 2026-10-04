# Safety

## UV radiation

| LED | Wavelength | Hazard |
|---|---|---|
| 1 | 255 nm | **UV-C** - injures eyes and skin within seconds of direct exposure |
| 2 | 275 nm | **UV-C** - same |
| 3 | 370 nm | UV-A |
| 4 | 400 nm | violet / UV-A boundary |

UV-C is invisible. An LED that looks dark may be on.

- Never look into the light source, and never point it at people or animals.
- Operate inside a closed dark box, or behind shielding that blocks UV.
- Cover exposed skin and wear UV-blocking eye protection when the box is open.
- The data sheet gives the UV LEDs a second power port (USB2, 5 V 2 A). The software
  cannot detect whether it is connected. **An unplugged USB2 is not a safeguard: all four
  UV LEDs light without it** (measured), because the light source also draws power
  through the other cables.

## What the software does

- LEDs 1-4 are never lit unless you explicitly allow it (`--allow-uv` on the command
  line, `allow_uv=True` in Python). The default channel set is LEDs 5-17.
- Every ON is paired with an OFF. If the device does not confirm an ON, an OFF is sent
  before the error is reported.
- A scan lights one LED at a time, and lighting an LED first switches off every LED that
  is not known to be off. The one exception is deliberate: `vispek-hc led on --all`
  (`exclusive=False` in Python) lights several LEDs together. That can draw more current
  than the USB1 port supplies; a brown-out shows as the serial port dropping out. UV LEDs
  are still excluded unless you allow them.
- All LEDs are switched off when a session ends or the port is closed, on Ctrl-C and on
  termination signals, and at interpreter exit. After such an emergency shutdown nothing
  can be lit until a normal all-off has been confirmed.
- UV LEDs lit by hand are switched off after a time limit (10 s), by a watchdog that is
  armed before the ON is sent.
- When an OFF command is not confirmed by the device, the software reports
  "shutdown unconfirmed". It does not claim the LED is off.

## What the software cannot do

- If the process is killed (`kill -9`, power loss, a crash of the host), nobody sends the
  OFF command and **an LED stays lit** (measured). Unplug every cable of the device
  (USB1, USB2 and the camera cable) to be sure.
- If the USB1 cable is pulled or its connection drops, **the LED stays lit** and there is
  no way left to switch it off until USB1 is back (measured: unchanged after 15 s, with
  USB2 connected and with USB2 unplugged). The light source goes on drawing power
  through the camera cable. The 10 s limit of a UV LED cannot reach the device then
  either. Unplug every cable.
- Termination signals are handled only if a light source was opened from the program's
  main thread; a program that opens it from a worker thread gets a warning in the log.
- Do not rely on the device to switch itself off. Whether the firmware has a timeout is
  not confirmed.
- It cannot see whether the dark box is closed.

## Reporting a safety problem

A defect that can light a UV LED without permission, or leave one lit, is a security
issue. Report it privately as described in [SECURITY.md](SECURITY.md).
