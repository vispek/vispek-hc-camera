# SPDX-License-Identifier: Apache-2.0
"""The seams between the parts: clock, light source, frame source, camera controls.

Real devices and the simulated ones in ``vispek_hc.sim`` implement the same protocols, so
a scan runs unchanged against either.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal, Protocol

PixelFormat = Literal["yuyv422", "mjpeg", "rgb24"]
ColorRange = Literal["full", "limited"]


class Clock(Protocol):
    def now(self) -> float:
        """Monotonic seconds."""
        ...

    def sleep(self, seconds: float) -> None: ...

    def utc(self) -> datetime: ...


class SystemClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)

    def utc(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class Transaction:
    """One light-source command and everything that happened on the wire for it."""

    led_id: int
    pwm: int | None  # None for OFF
    sent: bytes
    received: bytes  # raw bytes seen while waiting, at most the last 128
    purged: bytes  # bytes thrown away before sending and after a retried command
    attempts: int
    failure: str | None  # why the last failed attempt failed; None if the first one worked
    accepted: bool
    started_at: float  # clock seconds
    acked_at: float | None  # clock seconds when the accepted reply was read
    acked_at_utc: datetime | None
    ack_delay_s: float | None  # last send to accepted reply
    duration_s: float  # whole command, retries included


@dataclass(frozen=True, slots=True)
class LedState:
    state: Literal["off", "on", "unknown"]
    pwm: int | None = None


class LightSource(Protocol):
    def on(self, led_id: int, pwm: int) -> Transaction: ...

    def off(self, led_id: int) -> Transaction: ...

    def all_off(self) -> None: ...

    @property
    def states(self) -> Mapping[int, LedState]: ...

    @property
    def transactions(self) -> Sequence[Transaction]: ...


@dataclass(frozen=True, slots=True)
class Frame:
    seq: int  # grows by one per frame really received; never goes back across reconnects
    generation: int  # grows by one each time the stream is reopened
    received_at: float  # clock seconds when the frame reached the host
    received_utc: datetime
    width: int
    height: int
    pixel_format: PixelFormat
    data: bytes
    # Simulated frames know when their sensor rows were exposed (clock seconds).
    exposure_window: tuple[float, float] | None = None
    # How YUV code values map to 0-255 (only yuyv422 frames are converted with it).
    color_range: ColorRange = "full"


class FrameSource(Protocol):
    generation: int

    def start(self) -> None: ...

    def stop(self) -> None: ...

    def latest(self) -> Frame | None: ...

    def wait_new(self, count: int = 1, timeout: float | None = None) -> Frame:
        """Block until ``count`` frames have arrived after the call; return the newest.

        Raises ``FRAME_INCOMPLETE`` on timeout (default: max(3 s, count / fps + 2 s)).
        """
        ...

    def is_stale(self) -> bool: ...

    @property
    def measured_fps(self) -> float | None: ...


@dataclass(frozen=True, slots=True)
class ControlInfo:
    current: int
    minimum: int | None = None
    maximum: int | None = None
    step: int | None = None


class CameraControls(Protocol):
    def read_all(self) -> dict[str, ControlInfo]: ...

    def write(self, name: str, value: int) -> None: ...
