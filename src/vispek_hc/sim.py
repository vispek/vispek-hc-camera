# SPDX-License-Identifier: Apache-2.0
"""A simulated HC1500: develop, test and demo without hardware.

Everything here runs on a :class:`VirtualClock`, so a full 17-channel scan takes
milliseconds of real time while every delay, timeout and exposure window stays exact.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

import numpy as np
from numpy.typing import NDArray

from vispek_hc.errors import FrameIncompleteError
from vispek_hc.illuminator import Illuminator
from vispek_hc.interfaces import ControlInfo, Frame
from vispek_hc.models import HC1500, DeviceModel
from vispek_hc.protocol import Command, Function, encode_reply, parse_command
from vispek_hc.video import STALE_S, wait_timeout


class VirtualClock:
    """Time that only moves when somebody sleeps or waits on it."""

    def __init__(self, start_utc: datetime = datetime(2026, 1, 1, tzinfo=UTC)) -> None:
        self._now = 0.0
        self._start_utc = start_utc

    def now(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.advance_to(self._now + seconds)

    def advance_to(self, when: float) -> None:
        self._now = max(self._now, when)

    def utc(self) -> datetime:
        return self.utc_at(self._now)

    def utc_at(self, when: float) -> datetime:
        return self._start_utc + timedelta(seconds=when)


@dataclass(frozen=True, slots=True)
class SimReply:
    """How the simulated light source answers one command."""

    lost: bool = False  # the reply never arrives; the command is still carried out
    delay_s: float | None = None  # None: the device's usual delay
    status: int = 0  # non-zero: the device refuses and does nothing
    prefix: bytes = b""  # extra bytes on the wire before the reply


class SimLightSource:
    """The serial side of a simulated light source.

    Use it wherever a serial link is expected. Queue faults with :meth:`script`; inspect
    what the device did through ``log``, ``lit`` and ``switch_times``.
    """

    def __init__(
        self,
        clock: VirtualClock,
        *,
        reply_delay_s: float = 0.015,
        baud: int = 115200,
        echo: bool = False,
    ) -> None:
        self._clock = clock
        self._reply_delay_s = reply_delay_s
        self._device_baud = baud
        self._echo = echo
        self._script: deque[SimReply] = deque()
        self._incoming: list[tuple[float, bytes]] = []  # (arrival time, bytes), host side
        self.link_baud = 115200
        self.raw_writes: list[tuple[int, bytes]] = []  # every write, whatever the baud rate
        self.log: list[tuple[float, Command]] = []  # commands the device understood
        self.lit: dict[int, int] = {}  # LED id -> PWM, for LEDs that are on now
        self.switch_times: list[tuple[float, int, int]] = []  # (time, LED id, PWM; 0 = off)
        self.dead_leds: set[int] = set()  # these accept commands and give no light
        self.unplugged = False
        self.closed = False
        self.on_write: Callable[[], None] | None = None

    def script(self, *replies: SimReply) -> None:
        """Queue how the next commands are answered; afterwards replies are normal again."""
        self._script.extend(replies)

    def inject(self, data: bytes) -> None:
        """Put bytes on the wire towards the host now, as if left over from earlier."""
        self._incoming.append((self._clock.now(), data))

    @property
    def unread(self) -> bytes:
        """Bytes that have arrived and that the host has not read yet."""
        now = self._clock.now()
        return b"".join(data for at, data in self._incoming if at <= now)

    # --- serial link -------------------------------------------------------------------

    def write(self, data: bytes) -> None:
        if self.unplugged:
            raise OSError("simulated device is unplugged")
        if self.on_write:
            self.on_write()
        now = self._clock.now()
        self.raw_writes.append((self.link_baud, data))
        if self.link_baud != self._device_baud:
            return  # the device sees noise
        command = parse_command(data)
        self.log.append((now, command))
        reply = self._script.popleft() if self._script else SimReply()
        if self._echo:
            self._incoming.append((now, data))
        delay = self._reply_delay_s if reply.delay_s is None else reply.delay_s
        if reply.prefix:
            self._incoming.append((now + delay, reply.prefix))
        if reply.status == 0:
            self._switch(now, command)
        if not reply.lost:
            self._incoming.append((now + delay, encode_reply(command.function, reply.status)))

    def _switch(self, now: float, command: Command) -> None:
        if command.function is Function.ON and command.pwm:
            self.lit[command.led_id] = command.pwm
        else:
            self.lit.pop(command.led_id, None)
        self.switch_times.append((now, command.led_id, self.lit.get(command.led_id, 0)))

    def lit_intervals(self) -> list[tuple[int, int, float, float]]:
        """Every stretch an LED was on: (LED id, PWM, from, until); ``inf`` if still on."""
        intervals = []
        since: dict[int, tuple[float, int]] = {}
        for when, led_id, pwm in self.switch_times:
            if led_id in self.dead_leds:
                continue
            if led_id in since:
                start, previous_pwm = since.pop(led_id)
                intervals.append((led_id, previous_pwm, start, when))
            if pwm:
                since[led_id] = (when, pwm)
        return intervals + [
            (led_id, pwm, start, math.inf) for led_id, (start, pwm) in since.items()
        ]

    def read(self, timeout_s: float) -> bytes:
        if self.unplugged:
            raise OSError("simulated device is unplugged")
        deadline = self._clock.now() + timeout_s
        arrivals = [at for at, _ in self._incoming if at <= deadline]
        self._clock.advance_to(min(arrivals) if arrivals else deadline)
        return self.discard_input()

    def discard_input(self) -> bytes:
        now = self._clock.now()
        arrived = sorted((item for item in self._incoming if item[0] <= now), key=lambda i: i[0])
        self._incoming = [item for item in self._incoming if item[0] > now]
        return b"".join(data for _, data in arrived)

    def set_baud(self, baud: int) -> None:
        self.link_baud = baud

    def close(self) -> None:
        self.closed = True


class SimFrameSource:
    """A simulated rolling-shutter camera looking at a flat target under the simulated LEDs.

    Frame *k* of a stream starts exposing ``k / fps`` after the stream opened. Its rows are
    exposed one after another over ``readout_s``, each for ``exposure_s``, and the frame
    reaches the host ``latency_s`` after its last row. A frame read out while an LED
    switches is therefore lit in one part and dark in the other, as on the real device.

    A pixel under LED *n* reads ``black level + reflectance x white-board RGB x PWM /
    starting PWM`` (the model's white-board observation), clipped at 255.
    """

    def __init__(
        self,
        clock: VirtualClock,
        light: SimLightSource,
        *,
        size: tuple[int, int] = (64, 36),
        fps: float = 10.0,
        exposure_s: float = 0.0157,
        readout_s: float = 1 / 30,
        latency_s: float = 0.150,
        reflectance: NDArray[np.float64] | float = 1.0,
        model: DeviceModel = HC1500,
    ) -> None:
        """``reflectance``: what the scene sends back, 1.0 being the white board. A number,
        a (height, width) map, or a (height, width, LEDs) array with a layer per LED."""
        self._clock = clock
        self._light = light
        self._size = size
        self._fps = fps
        self._exposure_s = exposure_s
        self._readout_s = readout_s
        self._latency_s = latency_s
        self._model = model
        self.show(reflectance)
        self._epoch = 0.0  # when the current stream opened
        self._slot = 0  # index of the next frame of the current stream
        self._down_until: float | None = math.inf  # not started
        self._stalls: list[tuple[float, float]] = []
        self._seq = 0
        self._arrived: tuple[float, float, int] | None = None  # newest: start, arrival, gen.
        self._latest: Frame | None = None
        self.generation = 0
        self.on_restart: Callable[[], None] | None = None
        self.ambient = 0.0  # code values of room light in every pixel: an open lid
        self._kept_gain = 1.0

    def exposure_mode_changed(self, mode: int) -> None:
        """The camera's hidden gain. Automatic exposure raises it to its ceiling in
        darkness and lowers it under light; switching to manual keeps it where it is.
        Measured on the device: a factor of 4.4 between the two, read back by no control.
        Pictures made in automatic mode are rendered at the ceiling."""
        manual = mode == 1
        self._kept_gain = 1 / KEPT_GAIN_RANGE if manual and self._sees_light() else 1.0

    def _sees_light(self) -> bool:
        return self.ambient > 0 or bool(self._light.lit)

    @property
    def measured_fps(self) -> float | None:
        return self._fps

    def show(self, reflectance: NDArray[np.float64] | float) -> None:
        """Put another target in front of the camera: frames rendered from now on see it."""
        width, height = self._size
        leds = len(self._model.led_table.leds)
        if np.ndim(reflectance) == 3 and np.shape(reflectance) != (height, width, leds):
            raise ValueError(
                f"reflectance of shape {np.shape(reflectance)}: give (height, width), or "
                f"(height, width, {leds}) with one layer per LED"
            )
        # One map for every LED, or one layer per LED (index = LED number - 1).
        self._reflectance: NDArray[np.float64] = (
            np.array(reflectance, np.float64)  # a copy: later changes by the caller stay out
            if np.ndim(reflectance) == 3
            else np.broadcast_to(reflectance, (height, width))
        )

    def start(self) -> None:
        self._down_until = self._clock.now()

    def stop(self) -> None:
        self._pump(self._clock.now())
        self._down_until = math.inf

    def stall(self, seconds: float) -> None:
        """No frame arrives for this long; the stream itself stays open."""
        now = self._clock.now()
        self._stalls.append((now, now + seconds))

    def drop_out(self, seconds: float) -> None:
        """The stream dies now and is reopened after this long, as a new generation."""
        self._pump(self._clock.now())
        self._down_until = self._clock.now() + seconds

    def latest(self) -> Frame | None:
        self._pump(self._clock.now())
        if self._arrived is None:
            return None
        if self._latest is None or self._latest.seq != self._seq:
            self._latest = self._render(*self._arrived)  # only frames somebody looks at
        return self._latest

    def is_stale(self) -> bool:
        latest = self.latest()
        return latest is not None and self._clock.now() - latest.received_at > STALE_S

    def wait_new(self, count: int = 1, timeout: float | None = None) -> Frame:
        now = self._clock.now()
        self._pump(now)
        baseline = self._seq
        deadline = now + (wait_timeout(count, self._fps) if timeout is None else timeout)
        while self._seq - baseline < count:
            upcoming = self._arrival() if self._down_until is None else self._down_until
            if upcoming > deadline:
                self._clock.advance_to(deadline)
                raise FrameIncompleteError(
                    f"{self._seq - baseline} of {count} new frames arrived in time",
                    details={"wanted": count, "arrived": self._seq - baseline},
                )
            self._clock.advance_to(upcoming)
            self._pump(upcoming)
        latest = self.latest()
        assert latest is not None
        return latest

    def _arrival(self) -> float:
        period = self._slot / self._fps
        return self._epoch + period + self._exposure_s + self._readout_s + self._latency_s

    def _pump(self, until: float) -> None:
        """Account for every frame that arrives up to ``until``."""
        while True:
            if self._down_until is not None:
                if self._down_until > until:
                    return
                self._epoch, self._slot, self._down_until = self._down_until, 0, None
                self.generation += 1
                if self.generation > 1 and self.on_restart:
                    self.on_restart()
            arrival = self._arrival()
            if arrival > until:
                return
            slot, self._slot = self._slot, self._slot + 1
            if any(start <= arrival < end for start, end in self._stalls):
                continue
            self._seq += 1
            self._arrived = (self._epoch + slot / self._fps, arrival, self.generation)

    def _render(self, exposure_start: float, arrival: float, generation: int) -> Frame:
        width, height = self._size
        row_start = exposure_start + np.arange(height) * (self._readout_s / height)
        image = np.full((height, width, 3), float(self._model.black_level))
        image += self.ambient * self._kept_gain
        for led_id, pwm, lit_from, lit_until in self._light.lit_intervals():
            led = self._model.led(led_id)
            overlap = np.minimum(row_start + self._exposure_s, lit_until) - np.maximum(
                row_start, lit_from
            )
            # Rounded: a row lit for the whole exposure is lit exactly 1.0 of it, whatever
            # the floating-point sum of the times says.
            share = np.round(np.clip(overlap, 0, None) / self._exposure_s, 9)
            colour = np.array(led.white_rgb) * pwm / led.starting_pwm * self._kept_gain
            seen = self._reflectance
            if seen.ndim == 3:
                seen = seen[:, :, led_id - 1]
            image += share[:, None, None] * colour[None, None, :] * seen[:, :, None]
        data = np.clip(np.floor(image + 0.5), 0, 255).astype(np.uint8).tobytes()
        window = (float(row_start[0]), float(row_start[-1]) + self._exposure_s)
        return Frame(
            self._seq, generation, arrival, self._clock.utc_at(arrival), width, height,
            "rgb24", data, window,
        )  # fmt: skip


KEPT_GAIN_RANGE = 4.4  # ceiling over floor of the gain the camera keeps (measured)

# Plausible ranges for the simulated camera: (minimum, maximum, step). Not measured.
_SIM_RANGES = {
    "white-balance-temp": (2800, 6500, 1),
    "exposure-time-abs": (1, 5000, 1),
    "gain": (0, 100, 1),
    "brightness": (-64, 64, 1),
    "contrast": (0, 64, 1),
    "saturation": (0, 128, 1),
    "sharpness": (0, 6, 1),
    "gamma": (72, 500, 1),
    "power-line-frequency": (0, 2, 1),
    "backlight-compensation": (0, 2, 1),
}
_SIM_AUTOMATIC = {"auto-white-balance-temp": 1, "auto-exposure-mode": 8, "auto-focus": 1}


class SimControls:
    """Simulated camera parameters. A camera fresh from a stream start is in automatic mode."""

    def __init__(self, model: DeviceModel = HC1500) -> None:
        self.info: dict[str, ControlInfo] = {
            name: ControlInfo(value, *_SIM_RANGES.get(name, (None, None, None)))
            for name, value in {
                **model.initial_controls,
                **_SIM_AUTOMATIC,
                "focus-abs": 360,
            }.items()
        }
        self.writes: list[tuple[str, int]] = []
        self.quantise: dict[str, Callable[[int], int]] = {}  # what the camera really stores
        self.on_exposure_mode: Callable[[int], None] | None = None  # told when it changes

    @property
    def values(self) -> dict[str, int]:
        return {name: info.current for name, info in self.info.items()}

    def read_all(self) -> dict[str, ControlInfo]:
        return dict(self.info)

    def write(self, name: str, value: int) -> None:
        self.writes.append((name, value))
        stored = self.quantise[name](value) if name in self.quantise else value
        self._store(name, stored)

    def _store(self, name: str, stored: int) -> None:
        changed = self.info[name].current != stored
        self.info[name] = replace(self.info[name], current=stored)
        if name == "auto-exposure-mode" and changed and self.on_exposure_mode:
            self.on_exposure_mode(stored)

    def reset_to_automatic(self) -> None:
        """What reopening the video stream can do to the real camera."""
        for name, value in _SIM_AUTOMATIC.items():
            if name in self.info:
                self._store(name, value)


class SimulatedRig:
    """A complete simulated HC1500: light source, camera and controls on one virtual clock.

    ``device`` is the serial side of the light source (script faults there); ``light`` is
    the ordinary driver talking to it. Camera options go to :class:`SimFrameSource`.
    """

    def __init__(
        self,
        *,
        allow_uv: bool = False,
        size: tuple[int, int] = (64, 36),
        fps: float = 10.0,
        latency_s: float = 0.150,
        reflectance: NDArray[np.float64] | float = 1.0,
        reply_delay_s: float = 0.015,
    ) -> None:
        self.size, self.fps = size, fps
        self.clock = VirtualClock()
        self.device = SimLightSource(self.clock, reply_delay_s=reply_delay_s)
        self.light = Illuminator(self.device, allow_uv=allow_uv, clock=self.clock, port="sim")
        self.frames = SimFrameSource(
            self.clock,
            self.device,
            size=size,
            fps=fps,
            latency_s=latency_s,
            reflectance=reflectance,
        )
        self.controls = SimControls()
        self.frames.on_restart = self.controls.reset_to_automatic
        self.controls.on_exposure_mode = self.frames.exposure_mode_changed
