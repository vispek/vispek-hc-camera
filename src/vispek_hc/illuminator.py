# SPDX-License-Identifier: Apache-2.0
"""Driver for the LED light source on its serial port.

One command at a time, each waiting for its own reply: replies do not say which LED they
are about, and an OFF command for LED *n* is byte-identical to a "status *n*" OFF reply.
So the input is flushed before every send, a lost reply is retried (ON and OFF are
absolute, repeating them is harmless), and after a retried command the line is given 60 ms
and flushed again so that a late duplicate cannot be credited to the next command.

Safety rules kept here: an ON that is not confirmed is followed by an OFF; lighting one
LED switches off every LED not known to be off; closing switches everything off.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import sys
import threading
import time
from collections import deque
from collections.abc import Iterable, Mapping, Sequence
from types import TracebackType
from typing import Any, Protocol, Self

import serial
import serial.tools.list_ports

from vispek_hc import safety
from vispek_hc.errors import (
    ConfigRejectedError,
    DeviceNotFoundError,
    InvalidStateError,
    LedNoAckError,
    LedRejectedError,
    LedShutdownUnconfirmedError,
    NoProtocolReplyError,
    PermissionDeniedError,
    PortBusyError,
    choose_one,
)
from vispek_hc.interfaces import Clock, LedState, SystemClock, Transaction
from vispek_hc.models import HC1500, DeviceModel
from vispek_hc.protocol import (
    PWM_RANGE,
    ReplyScanner,
    Verdict,
    encode_off,
    encode_on,
    judge,
)

if sys.platform != "win32":
    import fcntl
    import termios

log = logging.getLogger(__name__)

BAUD_RATES = (115200, 9600, 57600, 38400, 19200)  # also the probing order
ATTEMPTS = 3
ACK_TIMEOUT_RANGE_S = (0.1, 2.0)
COMMAND_GAP_S = 0.010
QUIET_S = 0.060  # pause before a resend and after a retried command
EMERGENCY_WAIT_S = 0.100
UV_MAX_ON_S = 10.0
POLL_S = 0.005  # how long one serial read blocks
WRITE_TIMEOUT_S = 1.0
_NAME_PREFIXES = {
    "darwin": ("/dev/cu.usbserial", "/dev/cu.wchusbserial"),
    "linux": ("/dev/ttyUSB", "/dev/ttyACM"),
}
# Measured: an LED stays lit with only the camera cable connected.
UNPLUG_HINT = (
    "an LED may still be lit; unplug every cable of the device, the camera cable too, to be sure"
)


class SerialLink(Protocol):
    def write(self, data: bytes) -> None: ...

    def read(self, timeout_s: float) -> bytes:
        """Whatever arrives within the timeout; returns as soon as there is something."""
        ...

    def discard_input(self) -> bytes: ...

    def set_baud(self, baud: int) -> None: ...

    def close(self) -> None: ...


class _PySerialLink:
    def __init__(self, port: serial.Serial) -> None:
        self.serial = port

    def write(self, data: bytes) -> None:
        self.serial.write(data)
        self.serial.flush()

    def read(self, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        while True:
            data: bytes = self.serial.read(max(1, self.serial.in_waiting))  # blocks <= POLL_S
            if data or time.monotonic() >= deadline:
                return data

    def discard_input(self) -> bytes:
        waiting: bytes = self.serial.read(self.serial.in_waiting)
        return waiting

    def set_baud(self, baud: int) -> None:
        self.serial.baudrate = baud

    def close(self) -> None:
        self.serial.close()


def open_link(port: str, baud: int) -> _PySerialLink:
    """Open a serial port for exclusive use (8 data bits, no parity, 1 stop bit)."""
    try:
        opened = serial.Serial(
            port, baud, timeout=POLL_S, write_timeout=WRITE_TIMEOUT_S, exclusive=True
        )
    except (serial.SerialException, OSError) as error:
        details = {"port": port}
        text = str(error)
        busy = "exclusively lock" in text or "PermissionError(13" in text  # the latter: Windows
        if busy or error.errno in (errno.EBUSY, errno.EAGAIN):
            raise PortBusyError(f"{port} is in use by another program", details=details) from error
        if error.errno in (errno.EACCES, errno.EPERM):
            raise PermissionDeniedError(
                f"no permission to open {port}",
                hint="Linux: add your user to the 'dialout' group and log in again",
                details=details,
            ) from error
        raise DeviceNotFoundError(f"cannot open {port}: {error}", details=details) from error
    if sys.platform != "win32":
        # pyserial's exclusive flag is an advisory lock; this also refuses plain open() calls.
        with contextlib.suppress(OSError):
            fcntl.ioctl(opened.fileno(), termios.TIOCEXCL)
    return _PySerialLink(opened)


def candidate_ports(ports: Iterable[Any], platform: str = sys.platform) -> list[str]:
    """Serial ports that could be the light source.

    A port qualifies by its USB vendor id (CH341); where the system reports no vendor id,
    by its name. ``ports`` are pyserial ``list_ports`` entries.
    """
    prefixes = _NAME_PREFIXES.get(platform, ())
    return [
        port.device
        for port in ports
        if port.vid == HC1500.serial_usb_vid
        or (port.vid is None and port.device.startswith(prefixes))
    ]


def find_ports() -> list[str]:
    """Serial ports on this computer that could be the light source."""
    return candidate_ports(serial.tools.list_ports.comports())


def choose_port(explicit: str | None, candidates: Sequence[str]) -> str:
    return choose_one(
        explicit,
        candidates,
        what="light-source serial port",
        option="--port",
        hint="plug the light source's USB1 port in, or name the port with --port",
    )


def _check_baud(baud: int | None) -> None:
    if baud is not None and baud not in BAUD_RATES:
        raise ConfigRejectedError(
            f"baud rate {baud} is not supported",
            details={"baud": baud, "valid": list(BAUD_RATES)},
        )


def _check_ack_timeout(ack_timeout_s: float) -> None:
    low, high = ACK_TIMEOUT_RANGE_S
    if not low <= ack_timeout_s <= high:
        raise ConfigRejectedError(
            f"reply timeout {ack_timeout_s * 1000:g} ms is outside 100-2000 ms",
            details={"ack_timeout_ms": ack_timeout_s * 1000},
        )


def _percentile(sorted_values: Sequence[float], fraction: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(fraction * len(sorted_values)))]


class Illuminator:
    """The light source. Use as a context manager: leaving it switches every LED off.

    From creation until :meth:`close` the object is registered for the process-level
    all-off (interpreter exit, termination signals).
    """

    def __init__(
        self,
        link: SerialLink,
        *,
        model: DeviceModel = HC1500,
        allow_uv: bool = False,
        ack_timeout_s: float = 0.2,
        clock: Clock | None = None,
        port: str | None = None,
    ) -> None:
        _check_ack_timeout(ack_timeout_s)
        self._link = link
        self._model = model
        self.clock: Clock = clock or SystemClock()
        self._ack_timeout_s = ack_timeout_s
        self._lock = threading.Lock()
        self._last_command_end: float | None = None
        self._transactions: deque[Transaction] = deque(maxlen=200)
        self._states = {led.led_id: LedState("unknown") for led in model.led_table.leds}
        self._watchdogs: dict[int, threading.Timer] = {}
        self._halted = False  # set by the emergency all-off; cleared by a confirmed all_off()
        self._closed = False
        self.allow_uv = allow_uv
        self.port = port
        self.baud: int | None = None
        safety.protect(self)

    @classmethod
    def open(
        cls,
        port: str | None = None,
        *,
        baud: int | None = None,
        allow_uv: bool = False,
        ack_timeout_s: float = 0.2,
    ) -> Self:
        """Find the serial port, open it, settle the baud rate and switch every LED off."""
        _check_baud(baud)
        _check_ack_timeout(ack_timeout_s)
        chosen = choose_port(port, find_ports())
        light = cls(
            open_link(chosen, baud or BAUD_RATES[0]),
            allow_uv=allow_uv,
            ack_timeout_s=ack_timeout_s,
            port=chosen,
        )
        try:
            light.connect(baud=baud)
        except BaseException:
            light.close()
            raise
        return light

    @property
    def ack_timeout_s(self) -> float:
        return self._ack_timeout_s

    @property
    def states(self) -> Mapping[int, LedState]:
        return self._states

    @property
    def transactions(self) -> Sequence[Transaction]:
        return self._transactions

    def connect(self, baud: int | None = None) -> None:
        """Settle the baud rate (probing with OFF commands only) and switch every LED off."""
        _check_baud(baud)
        self.baud = baud or self._probe_baud()
        self._link.set_baud(self.baud)
        self.all_off()

    def _probe_baud(self) -> int:
        for rate in BAUD_RATES:
            self._link.set_baud(rate)
            try:
                self._command(1, None)  # probing must never light anything: OFF only
            except LedNoAckError:
                self._transactions.clear()  # silence at a wrong rate says nothing about loss
                continue
            except LedRejectedError:
                pass  # a refusal is still a well-formed reply at this baud rate
            return rate
        raise NoProtocolReplyError(
            "the light source did not answer at any baud rate",
            hint="check that USB1 is connected and that no other program holds the port",
            details={"tried": list(BAUD_RATES)},
        )

    def on(
        self, led_id: int, pwm: int, *, exclusive: bool = True, max_on_s: float | None = None
    ) -> Transaction:
        """Light one LED.

        Unless ``exclusive=False``, every other LED not known to be off is switched off
        first: lighting several at once can overload the USB1 port. ``max_on_s`` arms a
        watchdog that switches the LED off again; UV LEDs always get one (10 s unless
        given). If the ON is not confirmed, an OFF is sent before the error is raised.
        """
        led = self._model.led(led_id)
        safety.check_channels(self._model, [led_id], allow_uv=self.allow_uv)
        if pwm not in PWM_RANGE:
            raise ConfigRejectedError(f"PWM {pwm} is outside 0-1023", details={"pwm": pwm})
        if self._closed:
            raise InvalidStateError("the light source is closed")
        if self._halted:
            raise InvalidStateError(
                "an emergency shutdown switched the LEDs off",
                hint="call all_off() to confirm the state before lighting an LED again",
            )
        if exclusive:
            for other, state in self._states.items():
                if state.state != "off" and other != led_id:
                    self.off(other)
        if max_on_s is None and led.needs_uv_permission:
            max_on_s = UV_MAX_ON_S
        if max_on_s is not None:
            self._arm_watchdog(led_id, max_on_s)  # before sending: the LED may light unconfirmed
        try:
            return self._command(led_id, pwm)
        except (LedNoAckError, LedRejectedError) as error:
            with contextlib.suppress(LedShutdownUnconfirmedError):
                self.off(led_id)
            error.details["off_confirmed"] = self._states[led_id].state == "off"
            raise

    def off(self, led_id: int) -> Transaction:
        """Switch one LED off. Raises ``LED_SHUTDOWN_UNCONFIRMED`` unless the device confirms."""
        self._model.led(led_id)
        try:
            transaction = self._command(led_id, None)
        except (LedNoAckError, LedRejectedError) as error:
            raise LedShutdownUnconfirmedError(
                f"LED {led_id} shutdown unconfirmed: {error.message}",
                hint=UNPLUG_HINT,
                details={"led_ids": [led_id]},
            ) from error
        if watchdog := self._watchdogs.pop(led_id, None):
            watchdog.cancel()  # only now: an unconfirmed OFF keeps its watchdog
        return transaction

    def all_off(self) -> None:
        """Send OFF to every LED (the protocol has no single command for it)."""
        unconfirmed = []
        for led_id in self._states:
            try:
                self.off(led_id)
            except LedShutdownUnconfirmedError:
                unconfirmed.append(led_id)
        if unconfirmed:
            raise LedShutdownUnconfirmedError(
                f"shutdown unconfirmed for LED {', '.join(map(str, unconfirmed))}",
                hint=UNPLUG_HINT,
                details={"led_ids": unconfirmed},
            )
        self._halted = False

    def emergency_off(self) -> list[int]:
        """Lock-free all-off for signal handlers, interpreter exit and :meth:`close`.

        Writes OFF for every LED without taking the command lock (the interrupted code may
        hold it), waits at most 100 ms for each reply, and never raises. Returns the LEDs
        whose shutdown was not confirmed. Afterwards ON commands are refused until a
        normal :meth:`all_off` succeeds, so that interrupted code cannot light an LED
        again behind the shutdown.
        """
        self._halted = True
        unconfirmed: list[int] = []
        for led_id in self._states:
            verdicts: list[Verdict] = []
            try:
                # After a silent LED its reply may still come: wait it out, then drop it.
                self.clock.sleep(QUIET_S if unconfirmed and unconfirmed[-1] == led_id - 1 else 0)
                self.clock.sleep(COMMAND_GAP_S)
                self._link.discard_input()  # a reply still in flight belongs to someone else
                frame = encode_off(led_id)
                self._link.write(frame)
                scanner = ReplyScanner()
                deadline = self.clock.now() + EMERGENCY_WAIT_S
                answered = {Verdict.ACCEPTED, Verdict.REJECTED}
                while not answered & set(verdicts) and (left := deadline - self.clock.now()) > 0:
                    verdicts += [
                        judge(reply, frame) for reply in scanner.feed(self._link.read(left))
                    ]
            except Exception:
                verdicts = []
            confirmed = Verdict.ACCEPTED in verdicts and Verdict.REJECTED not in verdicts
            self._states[led_id] = LedState("off" if confirmed else "unknown")
            if not confirmed:
                unconfirmed.append(led_id)
        return unconfirmed

    def ack_stats(self) -> dict[str, float | int | None]:
        """Reply delay and loss over the recent command log, for diagnostics."""
        recent = list(self._transactions)
        delays = sorted(t.ack_delay_s * 1000 for t in recent if t.ack_delay_s is not None)
        retried = sum(t.attempts > 1 for t in recent)
        return {
            "commands": len(recent),
            "retried": retried,
            "loss_rate": retried / len(recent) if recent else None,
            "delay_ms_p50": round(_percentile(delays, 0.50), 2) if delays else None,
            "delay_ms_p99": round(_percentile(delays, 0.99), 2) if delays else None,
        }

    def close(self) -> None:
        """Release the port. Any LED not known to be off gets a last OFF first; if that
        OFF is not confirmed, ``LED_SHUTDOWN_UNCONFIRMED`` is raised after the port is
        released.

        Another thread may be lighting an LED at this moment. From here on no new ON is
        sent, and one already on the wire is waited for, so that its LED is seen and
        switched off before the port goes.
        """
        self._halted = True
        patience = ATTEMPTS * (self._ack_timeout_s + QUIET_S) + 1.0
        acquired = self._lock.acquire(timeout=patience)
        unconfirmed: list[int] = []
        try:
            # Without the lock a command is stuck on the wire and the states cannot be
            # trusted: everything gets an OFF.
            if not acquired or any(state.state != "off" for state in self._states.values()):
                unconfirmed = self.emergency_off()
            for watchdog in list(self._watchdogs.values()):
                watchdog.cancel()
            self._watchdogs.clear()
            safety.release(self)
            self._closed = True
            self._link.close()
        finally:
            if acquired:
                self._lock.release()
        if unconfirmed:  # the port is released; the caller must not take the LEDs for off
            raise LedShutdownUnconfirmedError(
                "the port was closed with the shutdown unconfirmed for LED "
                + ", ".join(map(str, unconfirmed)),
                hint=UNPLUG_HINT,
                details={"led_ids": unconfirmed},
            )

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.all_off()
        finally:
            self.close()

    # --- internals ---------------------------------------------------------------------

    def _arm_watchdog(self, led_id: int, max_on_s: float) -> None:
        def expire() -> None:
            try:
                self.off(led_id)
            except Exception as error:  # this runs in a timer thread: nothing may escape
                log.error("watchdog could not switch LED %d off: %s", led_id, error)

        if previous := self._watchdogs.pop(led_id, None):
            previous.cancel()
        watchdog = threading.Timer(max_on_s, expire)
        watchdog.daemon = True
        self._watchdogs[led_id] = watchdog
        watchdog.start()

    def _attempt(self, frame: bytes, received: bytearray) -> tuple[Verdict | None, str, int]:
        """Send once and wait. Returns (verdict, failure reason, reply status)."""
        scanner = ReplyScanner()
        wrong_function = False
        try:
            self._link.write(frame)
            deadline = self.clock.now() + self._ack_timeout_s
            while (remaining := deadline - self.clock.now()) > 0:
                data = self._link.read(remaining)
                received += data
                for reply in scanner.feed(data):
                    verdict = judge(reply, frame)
                    if verdict in (Verdict.ACCEPTED, Verdict.REJECTED):
                        return verdict, "rejected", reply.status
                    wrong_function |= verdict is Verdict.WRONG_FUNCTION
        except OSError as error:  # includes serial.SerialException
            log.debug("serial I/O failed: %s", error)
            return None, "io", 0
        if wrong_function:
            return None, "wrong_function", 0
        if scanner.skipped:
            return None, "framing_or_crc", 0
        return None, "incomplete" if scanner.pending else "timeout", 0

    def _discard(self) -> bytes:
        try:
            return self._link.discard_input()
        except OSError:
            return b""

    def _command(self, led_id: int, pwm: int | None) -> Transaction:
        frame = encode_off(led_id) if pwm is None else encode_on(led_id, pwm)
        received = bytearray()
        purged = bytearray()
        verdict: Verdict | None = None
        failure: str | None = None
        status = 0
        with self._lock:
            if self._closed:
                raise InvalidStateError("the light source is closed")
            if self._last_command_end is not None:
                wait = COMMAND_GAP_S - (self.clock.now() - self._last_command_end)
                if wait > 0:
                    self.clock.sleep(wait)
            started = sent_at = self.clock.now()
            for attempt in range(1, ATTEMPTS + 1):
                if pwm is not None and self._halted:
                    failure = "halted"  # an emergency shutdown ran: nothing may be lit now
                    break
                if attempt > 1:
                    self.clock.sleep(QUIET_S)
                purged += self._discard()
                sent_at = self.clock.now()
                verdict, reason, status = self._attempt(frame, received)
                if pwm is not None and self._halted:
                    verdict, failure = None, "halted"  # it ran while this ON was in flight
                    break
                if verdict is Verdict.ACCEPTED:
                    break
                failure = reason
                if verdict is Verdict.REJECTED:
                    break  # the device answered; asking again changes nothing
                log.debug("LED %d: no reply (%s), attempt %d", led_id, reason, attempt)
            accepted = verdict is Verdict.ACCEPTED
            acked_at = self.clock.now() if accepted else None
            acked_at_utc = self.clock.utc() if accepted else None
            if attempt > 1:
                self.clock.sleep(QUIET_S)
                purged += self._discard()  # a late duplicate must not answer the next command
            self._last_command_end = self.clock.now()
            transaction = Transaction(
                led_id=led_id,
                pwm=pwm,
                sent=frame,
                received=bytes(received[-128:]),
                purged=bytes(purged),
                attempts=attempt,
                failure=failure,
                accepted=accepted,
                started_at=started,
                acked_at=acked_at,
                acked_at_utc=acked_at_utc,
                ack_delay_s=None if acked_at is None else acked_at - sent_at,
                duration_s=(self._last_command_end if acked_at is None else acked_at) - started,
            )
            self._transactions.append(transaction)
            if accepted:
                self._states[led_id] = LedState("on", pwm) if pwm else LedState("off")
            else:
                self._states[led_id] = LedState("unknown")
        log.debug("LED %d: sent %s, received %s", led_id, frame.hex(" "), received.hex(" "))
        if verdict is Verdict.REJECTED:
            raise LedRejectedError(
                f"the light source refused the command for LED {led_id} (status {status})",
                details={"led_id": led_id, "status": status},
            )
        if not accepted:
            raise LedNoAckError(
                f"LED {led_id}: no valid reply after {attempt} attempts ({failure})",
                hint="the LED state is unknown; check the USB1 cable",
                details={"led_id": led_id, "attempts": attempt, "reason": failure},
            )
        return transaction
