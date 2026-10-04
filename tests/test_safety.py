# SPDX-License-Identifier: Apache-2.0
"""UV gating of channel plans and the process-level all-off."""

from __future__ import annotations

import logging
import os
import signal
import threading
from typing import Any

import pytest

from vispek_hc import safety
from vispek_hc.errors import ConfigRejectedError, UVNotAllowedError
from vispek_hc.models import HC1500


def test_default_channels_pass_without_uv_permission() -> None:
    plan = safety.check_channels(HC1500, HC1500.default_channels, allow_uv=False)
    assert plan == list(range(5, 18))


def test_a_plan_with_uv_is_refused_as_a_whole_before_anything_runs() -> None:
    with pytest.raises(UVNotAllowedError) as caught:
        safety.check_channels(HC1500, [5, 6, 7, 8, 1, 3], allow_uv=False)
    assert caught.value.details == {"led_ids": [1, 3]}
    assert "LED 1 (255 nm, UV-C)" in caught.value.message
    assert "--allow-uv" in (caught.value.hint or "")


def test_uv_plan_passes_with_permission() -> None:
    assert safety.check_channels(HC1500, range(1, 18), allow_uv=True) == list(range(1, 18))


@pytest.mark.parametrize("plan", [[5, 5], [0], [18], [5, 99]])
def test_unknown_or_repeated_leds_are_config_errors(plan: list[int]) -> None:
    with pytest.raises(ConfigRejectedError):
        safety.check_channels(HC1500, plan, allow_uv=True)


class Light:
    def __init__(self) -> None:
        self.emergencies = 0

    def emergency_off(self) -> list[int]:
        self.emergencies += 1
        return []


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """An isolated registry, with handler installation recorded instead of performed."""
    installed: dict[str, Any] = {"signals": {}, "atexit": [], "ignored": set()}
    monkeypatch.setattr(safety, "_registry", safety._Registry())

    def fake_signal(signum: int, handler: Any) -> Any:
        installed["signals"][signum] = handler
        return signal.default_int_handler if signum == signal.SIGINT else signal.SIG_DFL

    def fake_getsignal(signum: int) -> Any:
        return signal.SIG_IGN if signum in installed["ignored"] else signal.SIG_DFL

    monkeypatch.setattr(signal, "signal", fake_signal)
    monkeypatch.setattr(signal, "getsignal", fake_getsignal)
    monkeypatch.setattr(safety.atexit, "register", installed["atexit"].append)
    return installed


def expected_signals() -> set[int]:
    names = ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK")
    return {getattr(signal, name) for name in names if hasattr(signal, name)}


def test_protect_installs_exit_and_signal_handlers_once(fresh: dict[str, Any]) -> None:
    first, second = Light(), Light()
    safety.protect(first)
    safety.protect(second)
    assert fresh["atexit"] == [safety.all_off_now]
    assert set(fresh["signals"]) == expected_signals()


def test_all_off_now_reaches_every_protected_light(fresh: dict[str, Any]) -> None:
    first, second = Light(), Light()
    safety.protect(first)
    safety.protect(second)
    safety.release(second)
    safety.all_off_now()
    assert (first.emergencies, second.emergencies) == (1, 0)


def test_a_light_nobody_closed_is_still_switched_off_at_exit(fresh: dict[str, Any]) -> None:
    switched: list[int] = []

    class Forgotten(Light):
        def emergency_off(self) -> list[int]:
            switched.append(1)
            return []

    safety.protect(Forgotten())  # no reference kept: the registry must keep it alive
    safety.all_off_now()
    assert switched == [1]


def test_all_off_now_survives_a_light_that_fails(fresh: dict[str, Any]) -> None:
    class Broken(Light):
        def emergency_off(self) -> list[int]:
            raise OSError("port is gone")

    working = Light()
    safety.protect(Broken())
    safety.protect(working)
    safety.all_off_now()
    assert working.emergencies == 1


def test_interrupt_switches_off_then_raises_keyboard_interrupt(fresh: dict[str, Any]) -> None:
    light = Light()
    safety.protect(light)
    with pytest.raises(KeyboardInterrupt):
        fresh["signals"][signal.SIGINT](signal.SIGINT, None)
    assert light.emergencies == 1


def test_terminate_switches_off_then_dies_by_the_same_signal(
    fresh: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    light = Light()
    safety.protect(light)
    killed: list[tuple[int, int]] = []
    monkeypatch.setattr(os, "kill", lambda pid, signum: killed.append((pid, signum)))
    fresh["signals"][signal.SIGTERM](signal.SIGTERM, None)
    assert light.emergencies == 1
    assert killed == [(os.getpid(), signal.SIGTERM)]
    assert fresh["signals"][signal.SIGTERM] is signal.SIG_DFL


def test_a_signal_the_process_ignores_stays_ignored(fresh: dict[str, Any]) -> None:
    fresh["ignored"].add(signal.SIGTERM)  # as under nohup for SIGHUP
    safety.protect(Light())
    assert signal.SIGTERM not in fresh["signals"]
    assert signal.SIGINT in fresh["signals"]


def test_opening_from_a_worker_thread_defers_signal_handlers_to_the_main_thread(
    fresh: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="vispek_hc"):
        worker = threading.Thread(target=safety.protect, args=(Light(),))
        worker.start()
        worker.join()
    assert fresh["atexit"] == [safety.all_off_now], "exit handler does not need the main thread"
    assert fresh["signals"] == {}
    assert "main thread" in caplog.text

    safety.protect(Light())
    assert set(fresh["signals"]) == expected_signals()
