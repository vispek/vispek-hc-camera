# SPDX-License-Identifier: Apache-2.0
"""Light-source serial protocol: frame building, parsing and resynchronisation.

Pure functions and one small buffer class. No I/O happens here.

Host to device::

    3D | function | length | data ... | CRC8 | 0D 0A

``function`` is 01 (LED on; data = LED id, PWM high byte, PWM low byte) or 02 (LED off;
data = LED id). The CRC covers everything before it. The device answers with 7 bytes::

    3D | function | 01 | status | CRC8 | 0D 0A

``status`` 00 means success. A reply does not say which LED it is about.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass

HEAD = 0x3D
TAIL = b"\r\n"
LED_ID_RANGE = range(1, 23)  # the protocol allows 1-22; the HC1500 populates 1-17
PWM_RANGE = range(1024)
REPLY_SIZE = 7


class Function(enum.IntEnum):
    ON = 0x01
    OFF = 0x02


_DATA_SIZE = {Function.ON: 3, Function.OFF: 1}


def _crc8_table() -> bytes:
    table = bytearray()
    for value in range(256):
        crc = value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
        table.append(crc)
    return bytes(table)


_CRC8 = _crc8_table()


def crc8(data: bytes | bytearray) -> int:
    """CRC-8, polynomial 0x07, initial value 0, no reflection, no final XOR."""
    crc = 0
    for byte in data:
        crc = _CRC8[crc ^ byte]
    return crc


def _frame(function: Function, data: bytes) -> bytes:
    body = bytes([HEAD, function, len(data)]) + data
    return body + bytes([crc8(body)]) + TAIL


def _check_led_id(led_id: int) -> None:
    if led_id not in LED_ID_RANGE:
        raise ValueError(f"LED id {led_id} is outside the protocol range 1-22")


def encode_on(led_id: int, pwm: int) -> bytes:
    """Command that lights one LED at a PWM level of 0-1023."""
    _check_led_id(led_id)
    if pwm not in PWM_RANGE:
        raise ValueError(f"PWM {pwm} is outside 0-1023")
    return _frame(Function.ON, bytes([led_id, pwm >> 8, pwm & 0xFF]))


def encode_off(led_id: int) -> bytes:
    """Command that switches one LED off."""
    _check_led_id(led_id)
    return _frame(Function.OFF, bytes([led_id]))


def encode_reply(function: Function, status: int = 0) -> bytes:
    """The 7 bytes a device sends back. Used by the simulated device and by tests."""
    return _frame(function, bytes([status]))


@dataclass(frozen=True, slots=True)
class Command:
    function: Function
    led_id: int
    pwm: int | None  # None for OFF


def parse_command(frame: bytes) -> Command:
    """Decode one complete host-to-device frame. Raises ``ValueError`` if it is not one."""
    if len(frame) < 3 or frame[0] != HEAD or frame[1] not in _DATA_SIZE:
        raise ValueError(f"not a command frame: {frame.hex(' ')}")
    function = Function(frame[1])
    size = _DATA_SIZE[function]
    if frame[2] != size or len(frame) != size + 6:
        raise ValueError(f"command frame has the wrong length: {frame.hex(' ')}")
    if crc8(frame[: size + 3]) != frame[size + 3] or frame[size + 4 :] != TAIL:
        raise ValueError(f"command frame fails its CRC or tail check: {frame.hex(' ')}")
    led_id = frame[3]
    pwm = (frame[4] << 8) | frame[5] if function is Function.ON else None
    if led_id not in LED_ID_RANGE or (pwm is not None and pwm not in PWM_RANGE):
        raise ValueError(f"command frame carries an out-of-range value: {frame.hex(' ')}")
    return Command(function, led_id, pwm)


@dataclass(frozen=True, slots=True)
class Reply:
    function: Function
    status: int

    @property
    def accepted(self) -> bool:
        return self.status == 0

    def to_bytes(self) -> bytes:
        return encode_reply(self.function, self.status)


class ReplyScanner:
    """Finds replies in a byte stream, whatever the stream is cut into.

    A candidate starts at ``3D``; anything that fails a check is abandoned by dropping a
    single byte, so a reply that starts inside a damaged one is still found.
    """

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.skipped = 0  # bytes dropped so far because they were not part of a valid reply

    def reset(self) -> None:
        self._buffer.clear()
        self.skipped = 0

    @property
    def pending(self) -> bytes:
        """Bytes that may still turn out to be the start of a reply."""
        return bytes(self._buffer)

    def feed(self, data: bytes) -> list[Reply]:
        buffer = self._buffer
        buffer += data
        replies: list[Reply] = []
        start = 0
        while True:
            head = buffer.find(HEAD, start)
            if head < 0:
                self.skipped += len(buffer) - start
                start = len(buffer)
                break
            self.skipped += head - start
            start = head
            candidate = buffer[start : start + REPLY_SIZE]
            plausible = (len(candidate) < 2 or candidate[1] in _DATA_SIZE) and (
                len(candidate) < 3 or candidate[2] == 1
            )
            if plausible and len(candidate) < REPLY_SIZE:
                break  # wait for the rest
            if plausible and crc8(candidate[:4]) == candidate[4] and candidate[5:] == TAIL:
                replies.append(Reply(Function(candidate[1]), candidate[3]))
                start += REPLY_SIZE
            else:
                self.skipped += 1
                start += 1
        del buffer[:start]
        return replies


class Verdict(enum.Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    ECHO = "echo"
    WRONG_FUNCTION = "wrong_function"


def judge(reply: Reply, sent: bytes) -> Verdict:
    """Classify a reply against the command frame that is waiting for an answer.

    An OFF command for LED *n* is byte-for-byte a "status *n*" OFF reply, so a link that
    echoes what it is sent would otherwise read as a rejection. Such bytes are an echo.
    """
    if reply.to_bytes() == sent:
        return Verdict.ECHO
    if reply.function != sent[1]:
        return Verdict.WRONG_FUNCTION
    return Verdict.ACCEPTED if reply.accepted else Verdict.REJECTED
