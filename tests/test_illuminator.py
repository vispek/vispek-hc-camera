# SPDX-License-Identifier: Apache-2.0
"""Light-source driver against the simulated device: retries, safety, discovery."""

from __future__ import annotations

import errno
import itertools
import sys
import time
from types import SimpleNamespace
from typing import Any

import pytest
import serial

from vispek_hc import illuminator, safety
from vispek_hc.errors import (
    AmbiguousDeviceError,
    ConfigRejectedError,
    DeviceNotFoundError,
    InvalidStateError,
    LedNoAckError,
    LedRejectedError,
    LedShutdownUnconfirmedError,
    NoProtocolReplyError,
    PermissionDeniedError,
    PortBusyError,
    UVNotAllowedError,
)
from vispek_hc.illuminator import Illuminator
from vispek_hc.protocol import Command, Function, encode_off, encode_on, encode_reply
from vispek_hc.sim import SimLightSource, SimReply, VirtualClock

LOST = SimReply(lost=True)
ALL = list(range(1, 18))


def make(
    *, connect: bool = True, allow_uv: bool = False, ack_timeout_s: float = 0.2, **device: Any
) -> tuple[Illuminator, SimLightSource, VirtualClock]:
    """A driver on a simulated device; connected (every LED confirmed off) unless told not to."""
    clock = VirtualClock()
    sim = SimLightSource(clock, **device)
    light = Illuminator(sim, clock=clock, allow_uv=allow_uv, ack_timeout_s=ack_timeout_s)
    if connect:
        light.connect(baud=115200)
        sim.log.clear()
        sim.raw_writes.clear()
    return light, sim, clock


def commands(sim: SimLightSource) -> list[Command]:
    return [command for _, command in sim.log]


# --- normal operation ------------------------------------------------------------------


def test_on_and_off_update_device_state_and_log() -> None:
    light, device, _ = make()
    on = light.on(5, 24)
    assert device.lit == {5: 24}
    assert light.states[5].state == "on"
    assert light.states[5].pwm == 24
    assert (on.led_id, on.pwm, on.attempts, on.accepted, on.failure) == (5, 24, 1, True, None)
    assert on.sent == encode_on(5, 24)
    assert on.received == encode_reply(Function.ON)
    assert on.acked_at is not None
    assert on.acked_at_utc is not None

    off = light.off(5)
    assert device.lit == {}
    assert light.states[5].state == "off"
    assert off.pwm is None
    assert list(light.transactions)[-2:] == [on, off]
    assert commands(device) == [Command(Function.ON, 5, 24), Command(Function.OFF, 5, None)]


def test_led_state_is_unknown_until_connected() -> None:
    light, _, _ = make(connect=False)
    assert {state.state for state in light.states.values()} == {"unknown"}
    assert sorted(light.states) == ALL
    light.connect(baud=115200)
    assert {state.state for state in light.states.values()} == {"off"}


def test_reply_delay_is_measured() -> None:
    light, _, _ = make(reply_delay_s=0.012)
    tx = light.on(5, 24)
    assert tx.duration_s == pytest.approx(0.012)
    assert tx.ack_delay_s == pytest.approx(0.012)


def test_commands_are_at_least_10_ms_apart() -> None:
    light, device, _ = make(reply_delay_s=0.004)
    light.on(5, 24)
    light.off(5)
    (first_at, _), (second_at, _) = device.log
    assert second_at - (first_at + 0.004) >= 0.010 - 1e-9


def test_lighting_a_second_led_switches_the_first_one_off() -> None:
    light, device, _ = make()
    light.on(5, 24)
    light.on(6, 100)
    assert device.lit == {6: 100}
    assert commands(device) == [
        Command(Function.ON, 5, 24),
        Command(Function.OFF, 5, None),
        Command(Function.ON, 6, 100),
    ]


def test_several_leds_stay_lit_only_on_request() -> None:
    light, device, _ = make()
    light.on(5, 24)
    light.on(6, 100, exclusive=False)
    assert device.lit == {5: 24, 6: 100}


def test_lighting_an_led_first_switches_off_everything_not_known_to_be_off() -> None:
    light, device, _ = make(connect=False)  # nothing is known about any LED
    light.on(5, 24)
    assert commands(device) == [
        *[Command(Function.OFF, n, None) for n in ALL if n != 5],
        Command(Function.ON, 5, 24),
    ]


def test_transaction_log_keeps_the_latest_200() -> None:
    light, _, _ = make()
    for _ in range(105):
        light.on(5, 24)
        light.off(5)
    assert len(light.transactions) == 200


@pytest.mark.parametrize("timeout", [0.05, 2.5])
def test_reply_timeout_must_be_between_100_and_2000_ms(timeout: float) -> None:
    with pytest.raises(ConfigRejectedError):
        make(ack_timeout_s=timeout)


# --- lost, late, rejected and echoed replies -------------------------------------------


def test_lost_reply_is_retried_and_succeeds() -> None:
    light, device, _ = make()
    device.script(LOST)
    tx = light.on(5, 24)
    assert (tx.attempts, tx.accepted, tx.failure) == (2, True, "timeout")
    assert commands(device) == [Command(Function.ON, 5, 24)] * 2
    assert light.states[5].state == "on"


def test_three_lost_replies_end_with_an_off_and_report_what_is_known() -> None:
    light, device, clock = make()
    device.script(LOST, LOST, LOST)
    started = clock.now()
    with pytest.raises(LedNoAckError) as caught:
        light.on(5, 24)
    assert caught.value.details == {
        "led_id": 5,
        "attempts": 3,
        "reason": "timeout",
        "off_confirmed": True,
    }
    # The device did carry out the ON; the driver must not leave it lit.
    assert commands(device) == [*[Command(Function.ON, 5, 24)] * 3, Command(Function.OFF, 5, None)]
    assert device.lit == {}
    assert light.states[5].state == "off"
    # 10 ms gap, 3 timeouts of 200 ms, a 60 ms pause before each resend and one after the
    # last, then the OFF: 10 ms gap and its 15 ms reply.
    assert clock.now() - started == pytest.approx(0.010 + 3 * 0.2 + 3 * 0.06 + 0.010 + 0.015)
    failed = list(light.transactions)[-2]
    assert (failed.accepted, failed.attempts, failed.failure) == (False, 3, "timeout")


def test_led_state_stays_unknown_when_the_follow_up_off_is_lost_too() -> None:
    light, device, _ = make()
    device.script(*[LOST] * 6)
    with pytest.raises(LedNoAckError) as caught:
        light.on(5, 24)
    assert caught.value.details["off_confirmed"] is False
    assert light.states[5].state == "unknown"


def test_ack_timeout_is_configurable() -> None:
    light, device, clock = make(ack_timeout_s=0.5)
    device.script(LOST, LOST, LOST)
    started = clock.now()
    with pytest.raises(LedNoAckError):
        light.on(5, 24)
    assert clock.now() - started == pytest.approx(0.010 + 3 * 0.5 + 3 * 0.06 + 0.025)


def test_late_reply_is_purged_after_a_retried_command() -> None:
    light, device, _ = make()
    device.script(SimReply(delay_s=0.29))  # arrives after the retry already succeeded
    tx = light.on(5, 24)
    assert tx.attempts == 2
    assert tx.purged == encode_reply(Function.ON)
    assert device.unread == b""


def test_late_reply_arriving_before_the_resend_is_purged_too() -> None:
    light, device, _ = make()
    device.script(SimReply(delay_s=0.23))  # arrives during the pause before the resend
    tx = light.on(5, 24)
    assert tx.attempts == 2
    assert tx.purged == encode_reply(Function.ON)
    assert tx.received == encode_reply(Function.ON)


def test_stale_reply_waiting_before_a_command_is_not_credited_to_it() -> None:
    light, device, _ = make()
    device.inject(encode_reply(Function.ON))  # left over from who knows what
    device.script(LOST, LOST, LOST, SimReply())
    with pytest.raises(LedNoAckError):
        light.on(5, 24)
    failed = list(light.transactions)[-2]
    assert failed.purged == encode_reply(Function.ON)
    assert failed.accepted is False


def test_rejection_is_not_retried() -> None:
    light, device, _ = make()
    device.script(SimReply(status=1))
    with pytest.raises(LedRejectedError) as caught:
        light.on(5, 24)
    assert caught.value.details == {"led_id": 5, "status": 1, "off_confirmed": True}
    assert commands(device) == [Command(Function.ON, 5, 24), Command(Function.OFF, 5, None)]


def test_unknown_status_code_is_a_rejection() -> None:
    light, device, _ = make()
    device.script(SimReply(status=0x7F))
    with pytest.raises(LedRejectedError) as caught:
        light.on(5, 24)
    assert caught.value.details["status"] == 0x7F


def test_echoed_commands_are_ignored() -> None:
    light, device, _ = make(echo=True)
    assert light.on(5, 24).attempts == 1
    assert light.off(5).attempts == 1
    assert device.lit == {}


def test_reply_for_the_other_function_is_ignored() -> None:
    light, device, _ = make()
    device.script(SimReply(prefix=encode_reply(Function.OFF)))
    tx = light.on(5, 24)
    assert (tx.attempts, tx.accepted) == (1, True)


@pytest.mark.parametrize(
    ("prefix", "reason"),
    [
        (encode_reply(Function.OFF), "wrong_function"),
        (bytes.fromhex("3d 01 01 00 2a 0d 0a"), "framing_or_crc"),
        (encode_reply(Function.ON)[:4], "incomplete"),
        (b"", "timeout"),
    ],
)
def test_failure_reason_says_what_was_seen_on_the_wire(prefix: bytes, reason: str) -> None:
    light, device, _ = make()
    answer = SimReply(lost=True, prefix=prefix)
    device.script(answer, answer, answer)
    with pytest.raises(LedNoAckError) as caught:
        light.on(5, 24)
    assert caught.value.details["reason"] == reason


def test_garbage_before_the_reply_is_skipped() -> None:
    light, device, _ = make()
    device.script(SimReply(prefix=b"\x00\x3d\x01\xff"))
    assert light.on(5, 24).accepted


def test_the_hint_for_an_led_that_may_be_lit_names_every_cable() -> None:
    # Measured: with only the camera cable left, an LED stays lit. Pulling the two cables
    # of the light source is not enough.
    hint = illuminator.UNPLUG_HINT
    assert "every cable" in hint
    assert "camera" in hint
    assert "both" not in hint


def test_unplugged_port_is_reported_as_an_io_failure() -> None:
    light, device, _ = make()
    device.unplugged = True
    with pytest.raises(LedNoAckError) as caught:
        light.on(5, 24)
    assert caught.value.details["reason"] == "io"
    assert caught.value.details["off_confirmed"] is False
    assert light.states[5].state == "unknown"


def test_ack_statistics_summarise_delay_and_loss() -> None:
    light, device, _ = make(reply_delay_s=0.015)
    device.script(LOST)
    light.on(5, 24)
    light.off(5)
    stats = light.ack_stats()
    assert stats["commands"] == 17 + 2
    assert stats["retried"] == 1
    assert stats["loss_rate"] == pytest.approx(1 / 19)
    assert stats["delay_ms_p50"] == 15.0
    assert stats["delay_ms_p99"] == 15.0, "rounded for reading"


def test_ack_statistics_before_any_command() -> None:
    light, _, _ = make(connect=False)
    assert light.ack_stats() == {
        "commands": 0,
        "retried": 0,
        "loss_rate": None,
        "delay_ms_p50": None,
        "delay_ms_p99": None,
    }


# --- safety ----------------------------------------------------------------------------


@pytest.mark.parametrize("led_id", [1, 2, 3, 4])
def test_uv_leds_are_refused_without_permission_before_anything_is_sent(led_id: int) -> None:
    light, device, _ = make()
    with pytest.raises(UVNotAllowedError) as caught:
        light.on(led_id, 100)
    assert caught.value.details["led_ids"] == [led_id]
    assert device.raw_writes == []


def test_uv_leds_work_with_permission() -> None:
    light, device, _ = make(allow_uv=True)
    light.on(1, 100)
    assert device.lit == {1: 100}
    light.off(1)


@pytest.mark.parametrize(("led_id", "pwm"), [(0, 10), (18, 10), (5, -1), (5, 1024)])
def test_out_of_range_requests_are_refused_before_anything_is_sent(led_id: int, pwm: int) -> None:
    light, device, _ = make(allow_uv=True)
    with pytest.raises(ConfigRejectedError):
        light.on(led_id, pwm)
    assert device.raw_writes == []


def test_off_that_is_not_confirmed_is_reported_as_unconfirmed() -> None:
    light, device, _ = make()
    light.on(5, 24)
    device.script(LOST, LOST, LOST)
    with pytest.raises(LedShutdownUnconfirmedError) as caught:
        light.off(5)
    assert caught.value.details == {"led_ids": [5]}
    assert light.states[5].state == "unknown"


def test_all_off_commands_every_led_and_reports_the_unconfirmed_ones() -> None:
    light, device, _ = make()
    device.script(*[SimReply()] * 6, LOST, LOST, LOST)  # LED 7 never answers
    with pytest.raises(LedShutdownUnconfirmedError) as caught:
        light.all_off()
    assert caught.value.details == {"led_ids": [7]}
    assert {command.led_id for command in commands(device)} == set(ALL)
    assert all(command.function is Function.OFF for command in commands(device))
    assert light.states[7].state == "unknown"
    assert light.states[8].state == "off"


def test_leaving_the_context_switches_everything_off_and_closes() -> None:
    light, device, _ = make()
    with light:
        light.on(5, 24)
    assert device.lit == {}
    assert device.closed
    assert [command.led_id for command in commands(device)[-17:]] == ALL


def test_leaving_the_context_after_an_error_still_switches_everything_off() -> None:
    light, device, _ = make()
    with pytest.raises(RuntimeError), light:
        light.on(5, 24)
        raise RuntimeError("boom")
    assert device.lit == {}
    assert device.closed


def test_port_is_closed_even_when_the_final_all_off_is_unconfirmed() -> None:
    light, device, _ = make()
    with pytest.raises(LedShutdownUnconfirmedError), light:
        device.script(LOST, LOST, LOST)
    assert device.closed


def test_closing_with_an_led_lit_switches_it_off_first() -> None:
    light, device, _ = make(allow_uv=True)
    light.on(1, 458)
    light.close()
    assert device.lit == {}
    assert device.closed
    assert light.states[1].state == "off"


def test_closing_reports_an_led_whose_last_off_was_not_confirmed() -> None:
    # No reply to the last OFF: the LED may still be lit behind a closed port. Returning
    # quietly made a caller show it as off.
    light, device, _ = make()
    light.on(9, 36)
    device.script(*[SimReply()] * 8, LOST)  # the final all-off: LEDs 1-8 answer, LED 9 does not
    with pytest.raises(LedShutdownUnconfirmedError) as caught:
        light.close()
    assert caught.value.details == {"led_ids": [9]}
    assert "unplug every cable" in (caught.value.hint or "")
    assert device.closed, "the port is released first, then the caller is told"
    assert light.states[9].state == "unknown"
    with pytest.raises(InvalidStateError, match="closed"):
        light.on(9, 36)


def test_closing_with_leds_in_unknown_state_switches_everything_off() -> None:
    light, device, _ = make(connect=False)
    light.close()
    assert commands(device) == [Command(Function.OFF, n, None) for n in ALL]


def test_a_closed_light_refuses_commands_instead_of_touching_the_port() -> None:
    light, device, _ = make()
    light.close()
    for command in (lambda: light.on(5, 24), lambda: light.off(5), light.all_off):
        with pytest.raises(InvalidStateError, match="closed"):
            command()
    assert device.raw_writes == []


def test_closing_while_another_thread_lights_an_led_leaves_nothing_lit() -> None:
    # The ON is on the wire when close() starts: its LED must not be left lit behind a
    # closed port.
    import threading

    light, device, _ = make()
    sending, go = threading.Event(), threading.Event()

    def hold() -> None:
        sending.set()
        go.wait(5)

    failures: list[BaseException] = []

    def switch_on() -> None:
        device.on_write = hold
        try:
            light.on(9, 36)
        except BaseException as error:  # refused or cut short: both are fine
            failures.append(error)

    lighting = threading.Thread(target=switch_on)
    lighting.start()
    assert sending.wait(5)
    device.on_write = None
    closing = threading.Thread(target=light.close)
    closing.start()
    time.sleep(0.05)  # close() is now waiting for the command in flight
    go.set()
    lighting.join(5)
    closing.join(5)
    assert not lighting.is_alive()
    assert not closing.is_alive()
    assert device.lit == {}
    assert device.closed
    with pytest.raises(InvalidStateError):
        light.on(9, 36)
    assert device.lit == {}


def test_closing_when_everything_is_off_sends_nothing() -> None:
    light, device, _ = make()
    light.close()
    assert device.raw_writes == []
    assert device.closed


def test_a_light_is_protected_from_creation_until_closed() -> None:
    light, _, _ = make(connect=False)
    assert light in safety._registry.lights
    light.close()
    assert light not in safety._registry.lights


# --- emergency all-off -----------------------------------------------------------------


def test_emergency_off_does_not_wait_for_a_command_in_progress() -> None:
    light, device, _ = make()
    unconfirmed: list[list[int]] = []

    def interrupt_once() -> None:
        device.on_write = None
        unconfirmed.append(light.emergency_off())  # as a signal handler would, mid-command

    device.on_write = interrupt_once
    with pytest.raises(LedNoAckError) as caught:
        light.on(5, 24)  # would deadlock here if the emergency path took the command lock
    assert unconfirmed == [[]]
    assert caught.value.details["reason"] == "halted", "no resend behind the shutdown"
    assert device.lit == {}, "the interrupted ON was followed by an OFF"


def test_emergency_off_keeps_the_command_gap_and_drops_replies_in_flight() -> None:
    light, device, _ = make(reply_delay_s=0.001)  # so that only the gap spaces the commands
    device.inject(encode_reply(Function.OFF))  # would otherwise be credited to LED 1
    device.script(LOST)
    assert light.emergency_off() == [1]
    times = [at for at, _ in device.log]
    assert commands(device) == [Command(Function.OFF, n, None) for n in ALL]
    assert min(b - a for a, b in itertools.pairwise(times)) >= 0.010 - 1e-9


def test_emergency_off_is_honest_about_what_was_confirmed() -> None:
    light, device, clock = make()
    light.on(5, 24, exclusive=False)
    device.script(*[SimReply()] * 2, LOST, SimReply(status=1))
    started = clock.now()
    assert light.emergency_off() == [3, 4]
    assert clock.now() - started < 17 * (0.1 + 0.01)
    assert light.states[3].state == "unknown"
    assert light.states[4].state == "unknown", "a refusal is not a confirmation"
    assert light.states[5].state == "off"
    device.unplugged = True
    assert light.emergency_off() == ALL, "never raises"
    assert {state.state for state in light.states.values()} == {"unknown"}


def test_emergency_off_does_not_take_a_refusal_or_a_late_reply_for_a_confirmation() -> None:
    light, device, _ = make()
    light.on(5, 24, exclusive=False)
    # LED 1: its reply comes 150 ms late, after the wait for it ended. Without a pause it
    # would be read as LED 2's confirmation.
    device.script(SimReply(delay_s=0.150), LOST)
    assert light.emergency_off() == [1, 2]
    assert light.states[2].state == "unknown"


def test_nothing_can_be_lit_after_an_emergency_until_a_confirmed_all_off() -> None:
    light, device, _ = make()
    light.emergency_off()
    with pytest.raises(InvalidStateError):
        light.on(5, 24)
    assert device.lit == {}
    device.script(LOST, LOST, LOST)
    with pytest.raises(LedShutdownUnconfirmedError):
        light.all_off()
    with pytest.raises(InvalidStateError):
        light.on(5, 24)  # a partly confirmed all-off does not lift the latch
    light.all_off()
    light.on(5, 24)
    assert device.lit == {5: 24}


# --- watchdog --------------------------------------------------------------------------


def wait_until(condition: Any, seconds: float = 5.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.005)
    return False


def test_watchdog_switches_a_held_led_off() -> None:
    light, device, _ = make()
    light.on(5, 24, max_on_s=0.05)
    assert device.lit == {5: 24}
    assert wait_until(lambda: device.lit == {} and light.states[5].state == "off")


def test_uv_leds_get_the_ten_second_watchdog_by_default() -> None:
    light, _, _ = make(allow_uv=True)
    light.on(1, 100)
    watchdog = light._watchdogs[1]
    assert watchdog.interval == 10.0
    assert watchdog.daemon
    light.off(1)
    assert watchdog.finished.is_set(), "cancelled once the OFF is confirmed"
    assert light._watchdogs == {}


def test_visible_leds_have_no_watchdog_unless_asked() -> None:
    light, _, _ = make()
    light.on(5, 24)
    assert light._watchdogs == {}
    light.off(5)


def test_uv_watchdog_is_armed_even_when_the_on_is_never_confirmed() -> None:
    light, device, _ = make(allow_uv=True)
    device.script(*[LOST] * 6)  # ON carried out but unconfirmed; the follow-up OFF lost too
    with pytest.raises(LedNoAckError):
        light.on(1, 458)
    assert device.lit == {}
    assert 1 in light._watchdogs, "still armed: the OFF was not confirmed"
    light.close()


def test_a_failing_watchdog_does_not_crash_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    light, _, _ = make()
    fired: list[int] = []

    def broken_off(led_id: int) -> None:
        fired.append(led_id)
        raise TypeError("port already closed")

    monkeypatch.setattr(light, "off", broken_off)
    light.on(5, 24, max_on_s=0.01)
    assert wait_until(lambda: fired == [5])
    light._watchdogs[5].join()  # an unhandled exception in the thread would fail this test


def test_unconfirmed_off_keeps_the_watchdog_armed() -> None:
    light, device, _ = make(allow_uv=True)
    light.on(1, 100)
    device.unplugged = True
    with pytest.raises(LedShutdownUnconfirmedError):
        light.off(1)
    assert not light._watchdogs[1].finished.is_set()
    with pytest.raises(LedShutdownUnconfirmedError):
        light.close()
    assert light._watchdogs == {}


# --- connecting ------------------------------------------------------------------------


def test_connect_with_a_known_baud_rate_switches_everything_off() -> None:
    light, device, _ = make(connect=False)
    light.connect(baud=115200)
    assert device.link_baud == 115200
    assert commands(device) == [Command(Function.OFF, n, None) for n in ALL]
    assert {state.state for state in light.states.values()} == {"off"}


def test_baud_probe_sends_only_off_1_three_times_per_rate() -> None:
    light, device, _ = make(connect=False, baud=9600)
    light.connect()
    assert device.link_baud == 9600
    assert light.baud == 9600
    probe = device.raw_writes[:4]
    assert probe == [(115200, encode_off(1))] * 3 + [(9600, encode_off(1))]
    assert len(device.raw_writes) == 4 + 17
    assert light.ack_stats()["retried"] == 0, "silence at a wrong rate is not counted as loss"


def test_baud_probe_order_and_failure() -> None:
    light, device, _ = make(connect=False, baud=1200)
    with pytest.raises(NoProtocolReplyError) as caught:
        light.connect()
    assert caught.value.details == {"tried": [115200, 9600, 57600, 38400, 19200]}
    assert [baud for baud, _ in device.raw_writes] == [
        rate for rate in (115200, 9600, 57600, 38400, 19200) for _ in range(3)
    ]
    assert {frame for _, frame in device.raw_writes} == {encode_off(1)}


def test_a_refusal_during_the_probe_still_settles_the_baud_rate() -> None:
    light, device, _ = make(connect=False)
    device.script(SimReply(status=1))
    light.connect()
    assert light.baud == 115200


def test_connect_fails_when_the_initial_all_off_is_unconfirmed() -> None:
    light, device, _ = make(connect=False)
    device.script(*[SimReply()] * 4, LOST, LOST, LOST)
    with pytest.raises(LedShutdownUnconfirmedError):
        light.connect(baud=115200)


@pytest.mark.parametrize("baud", [1200, 0, 250000])
def test_unsupported_baud_rate_is_refused(baud: int) -> None:
    light, device, _ = make(connect=False)
    with pytest.raises(ConfigRejectedError):
        light.connect(baud=baud)
    assert device.raw_writes == []


# --- discovery -------------------------------------------------------------------------


def port(device: str, vid: int | None = None) -> Any:
    return SimpleNamespace(device=device, vid=vid, description="n/a")


def test_ports_are_matched_by_the_usb_vendor_id() -> None:
    ports = [port("COM3", 0x1A86), port("COM4", 0x0403), port("COM1")]
    assert illuminator.candidate_ports(ports, platform="win32") == ["COM3"]
    mixed = [port("/dev/cu.usbserial-1", 0x0403), port("/dev/cu.usbserial-2", 0x1A86)]
    assert illuminator.candidate_ports(mixed, platform="darwin") == ["/dev/cu.usbserial-2"]


def test_ports_without_usb_information_fall_back_to_name_patterns() -> None:
    mac = [
        port("/dev/cu.usbserial-1120"),
        port("/dev/cu.wchusbserial1120"),
        port("/dev/cu.Bluetooth-Incoming-Port"),
        port("/dev/cu.debug-console"),
        port("/dev/cu.usbmodem14101"),
    ]
    assert illuminator.candidate_ports(mac, platform="darwin") == [
        "/dev/cu.usbserial-1120",
        "/dev/cu.wchusbserial1120",
    ]
    linux = [port("/dev/ttyUSB0"), port("/dev/ttyACM1"), port("/dev/ttyS0")]
    assert illuminator.candidate_ports(linux, platform="linux") == ["/dev/ttyUSB0", "/dev/ttyACM1"]
    assert illuminator.candidate_ports([port("COM1")], platform="win32") == []


def test_a_named_port_wins_over_discovery() -> None:
    assert illuminator.choose_port("/dev/x", ["a", "b"]) == "/dev/x"


def test_one_candidate_is_used() -> None:
    assert illuminator.choose_port(None, ["/dev/cu.usbserial-1120"]) == "/dev/cu.usbserial-1120"


def test_no_candidate_is_device_not_found() -> None:
    with pytest.raises(DeviceNotFoundError) as caught:
        illuminator.choose_port(None, [])
    assert "--port" in (caught.value.hint or "")


def test_several_candidates_are_never_guessed() -> None:
    with pytest.raises(AmbiguousDeviceError) as caught:
        illuminator.choose_port(None, ["a", "b"])
    assert caught.value.details == {"candidates": ["a", "b"], "option": "--port"}


# --- the real serial link, against a fake pyserial -------------------------------------


class FakeSerial:
    def __init__(self, port: str, baudrate: int, **options: Any) -> None:
        self.port, self.baudrate, self.options = port, baudrate, options
        self.reconfigured = 0
        self.incoming = bytearray()
        self.written = bytearray()
        self.is_open = True

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    @property
    def timeout(self) -> float:
        return float(self.options["timeout"])

    @timeout.setter
    def timeout(self, value: float) -> None:
        self.reconfigured += 1  # the real setter reconfigures the port

    def fileno(self) -> int:
        return 10**6  # not an open descriptor

    def read(self, size: int) -> bytes:
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data: bytes) -> int:
        self.written += data
        return len(data)

    def flush(self) -> None:
        pass

    def close(self) -> None:
        self.is_open = False


def test_serial_link_opens_the_port_exclusively_at_8n1(monkeypatch: pytest.MonkeyPatch) -> None:
    locked: list[int] = []
    if sys.platform != "win32":
        monkeypatch.setattr(illuminator.fcntl, "ioctl", lambda fd, request: locked.append(request))
    monkeypatch.setattr(serial, "Serial", FakeSerial)
    link = illuminator.open_link("/dev/x", 115200)
    port_object = link.serial
    assert (port_object.port, port_object.baudrate) == ("/dev/x", 115200)
    assert port_object.options == {"timeout": 0.005, "write_timeout": 1.0, "exclusive": True}
    if sys.platform != "win32":
        assert locked == [illuminator.termios.TIOCEXCL], "also refuses plain open() by others"

    port_object.incoming += b"abc"
    assert link.read(0.2) == b"abc"
    started = time.monotonic()
    assert link.read(0.05) == b""
    assert time.monotonic() - started >= 0.05
    assert port_object.reconfigured == 0, "reading must not reconfigure the port"
    port_object.incoming += b"stale"
    assert link.discard_input() == b"stale"
    link.write(b"\x3d")
    assert bytes(port_object.written) == b"\x3d"
    link.set_baud(9600)
    assert port_object.baudrate == 9600
    link.close()
    assert not port_object.is_open


WINDOWS_BUSY = "could not open port 'COM3': PermissionError(13, 'Access is denied.', None, 5)"


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            serial.SerialException("Could not exclusively lock port /dev/x: [Errno 35]"),
            PortBusyError,
        ),
        (serial.SerialException(errno.EBUSY, "could not open port: Resource busy"), PortBusyError),
        (serial.SerialException(WINDOWS_BUSY), PortBusyError),
        (serial.SerialException(errno.EACCES, "Permission denied"), PermissionDeniedError),
        (serial.SerialException(errno.ENOENT, "No such file or directory"), DeviceNotFoundError),
        (FileNotFoundError(errno.ENOENT, "No such file"), DeviceNotFoundError),
        (serial.SerialException("something else"), DeviceNotFoundError),
    ],
)
def test_open_failures_map_to_device_errors(
    error: Exception, expected: type[Exception], monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise error

    monkeypatch.setattr(serial, "Serial", fail)
    with pytest.raises(expected) as caught:
        illuminator.open_link("/dev/x", 115200)
    assert caught.value.details["port"] == "/dev/x"  # type: ignore[attr-defined]


class AnsweringSerial(FakeSerial):
    """A fake pyserial port with a well-behaved light source behind it."""

    def write(self, data: bytes) -> int:
        self.incoming += encode_reply(Function(data[1]))
        return super().write(data)


@pytest.fixture
def opened(monkeypatch: pytest.MonkeyPatch) -> list[FakeSerial]:
    ports: list[FakeSerial] = []

    def answering(port_name: str, baudrate: int, **options: Any) -> FakeSerial:
        ports.append(AnsweringSerial(port_name, baudrate, **options))
        return ports[-1]

    monkeypatch.setattr(serial, "Serial", answering)
    monkeypatch.setattr(
        serial.tools.list_ports, "comports", lambda: [port("/dev/cu.usbserial-7", 0x1A86)]
    )
    return ports


def test_open_finds_the_port_probes_and_switches_everything_off(opened: list[FakeSerial]) -> None:
    with Illuminator.open() as light:
        assert (light.port, light.baud) == ("/dev/cu.usbserial-7", 115200)
        assert {state.state for state in light.states.values()} == {"off"}
    (used,) = opened
    assert not used.is_open
    frames = [bytes(used.written[i : i + 7]) for i in range(0, len(used.written), 7)]
    assert frames[0] == encode_off(1), "probe"
    assert frames[1:18] == [encode_off(n) for n in ALL], "all-off on connect"
    assert frames[18:] == [encode_off(n) for n in ALL], "all-off on exit"


def test_open_refuses_a_bad_baud_rate_before_touching_the_port(opened: list[FakeSerial]) -> None:
    with pytest.raises(ConfigRejectedError):
        Illuminator.open(baud=1200)
    assert opened == []


def test_open_refuses_a_bad_reply_timeout_before_touching_the_port(
    opened: list[FakeSerial],
) -> None:
    with pytest.raises(ConfigRejectedError):
        Illuminator.open(ack_timeout_s=5.0)
    assert opened == []


def test_open_closes_the_port_when_connecting_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    silent: list[FakeSerial] = []

    def no_answer(port_name: str, baudrate: int, **options: Any) -> FakeSerial:
        silent.append(FakeSerial(port_name, baudrate, **options))
        return silent[-1]

    monkeypatch.setattr(serial, "Serial", no_answer)
    monkeypatch.setattr(illuminator, "QUIET_S", 0.0)
    monkeypatch.setattr(illuminator, "ACK_TIMEOUT_RANGE_S", (0.0, 2.0))
    monkeypatch.setattr(illuminator, "EMERGENCY_WAIT_S", 0.001)
    lights_before = set(safety._registry.lights)
    with pytest.raises(LedShutdownUnconfirmedError):
        Illuminator.open("/dev/x", baud=115200, ack_timeout_s=0.001)
    assert not silent[0].is_open
    assert set(safety._registry.lights) == lights_before, "released again"
