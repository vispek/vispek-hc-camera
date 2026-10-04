# Vispek HC1500: device guide for writing your own control software

This document is for a developer, or a coding agent, who has an HC1500 on the desk and
wants to control it from their own program in any language. It needs nothing else from
this repository: every byte of the protocol, every camera setting and every timing rule
that matters is here.

Each fact says where it comes from:

- **data sheet**: Vispek's technical data sheet and protocol sheet.
- **measured**: measured on one HC1500 with the software in this repository, on macOS
  (Apple silicon), October 2026. Records are in [acceptance/](acceptance/README.md).
- **to be confirmed**: not yet confirmed by the hardware team. Do not build on it without
  checking on your own unit.

If you only read one section, read [section 7](#7-five-things-the-device-does-that-your-software-must-handle).
A program that ignores it produces pictures, and the numbers in them are wrong.

## 1. What the device is

One housing with two independent USB devices inside:

1. **A light source**: 17 LEDs of different wavelengths, 255 nm to 1050 nm, switched and
   dimmed over a USB serial port.
2. **A colour camera**: an ordinary USB video class (UVC) camera with a sensor that
   responds from 300 to 1100 nm.

A "multispectral image" is made by your software: light LED 1, take a picture; light
LED 2, take a picture; and so on. The device does none of that by itself. There is no
spectrometer, no on-board storage, no trigger line between LEDs and camera, and no
firmware interface beyond the two serial commands in section 4.

**There is no Bluetooth, no Wi-Fi and no network interface.** Everything goes over USB
cables.

| | |
|---|---|
| Size, weight, housing | 60 x 60 x 20.5 mm, about 300 g, anodised aluminium (data sheet) |
| Power | 6 W working, 1 W standby (data sheet) |
| Operating range | -30 to +70 °C, up to 80 % relative humidity (data sheet) |
| Light source | 17 LED channels, PWM at 16 kHz with 1024 steps; centre wavelengths stable to 1-3 nm (data sheet) |
| Sensor | colour, 1928 x 1088, 2.9 µm pixels, 1/2.8", rolling shutter, responds 300-1100 nm (data sheet) |
| Lens | manual focus. 2.5 mm, 130° (data sheet); the unit measured showed about 53° horizontally (to be confirmed) |
| Serial number | none: neither the serial chip nor the camera reports one (measured) |

## 2. Cables

| Port | Carries | Shows up on the computer as |
|---|---|---|
| USB1 (Type-C) | **the serial port that switches the LEDs**, and 5 V 0.5 A for the light source | a CH341 USB serial adapter, USB vendor id `1A86`. macOS: `/dev/cu.usbserial-*`. Linux: `/dev/ttyUSB*`. Windows: `COMn` (install the CH341 driver) |
| USB2 (Type-C) | power only, 5 V 2 A, meant for the UV LEDs (data sheet) | nothing |
| USB3 | **the camera**, 5 V 0.5 A | a UVC camera, VID:PID `0c45:6370`, name `LRCP 1080P-60FPS` |

Measured, and different from what the port labels suggest:

- The light source also draws power through the camera cable. An LED that is lit **stays
  lit** when USB1 is pulled, with or without USB2.
- All four UV LEDs light **without** USB2. An unplugged USB2 is not a safety measure.
- Pulling the camera cable does switch the LEDs off.

With several units on one computer you must tell them apart by USB port location: there
is no serial number.

## 3. Safety: read before lighting anything

| LED | Wavelength | Hazard |
|---|---|---|
| 1 | 255 nm | **UV-C**: injures eyes and skin within seconds of direct exposure |
| 2 | 275 nm | **UV-C** |
| 3 | 370 nm | UV-A |
| 4 | 400 nm | violet / UV-A boundary |

UV-C is invisible: an LED that looks dark may be on. Operate inside a closed dark box.

Rules your software must follow:

1. **Never light LEDs 1-4 by default.** Require an explicit switch from the operator.
2. **Every ON must be followed by an OFF**, also when your program fails: use `finally`,
   signal handlers and an exit hook. The device has no "all off" command and **no
   timeout: an LED stays lit until it receives OFF** (measured: unchanged after 15 s
   with the host gone). If your process is killed, the LED stays lit.
3. On start-up, send OFF to LEDs 1-17 before anything else. You do not know what the
   last program left lit.
4. If an OFF gets no reply, do not report the LED as off. Send it again; if it stays
   unanswered, tell the operator to unplug **every** cable, the camera cable too.
5. Light one LED at a time. Several at once can exceed what USB1 supplies (to be
   confirmed how many).
6. Give a UV LED a time limit in your own code (this repository uses 10 s).

## 4. Light-source protocol

### Line settings

115200 baud, 8 data bits, no parity, 1 stop bit, no flow control (measured; the protocol
sheet does not state the rate). Leave at least 10 ms between two commands.

### Command (computer to device)

| Field | Bytes | Value |
|---|---|---|
| Header | 1 | `0x3D` |
| Function | 1 | `0x01` ON, `0x02` OFF |
| Data length | 1 | 3 for ON, 1 for OFF |
| Data | n | ON: LED number, PWM high byte, PWM low byte (PWM 0-1023, big-endian). OFF: LED number |
| Checksum | 1 | CRC-8 over header, function, length and data |
| Trailer | 2 | `0x0D 0x0A` |

LED numbers are 1-17. The protocol sheet allows up to 22; what 18-22 do is unknown, do
not send them.

CRC-8: polynomial `0x07`, initial value `0x00`, no reflection, no final XOR.

### Reply (device to computer)

Seven bytes: `3D | function | 01 | status | CRC-8 | 0D 0A`. Status `0x00` is success,
anything else is a refusal. **The reply does not say which LED it is about.** A reply
arrives 7-20 ms after the command (measured).

| Reply | Bytes |
|---|---|
| ON accepted | `3d 01 01 00 29 0d 0a` |
| OFF accepted | `3d 02 01 00 94 0d 0a` |

### Test vectors

| Action | Command |
|---|---|
| LED 1 on, PWM 500 | `3d 01 03 01 01 f4 83 0d 0a` |
| LED 5 on, PWM 1023 | `3d 01 03 05 03 ff 33 0d 0a` |
| LED 9 on, PWM 1023 | `3d 01 03 09 03 ff c9 0d 0a` |
| LED 17 on, PWM 1023 | `3d 01 03 11 03 ff 3a 0d 0a` |
| LED 1 off | `3d 02 01 01 93 0d 0a` |
| LED 5 off | `3d 02 01 05 8f 0d 0a` |
| LED 17 off | `3d 02 01 11 e3 0d 0a` |

[protocol.md](protocol.md) lists ON and OFF for all 17 LEDs.

### Reference code (Python, `pyserial` only)

```python
import time
import serial  # pip install pyserial


def crc8(data: bytes) -> int:
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc


def frame(function: int, data: bytes) -> bytes:
    body = bytes([0x3D, function, len(data)]) + data
    return body + bytes([crc8(body)]) + b"\r\n"


def led_on(led: int, pwm: int) -> bytes:
    assert 1 <= led <= 17 and 0 <= pwm <= 1023
    return frame(0x01, bytes([led, pwm >> 8, pwm & 0xFF]))


def led_off(led: int) -> bytes:
    assert 1 <= led <= 17
    return frame(0x02, bytes([led]))


def send(port: serial.Serial, command: bytes, attempts: int = 3) -> bool:
    """True if the device accepted the command. One command at a time."""
    wanted = frame(command[1], b"\x00")  # the success reply for this function
    for _ in range(attempts):
        time.sleep(0.010)  # gap between commands
        port.reset_input_buffer()  # a late reply belongs to an older command
        port.write(command)
        port.flush()
        deadline, received = time.monotonic() + 0.2, b""
        while time.monotonic() < deadline and wanted not in received:
            received += port.read(max(1, port.in_waiting))
        if wanted in received:
            return True
        time.sleep(0.060)  # let a late reply arrive, then drop it
    return False


def all_off(port: serial.Serial) -> list[int]:
    """Switch LEDs 1-17 off. Returns the LEDs whose OFF was not confirmed."""
    return [led for led in range(1, 18) if not send(port, led_off(led))]


# The port name is yours: see section 2. LED 9 is visible red; keep the lid closed anyway.
port = serial.Serial("/dev/cu.usbserial-1210", 115200, timeout=0.005, exclusive=True)
try:
    assert all_off(port) == []
    assert send(port, led_on(9, 36))  # LED 9 (630 nm), visible red
    time.sleep(1.0)
finally:
    unconfirmed = all_off(port)
    port.close()
    if unconfirmed:
        print("NOT CONFIRMED OFF:", unconfirmed, "- unplug every cable of the device")
```

### What the protocol leaves open, and how to deal with it

1. **About one command in twenty gets no reply at all** (measured: independent of the
   gap, the timeout and whether the camera runs). The resend is answered. Use a timeout
   of 200 ms and three attempts. ON and OFF set an absolute state, so a resend is
   harmless.
2. **A late reply would be credited to the next command**, because replies carry no LED
   number. Clear the input buffer before every command and never send two commands
   without waiting for the first reply.
3. **"LED n off" has the same bytes as an OFF reply with status n.** On a link that
   echoes what you send, a command would read as its own refusal. If you receive seven
   bytes identical to what you just sent, it is an echo: drop it and keep waiting.
4. **Search for the header.** Look for `0x3D` in the byte stream, take seven bytes,
   verify the CRC, and continue from the next byte if it fails.
5. After an ON that got no reply, send OFF for that LED before reporting the error: the
   LED may be lit.

## 5. The LEDs

| LED | Wavelength | Strongest camera channel | Starting PWM | Notes |
|---|---|---|---|---|
| 1 | 255 nm | B | 458 | **UV-C** |
| 2 | 275 nm | B | 300 | **UV-C** |
| 3 | 370 nm | B | 74 | UV-A |
| 4 | 400 nm | B | 74 | violet |
| 5 | 455 nm | B | 24 | |
| 6 | 520 nm | G | 224 | |
| 7 | 560 nm | G | 600 | weak: needs full power on most targets |
| 8 | 610 nm | R | 74 | |
| 9 | 630 nm | R | 36 | |
| 10 | 667 nm | R | 18 | |
| 11 | 700 nm | R | 36 | |
| 12 | 804 nm | R | 74 | |
| 13 | 850 nm | R | 74 | |
| 14 | 890 nm | R | 75 | |
| 15 | 933 nm | R | 75 | |
| 16 | 973 nm | R | 150 | |
| 17 | 1050 nm | R | 300 | weak |

- LED *n* is the *n*-th entry of the data sheet's ascending wavelength list. The order of
  LEDs 1-2 and of 13-17 cannot be told apart with the camera and is **to be confirmed**.
- Wavelengths are nominal LED centre wavelengths, not what the sensor responds to. Key
  your data by LED number, not by wavelength.
- "Strongest camera channel" is where that LED shows in the camera's RGB picture. Above
  800 nm all three channels respond almost equally (on the measured unit blue was 2-5 %
  above red for LEDs 13-17). Decide it from your own white reference.
- The starting PWM values are within a factor of two of what a white target 30 cm away
  needed **when the camera was locked in darkness** (section 7.1). The PWM a channel needs differs by
  a factor of about 50 between channels: there is no single PWM for all. Find your own
  values (section 8.4).
- The sensor responds from 300 nm. What LEDs 1 and 2 show is very likely **fluorescence**
  of the sample, not reflected UV: bare metal reads zero, office paper reads bright blue
  under all four UV LEDs (measured).
- LED output drifts with temperature: the same sample read 1-2 % lower an hour after its
  white reference.
- Illumination is uneven: on a flat white target the corners reach 0.53-0.87 of the
  centre, depending on the LED (measured). A white reference removes it (section 8.3).

## 6. The camera

A standard UVC 1.1 camera. Any UVC host works: AVFoundation (macOS), V4L2 (Linux),
DirectShow or Media Foundation (Windows), or a library on top of them (ffmpeg, OpenCV,
GStreamer). Only macOS has been measured; Linux and Windows are **to be confirmed**.

### Formats

| | |
|---|---|
| Pixel formats | YUY2 (uncompressed, 4:2:2) and MJPEG (4:2:2) |
| Sizes | 640x480, 1280x720, 1920x1080 |
| Recommended | **1280x720, uncompressed YUY2, 10 fps** |
| Why not MJPEG | compression changes the values, and they depend on the JPEG decoder |
| Why 10 fps | uncompressed 720p fills most of USB 2.0. 1080p "60 fps" delivered about 5.6 fps on macOS |

With ffmpeg, raw frames on standard output (each frame is width x height x 2 bytes):

```bash
# macOS
ffmpeg -f avfoundation -framerate 10 -video_size 1280x720 -pixel_format yuyv422 -i "LRCP 1080P-60FPS:none" -pix_fmt yuyv422 -f rawvideo -

# Linux (to be confirmed)
ffmpeg -f v4l2 -input_format yuyv422 -framerate 10 -video_size 1280x720 -i /dev/video0 -pix_fmt yuyv422 -f rawvideo -

# Windows (to be confirmed)
ffmpeg -f dshow -rtbufsize 128M -pixel_format yuyv422 -framerate 10 -video_size 1280x720 -i "video=LRCP 1080P-60FPS" -pix_fmt yuyv422 -f rawvideo -
```

On macOS the operating system asks for camera permission once per application; without
it no frames arrive and no error is shown.

### From YUY2 to RGB

The uncompressed stream is **limited range** (measured on macOS: a dark frame is Y = 20
in every pixel). Convert with BT.601, limited range: stretch Y 16-235 and Cb/Cr 16-240 to
0-255 first. After conversion a dark frame is **5, 5, 5 in every pixel**: the black level
of the camera's own processing, with no noise in it. Judge "dark" against 5, not 0. If
your dark frame reads 20, you are treating limited range as full range.

Only luma has full horizontal resolution (4:2:2).

### What the values are

8-bit code values after the camera's own gamma and colour matrix. They are **not linear
in light**: doubling the PWM of LED 9 gave 98, 153, 205 above black (measured). Under
red light the green channel falls below the black level. Treat reflectance computed
from them as a relative index, not as a radiometric quantity.

### Controls

Standard UVC controls. Names below are as `uvc-util` (macOS) spells them; `v4l2-ctl`
(Linux) names in brackets.

| Control | Range | Lock it to |
|---|---|---|
| `auto-exposure-mode` (`auto_exposure`) | 1 manual, 8 automatic (UVC); V4L2 numbers them 1 manual, 3 automatic | 1 |
| `exposure-time-abs` (`exposure_time_absolute`) | 1-5000, units of 100 µs | 157 (15.7 ms) |
| `auto-exposure-priority` | 0 / 1 | 0 |
| `gain` | 0-100 | 0 |
| `auto-white-balance-temp` (`white_balance_automatic`) | 0 / 1 | 0 |
| `white-balance-temp` (`white_balance_temperature`) | 2800-6500 K | 4600 |
| `brightness` | -64 to 64 | 0 |
| `contrast` | 0-64 | 32 |
| `saturation` | 0-128 | 66 |
| `sharpness` | 0-6 | 0 (the default 3 invents signal at edges) |
| `gamma` | 72-500 | 100 |
| `backlight-compensation` | 0-2 | 1 |
| `power-line-frequency` | 0-2 | 1 |
| `auto-focus` | 0 / 1 | 0 |
| `focus-abs` | 1-1023 | do not write: the lens is manual and the read-back is not a position |

Switch an automatic mode off **before** writing the value it would override (automatic
exposure before exposure time, automatic white balance before the temperature). Write on
the **running** stream, read everything back, wait a few frames, and read back again:
a value that does not hold is a failure.

How to write them: macOS has no system tool; this repository ships one
(`tools/uvc-util`), or use libuvc. Linux: `v4l2-ctl -d /dev/video0 --set-ctrl name=value`.
Windows: DirectShow `IAMCameraControl` / `IAMVideoProcAmp` (exposure there is in powers
of two seconds, not in 100 µs).

Keep exposure at 6.3 ms or longer: the LEDs are dimmed by PWM at 16 kHz, and against the
rolling shutter a shorter exposure shows as row-to-row ripple above 1 %.

## 7. Five things the device does that your software must handle

All measured. Each one silently spoils the numbers if ignored.

### 7.1 The camera keeps a hidden gain from the moment you switch automatic exposure off

Automatic exposure adjusts an internal gain. When you switch to manual exposure, that
gain **stays where it was** and no control reads it back: the `gain` control reads 0
throughout and works on top of it. Between a lock in darkness and a lock under bright
light lies a factor of **4.4** in every pixel value, with every setting identical.

A lock in darkness is repeatable (twelve locks within 0.2 code values). A lock under
light depends on the picture and is not.

**Procedure for every lock:**

1. Switch every LED off. The device must be in a closed dark box.
2. Set automatic exposure (`auto-exposure-mode` 8).
3. Wait one second (half a second was enough).
4. Take a frame and check it is dark: every channel mean at most 12. If it is not,
   **stop**: light is leaking in and the gain is unknown.
5. Set manual exposure and write the rest of the profile (section 6).
6. Lock again in this way whenever the video stream was reopened, the camera was
   replugged, or another program may have touched the camera.

A white reference and a sample are only comparable if both were recorded after locks
taken this way. Record in your files that the lock was taken in darkness.

### 7.2 The camera remembers the previous picture

The camera's processing carries state from one frame to the next. LED 9 read 118 when
lit straight after LED 8, and 141 after darkness: 20 % apart. After one whole frame of
darkness the state is always the same.

**Before every LED, keep everything off for one frame period + exposure time + 50 ms**
(170 ms at 10 fps and 15.7 ms exposure). Use the same pause for the white reference and
for the samples.

### 7.3 Light reaches the picture late, and the first lit frame is half lit

There is no synchronisation between LED and camera. Measured through ffmpeg on macOS at
10 fps: the first frame that shows the LED arrives about 350 ms after the device's reply
and is lit only in its lower part (rolling shutter); the first fully lit frame arrives
about 450 ms after the reply. Switching off looks the same. The delay depends on the
computer and the capture path: **measure it on yours** by lighting an LED and watching
the frame mean.

**After ON, discard frames until at least (your measured delay + one frame period +
exposure time) has passed, then take a frame only if it agrees with the one before it**
(this repository asks for a mean absolute difference of at most 8 code values and no
row that differs by more than 32).

### 7.4 Opening the stream resets the camera

Opening or reopening the video stream, and replugging the camera, can put every
parameter back to automatic. Lock on the running stream, and lock again (section 7.1)
after any interruption of it.

### 7.5 An LED never switches itself off

See section 3. The firmware has no watchdog.

## 8. Recording a multispectral image

### 8.1 One scan

```text
open the serial port; send OFF to LEDs 1-17
open the camera stream (1280x720, YUY2, 10 fps); wait for the first frames
lock the camera in darkness (section 7.1)

dark = one frame with every LED off          # must be 5, 5, 5 everywhere
for each LED in your channel list:
    wait the dark pause (section 7.2)
    send ON(led, pwm[led]); on no reply after three attempts: send OFF, stop
    wait until light has reached a whole frame (section 7.3)
    take frames until two in a row agree; keep the last one as band[led]
    send OFF(led); on no reply: stop and report the LED as possibly lit
finally: send OFF to LEDs 1-17
```

About 1.1 s per LED at 10 fps: 15 s for LEDs 5-17, 19 s for all 17 (measured). Sample
and camera must not move during the scan.

### 8.2 One number per pixel and band

Each band is an RGB picture taken under one LED. Reduce it to one value per pixel by
taking the camera channel that is strongest for that LED on the white reference
(section 5), not the luma. Subtract the dark frame's value in the same channel.

Mark as invalid every pixel that is 255 in any of R, G, B: it is clipped.

### 8.3 White reference and relative reflectance

Illumination is uneven and differs per LED, and the camera's response differs per band.
Both are removed by dividing by a scan of a white target, recorded with **the same PWM
values, exposure, gain, picture size, dark pause and a lock taken in darkness**:

```text
relative[y, x, band] = (sample[y, x, band] - sample_dark[y, x, channel])
                     / (white[y, x, band]  - white_dark[y, x, channel])
```

- Where the denominator is below 8 code values, or below 5 % of its median, the pixel has
  no value.
- The white target must fill the field of view at the working distance, and must be
  recorded again whenever the distance, the lens, the PWM values or the camera lock
  change, and at least daily (drift).
- **Office paper is not a white reference for LEDs 1-4**: its optical brightener
  fluoresces. It is usable for LEDs 5-17. A sintered PTFE plate is the proper target.
- A reference file from another unit, or shipped from the factory, does not replace
  your own: it corrects this camera at this distance.

### 8.4 Finding PWM values

For each LED, find the PWM at which the 99th percentile of the white target, in the
LED's strongest channel, lies at 150-235 and no pixel is clipped. Start from the table in
section 5 and halve or double. Store the values with the unit and use them unchanged for the reference and
all samples.

## 9. Checks for your own software

Run these on the device before trusting your data:

| Check | Expected |
|---|---|
| Encode the test vectors of section 4 | byte-identical |
| Dark frame, every LED off, lid closed, after the lock | 5, 5, 5 in every pixel |
| Light LED 6: the frame mean rises; switch off: it returns to 5 | pairing of serial port and camera |
| Lock ten times in darkness, read LED 9 at PWM 36 each time | within 1 code value |
| Scan a white target twice; divide one by the other | 1.00 ± 0.01 in every band |
| Kill your program with an LED lit; start it again | the LED is switched off on start-up |
| Run 200 commands | about 5 % need a resend; none fails three times |

## 10. Not known yet

- The order of LEDs 1-2 and 13-17 (to be confirmed by the hardware team).
- What LED numbers 18-22 do, and what the firmware replies to a number that does not
  exist.
- Why replies get lost, and whether the device acted on a command it did not answer.
- Whether the camera has a vendor control for the gain of section 7.1.
- How many LEDs may be lit together.
- The lens and field of view of a standard unit.
- Behaviour on Linux and Windows: pixel range of the uncompressed stream, control names,
  frame rates.

## 11. A working implementation

This repository is one implementation of everything above, under the Apache-2.0 licence:
a Python library and command line (`vispek-hc`). Read it for details this guide leaves
out:

| File | What it shows |
|---|---|
| `src/vispek_hc/protocol.py` | frames, CRC, a reply scanner that resynchronises |
| `src/vispek_hc/illuminator.py` | resends, purging, emergency all-off, UV time limit |
| `src/vispek_hc/session.py` | the lock in darkness, measuring the picture delay |
| `src/vispek_hc/sweep.py` | the scan: dark pause, waiting for a settled frame, PWM search |
| `src/vispek_hc/calibration.py`, `reflectance.py` | white reference and relative reflectance |
| [data-format.md](data-format.md) | a file format for captures that records every setting |
| [SAFETY.md](../SAFETY.md) | the safety rules in full |
