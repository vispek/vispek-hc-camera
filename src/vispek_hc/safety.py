# SPDX-License-Identifier: Apache-2.0
"""UV gating and the last line of defence that switches LEDs off when the process ends.

LEDs 1 and 2 emit UV-C. Nothing in this package may light LEDs 1-4 without an explicit
permission, and the check runs before the first LED of a plan is lit.
"""

from __future__ import annotations

import atexit
import logging
import os
import signal
import threading
from collections.abc import Iterable
from dataclasses import dataclass, field
from types import FrameType
from typing import Any, Protocol

from vispek_hc.errors import ConfigRejectedError, UVNotAllowedError
from vispek_hc.models import DeviceModel

log = logging.getLogger(__name__)


def check_channels(model: DeviceModel, led_ids: Iterable[int], *, allow_uv: bool) -> list[int]:
    """Validate a whole channel plan up front and return it as a list.

    Raises before anything is lit: unknown LED numbers and duplicates are configuration
    errors; a UV LED without permission is ``UV_NOT_ALLOWED``.
    """
    plan = list(led_ids)
    leds = [model.led(led_id) for led_id in plan]
    if len(set(plan)) != len(plan):
        raise ConfigRejectedError("an LED appears more than once", details={"led_ids": plan})
    gated = [led for led in leds if led.needs_uv_permission]
    if gated and not allow_uv:
        listing = ", ".join(f"LED {led.led_id} ({led.nm} nm, {led.hazard})" for led in gated)
        raise UVNotAllowedError(
            f"ultraviolet LEDs need explicit permission: {listing}",
            hint="UV-C injures eyes and skin. Read SAFETY.md, shield the light source, "
            "then pass --allow-uv (allow_uv=True in Python)",
            details={"led_ids": [led.led_id for led in gated]},
        )
    return plan


class _SwitchesOff(Protocol):
    def emergency_off(self) -> list[int]: ...


@dataclass
class _Registry:
    """Process-wide by necessity: exit and signal handlers belong to the process."""

    lights: set[Any] = field(default_factory=set)  # strong references, until released
    atexit_installed: bool = False
    signals_installed: bool = False
    warned: bool = False
    previous: dict[int, Any] = field(default_factory=dict)


_registry = _Registry()


def all_off_now() -> None:
    """Switch off every LED of every open light source, without waiting for locks.

    Safe to call from a signal handler or at interpreter exit. Never raises.
    """
    for light in list(_registry.lights):
        try:
            light.emergency_off()
        except Exception:
            log.exception("emergency all-off failed for one light source")


def _on_signal(signum: int, frame: FrameType | None) -> None:
    all_off_now()
    previous = _registry.previous.get(signum)
    if callable(previous):
        previous(signum, frame)  # for SIGINT this raises KeyboardInterrupt as usual
        return
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


def protect(light: _SwitchesOff) -> None:
    """Make sure this light source is switched off when the process exits or is signalled.

    The light stays registered (and alive) until :func:`release`.
    """
    registry = _registry
    registry.lights.add(light)
    if not registry.atexit_installed:
        registry.atexit_installed = True
        atexit.register(all_off_now)
    if registry.signals_installed:
        return
    if threading.current_thread() is not threading.main_thread():
        if not registry.warned:
            registry.warned = True
            log.warning(
                "light source opened outside the main thread: termination signals cannot "
                "switch the LEDs off until one is opened from the main thread"
            )
        return
    registry.signals_installed = True
    for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):  # the last two are per platform
        signum = getattr(signal, name, None)
        if signum is not None and signal.getsignal(signum) is not signal.SIG_IGN:
            registry.previous[signum] = signal.signal(signum, _on_signal)


def release(light: _SwitchesOff) -> None:
    _registry.lights.discard(light)
