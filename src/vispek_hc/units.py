# SPDX-License-Identifier: Apache-2.0
"""Per-device profiles kept in the user's configuration directory.

Neither the camera nor the serial adapter has a serial number, so a device is identified
by a label the user chooses (``unit_id``). What was learned about that device lives in
``<VISPEK_HC_HOME>/units/<unit_id>.json`` and is restored the next time it is connected.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from vispek_hc.errors import ConfigRejectedError, PermissionDeniedError, SchemaUnsupportedError

SCHEMA = "vispek-hc.unit/1"
_UNIT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")


def home() -> Path:
    """The configuration directory: ``VISPEK_HC_HOME`` or the platform's usual place."""
    if override := os.environ.get("VISPEK_HC_HOME"):
        return Path(override)
    if sys.platform == "darwin":
        base = Path("~/Library/Application Support")
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or "~/AppData/Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config")
    return (base / "vispek-hc").expanduser()


@dataclass(slots=True)
class UnitProfile:
    unit_id: str
    setup_label: str | None = None  # lens and working distance, as declared by the user
    camera_controls: dict[str, int] = field(default_factory=dict)  # confirmed effective values
    pwm_by_led: dict[int, int] = field(default_factory=dict)
    min_on_ms_by_led: dict[int, int] = field(default_factory=dict)
    pipeline_latency_ms: float | None = None  # ON acknowledged to first brighter frame
    measured_fps: float | None = None


def _path(unit_id: str) -> Path:
    if not _UNIT_ID.fullmatch(unit_id):
        raise ConfigRejectedError(
            f"unit label {unit_id!r} is not usable as a file name",
            hint="use 1-64 letters, digits, '.', '_' or '-', starting with a letter or digit",
            details={"unit_id": unit_id},
        )
    return home() / "units" / f"{unit_id}.json"


def _optional(value: Any, kind: Any) -> Any:
    if value is None or (isinstance(value, kind) and not isinstance(value, bool)):
        return value
    raise TypeError(f"unexpected value {value!r}")


def _by_led(stored: Any) -> dict[int, int]:
    return {int(led_id): _optional(value, int) for led_id, value in stored.items()}


def load_profile(unit_id: str) -> UnitProfile:
    """Read a unit's profile; a unit never saved before gets an empty one.

    Fields this version does not know are ignored.
    """
    path = _path(unit_id)
    if not path.exists():
        return UnitProfile(unit_id=unit_id)
    try:
        stored = json.loads(path.read_text("utf-8"))
        if stored.get("schema") != SCHEMA:
            raise ValueError(f"schema is {stored.get('schema')!r}, expected {SCHEMA!r}")
        return UnitProfile(
            unit_id=unit_id,
            setup_label=_optional(stored.get("setup_label"), str),
            camera_controls={
                str(name): _optional(value, int)
                for name, value in stored.get("camera_controls", {}).items()
            },
            pwm_by_led=_by_led(stored.get("pwm_by_led", {})),
            min_on_ms_by_led=_by_led(stored.get("min_on_ms_by_led", {})),
            pipeline_latency_ms=_optional(stored.get("pipeline_latency_ms"), int | float),
            measured_fps=_optional(stored.get("measured_fps"), int | float),
        )
    except OSError as error:
        raise PermissionDeniedError(
            f"unit profile {path.name} cannot be read: {error.strerror}",
            details={"file": path.name},
        ) from error
    except (ValueError, TypeError, AttributeError) as error:
        raise SchemaUnsupportedError(
            f"unit profile {path.name} is not usable: {error}",
            hint=f"delete {path.name} in the units folder ('vispek-hc check' prints the "
            "location) to start this unit afresh",
            details={"file": path.name},
        ) from error


def save_profile(profile: UnitProfile) -> Path:
    """Write a profile atomically and return its path."""
    path = _path(profile.unit_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps({"schema": SCHEMA, **asdict(profile)}, indent=2) + "\n", "utf-8"
    )
    temporary.replace(path)
    return path
