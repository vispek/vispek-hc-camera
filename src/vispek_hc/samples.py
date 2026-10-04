# SPDX-License-Identifier: Apache-2.0
"""Captures to try things on without a device, recorded on the simulated HC1500.

The scene is invented: a white board with three patches whose spectra are plausible
shapes, not measurements, under light that is a little uneven. It exists so that
pictures, indices and spectra have something to show, and so that the result of the
whole chain can be checked against a known truth.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
from numpy.typing import NDArray

from vispek_hc.cube import Capture
from vispek_hc.errors import InvalidStateError
from vispek_hc.models import HC1500, DeviceModel
from vispek_hc.session import Session
from vispek_hc.sim import SimulatedRig
from vispek_hc.sweep import ScanSettings, record

SIZE = (128, 72)  # width, height
# Patches as x, y, w, h in pixels, and their reflectance by wavelength (nm, value) anchors.
PATCHES: dict[str, tuple[int, int, int, int]] = {
    "leaf": (14, 12, 36, 28),
    "red": (70, 10, 40, 24),
    "grey": (44, 46, 48, 18),
}
_SPECTRA: dict[str, tuple[tuple[int, ...], tuple[float, ...]]] = {
    # dark in the visible with a little green, bright in the near infrared
    "leaf": (
        (255, 400, 455, 520, 560, 610, 667, 700, 804, 1050),
        (0.04, 0.04, 0.06, 0.12, 0.16, 0.08, 0.05, 0.25, 0.55, 0.50),
    ),
    "red": ((255, 560, 610, 630, 1050), (0.05, 0.05, 0.50, 0.70, 0.70)),
    "grey": ((255, 1050), (0.40, 0.40)),
}
BOARD = 0.9


def scene(model: DeviceModel = HC1500) -> NDArray[np.float64]:
    """Reflectance of the simulated sample, (height, width, LEDs), 1.0 being the white board."""
    width, height = SIZE
    leds = model.led_table.leds
    layers = np.full((height, width, len(leds)), BOARD)
    wavelengths = [led.nm for led in leds]
    for name, (x, y, w, h) in PATCHES.items():
        layers[y : y + h, x : x + w, :] = np.interp(wavelengths, *_SPECTRA[name])
    return layers


def illumination() -> NDArray[np.float64]:
    """How evenly the simulated light falls, (height, width): 1.0 on the left, 0.85 on the
    right. It falls on the sample and on the white reference alike, so dividing by the
    white reference pixel by pixel takes it out."""
    width, height = SIZE
    return np.tile(np.linspace(1.0, 0.85, width), (height, 1))


def _check_replaceable(directory: Path) -> None:
    """A capture left by an earlier simulated run may be replaced; a real one may not."""
    try:
        document = json.loads((directory / "capture.json").read_text("utf-8"))
    except (OSError, ValueError):
        return  # nothing there, or not a capture: the writer decides
    device = document.get("device") if isinstance(document, dict) else None
    if not (isinstance(device, dict) and device.get("simulated") is True):
        raise InvalidStateError(
            f"{directory.name} holds a capture that is not a simulated capture; it is left alone",
            hint="choose another output folder",
        )


def simulated(directory: str | Path) -> tuple[Capture, Capture]:
    """Record the sample scene and its white reference on the simulated device.

    Written to ``sample`` and ``white`` inside ``directory``. Simulated captures left
    there by an earlier call are replaced; anything else in the way is refused.
    """
    directory = Path(directory)
    light = illumination()
    seen = {"sample": scene() * light[:, :, None], "white": light}
    for name in seen:
        _check_replaceable(directory / name)
    captures = []
    for name, role in (("sample", None), ("white", "white")):
        rig = SimulatedRig(size=SIZE, reflectance=seen[name])
        with Session.from_rig(rig) as session:
            session.pair_probe()
            settings = ScanSettings(channels=tuple(range(5, 18)), role=role)  # type: ignore[arg-type]
            captures.append(record(session, directory / name, settings, overwrite=True))
    return captures[0], captures[1]


def example_captures(directory: str | Path) -> tuple[Capture, Capture]:
    """A sample capture and its white reference to try things on: see :func:`simulated`."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    return simulated(directory)
