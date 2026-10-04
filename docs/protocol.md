# Light-source serial protocol

The LEDs are switched over a USB serial port (CH341, USB vendor id `1A86`). The protocol
has two commands, ON and OFF. `vispek_hc.protocol` builds and parses the frames and does
no I/O; `vispek_hc.illuminator` talks to the port.

## Line settings

115200 baud, 8 data bits, no parity, 1 stop bit, no flow control. The protocol sheet does
not state the rate; 115200 is what was observed and awaits confirmation. If no rate is
given, the driver probes 115200, 9600, 57600, 38400, 19200 in that order, **with OFF
commands only**: probing never lights an LED.

The port is opened exclusively. At least 10 ms pass between two commands.

## Command (computer to device)

| Field | Bytes | Value |
|---|---|---|
| Header | 1 | `0x3D` |
| Function | 1 | `0x01` ON, `0x02` OFF |
| Data length | 1 | 3 for ON, 1 for OFF |
| Data | n | ON: LED number, PWM high byte, PWM low byte (0-1023, big-endian). OFF: LED number |
| Checksum | 1 | CRC-8 over header, function, length and data |
| Trailer | 2 | `0x0D 0x0A` |

The protocol allows LED numbers 1-22. The HC1500 has 17 and the software uses 1-17.

CRC-8: polynomial `0x07`, initial value `0x00`, no reflection, no final XOR. Reference
routine from the protocol sheet:

```c
uint8_t protocol_crc8(uint8_t *data, uint16_t length)
{
 uint8_t crc;
 uint8_t i;

 crc = 0;
 while(length--)
 {
  crc ^= *data++;
  for(i=0; i<8; i++)
  {
   if(crc & 0x80)
    crc = (crc << 1) ^ 0x07;
   else
    crc <<= 1;
  }
 }

 return crc;
}
```

## Reply (device to computer)

Seven bytes: `3D | function | 01 | status | CRC-8 | 0D 0A`. Status `0x00` is success,
`0x01` failure; any other value is treated as a refusal with an unknown code. **The reply
does not name the LED.**

## Examples from the protocol sheet

| Action | Command | Reply |
|---|---|---|
| LED 1 on, PWM 500 (`0x01F4`) | `3d 01 03 01 01 f4 83 0d 0a` | `3d 01 01 00 29 0d 0a` |
| LED 5 on, PWM 1023 (`0x03FF`) | `3d 01 03 05 03 ff 33 0d 0a` | `3d 01 01 00 29 0d 0a` |
| LED 1 off | `3d 02 01 01 93 0d 0a` | `3d 02 01 00 94 0d 0a` |
| LED 5 off | `3d 02 01 05 8f 0d 0a` | `3d 02 01 00 94 0d 0a` |

```python
from vispek_hc.protocol import encode_off, encode_on

assert encode_on(5, 1023).hex(" ") == "3d 01 03 05 03 ff 33 0d 0a"
assert encode_off(5).hex(" ") == "3d 02 01 05 8f 0d 0a"
```

## The full routine: every LED on at PWM 1023, and off

| LED | ON | OFF |
|---|---|---|
| 1 | `3d 01 03 01 03 ff 98 0d 0a` | `3d 02 01 01 93 0d 0a` |
| 2 | `3d 01 03 02 03 ff 25 0d 0a` | `3d 02 01 02 9a 0d 0a` |
| 3 | `3d 01 03 03 03 ff 4e 0d 0a` | `3d 02 01 03 9d 0d 0a` |
| 4 | `3d 01 03 04 03 ff 58 0d 0a` | `3d 02 01 04 88 0d 0a` |
| 5 | `3d 01 03 05 03 ff 33 0d 0a` | `3d 02 01 05 8f 0d 0a` |
| 6 | `3d 01 03 06 03 ff 8e 0d 0a` | `3d 02 01 06 86 0d 0a` |
| 7 | `3d 01 03 07 03 ff e5 0d 0a` | `3d 02 01 07 81 0d 0a` |
| 8 | `3d 01 03 08 03 ff a2 0d 0a` | `3d 02 01 08 ac 0d 0a` |
| 9 | `3d 01 03 09 03 ff c9 0d 0a` | `3d 02 01 09 ab 0d 0a` |
| 10 | `3d 01 03 0a 03 ff 74 0d 0a` | `3d 02 01 0a a2 0d 0a` |
| 11 | `3d 01 03 0b 03 ff 1f 0d 0a` | `3d 02 01 0b a5 0d 0a` |
| 12 | `3d 01 03 0c 03 ff 09 0d 0a` | `3d 02 01 0c b0 0d 0a` |
| 13 | `3d 01 03 0d 03 ff 62 0d 0a` | `3d 02 01 0d b7 0d 0a` |
| 14 | `3d 01 03 0e 03 ff df 0d 0a` | `3d 02 01 0e be 0d 0a` |
| 15 | `3d 01 03 0f 03 ff b4 0d 0a` | `3d 02 01 0f b9 0d 0a` |
| 16 | `3d 01 03 10 03 ff 51 0d 0a` | `3d 02 01 10 e4 0d 0a` |
| 17 | `3d 01 03 11 03 ff 3a 0d 0a` | `3d 02 01 11 e3 0d 0a` |

These 34 frames and the four examples above are test vectors (`tests/test_protocol.py`).

## What the protocol leaves open, and the rules the driver follows

1. **"LED *n* off" is byte for byte the same as an OFF reply with status *n*.**
   `3d 02 01 01 93 0d 0a` is both "switch LED 1 off" and "OFF failed". On a link that
   echoes (RS-485 adapters, some wireless bridges) a command would read as its own
   refusal. Rule: seven received bytes identical to the command just sent are an echo
   and are dropped; the driver goes on waiting for the reply.
2. **A late reply would be credited to the next command**, because replies carry no LED
   number. Rule: the input buffer is cleared before every command; one command is sent
   at a time; after any command that needed a resend, the driver waits 60 ms and clears
   the buffer again.
3. **The receiver resynchronises.** It looks for `0x3D` in the byte stream, checks the
   function and length bytes, takes seven bytes and verifies the CRC; if that fails it
   continues from the next byte. A complete reply with the wrong function code is
   dropped. The result does not depend on how `read()` happened to split the bytes
   (property-tested).
4. **Timeout and resend.** 200 ms per attempt (configurable 100-2000 ms), three attempts.
   ON and OFF set an absolute state, so a resend does no harm. Observed reply delay is
   10-20 ms.
   - No valid reply after three attempts: `LED_NO_ACK`; the LED's state is recorded as
     **unknown**, and after an ON an OFF is sent before the error is raised.
   - A reply with a non-zero status: `LED_REJECTED`, without a resend. A refusal is never
     turned into a success.
5. **There is no "all off" command.** The driver sends OFF to LEDs 1-17 in turn, on
   connecting and on leaving. If one is not confirmed it reports
   `LED_SHUTDOWN_UNCONFIRMED` and does not claim the LED is off.

Every command is kept as a `Transaction` (bytes sent, bytes received, bytes purged,
attempts, why an attempt failed, timings); the last 200 are available as
`Illuminator.transactions`, and `vispek-hc check` reports the reply delay (median and
99th percentile) and the share of commands that needed a resend.

## Not verified yet

How LED numbers 18-22 behave; what the firmware replies to a number that does not exist;
why replies get lost.

An LED does not go out by itself when the host disconnects: LED 6 stayed lit for the
15 s it was watched after USB1 was pulled, with USB2 connected and with only the camera
cable left ([acceptance](acceptance/2026-10-02-unplug-test.md)).

Measured on one device ([acceptance](acceptance/2026-10-02-acceptance-leds-5-17.md)):
about one command in twenty gets no reply at all, whether the pause before it was 10 or
100 ms and whatever the timeout; the resend is answered. Whether the device acted on the
unanswered command is not known.
