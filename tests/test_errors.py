# SPDX-License-Identifier: Apache-2.0
"""Error codes, exit-code families, and their documentation."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from vispek_hc import errors
from vispek_hc.errors import ERROR_TYPES, VispekHCError

ERRORS_DOC = Path(__file__).parent.parent / "docs" / "errors.md"

# Exit-code families fixed by the CLI contract.
FAMILIES = {
    1: {"INTERNAL_ERROR"},
    2: {"USAGE_ERROR"},
    3: {"BACKEND_MISSING", "CONTROLS_UNAVAILABLE", "PERMISSION_DENIED"},
    4: {"DEVICE_NOT_FOUND", "AMBIGUOUS_DEVICE", "PORT_BUSY"},
    5: {
        "UV_NOT_ALLOWED",
        "AWB_NOT_CONFIRMED",
        "EXPOSURE_NOT_CONFIRMED",
        "GAIN_NOT_CONFIRMED",
        "CONFIG_REJECTED",
        "CALIBRATION_MISMATCH",
        "INVALID_STATE",
        "SCHEMA_UNSUPPORTED",
    },
    6: {
        "LED_NO_ACK",
        "LED_REJECTED",
        "LED_SHUTDOWN_UNCONFIRMED",
        "NO_PROTOCOL_REPLY",
        "CAMERA_CONTROL_FAILED",
        "CAMERA_CHANGED",
        "FRAME_INCOMPLETE",
        "FRAME_NOT_FOUND",
        "TIMING_UNSTABLE",
    },
    7: {"RECORDING_PARTIAL", "CALIBRATION_QUALITY", "STORAGE_LOW"},
    130: {"CANCELLED"},
}


def all_subclasses(cls: type) -> list[type]:
    found = []
    for sub in cls.__subclasses__():
        found += [sub, *all_subclasses(sub)]
    return found


def test_every_code_sits_in_its_exit_code_family() -> None:
    by_exit: dict[int, set[str]] = {}
    for code, error_type in ERROR_TYPES.items():
        by_exit.setdefault(error_type.exit_code, set()).add(code)
    assert by_exit == FAMILIES


def test_each_concrete_error_class_has_its_own_code() -> None:
    concrete = [VispekHCError, *all_subclasses(VispekHCError)]
    concrete = [cls for cls in concrete if "code" in vars(cls)]
    assert errors.OperationCancelledError in concrete
    assert len({cls.code for cls in concrete}) == len(concrete)
    assert {cls.code: cls for cls in concrete} == ERROR_TYPES


def test_family_base_classes_cannot_be_raised_directly() -> None:
    families = [cls for cls in all_subclasses(VispekHCError) if "code" not in vars(cls)]
    assert families, "expected family base classes"
    for family in families:
        with pytest.raises(TypeError, match="family"):
            family("nope")


def test_subclass_of_a_concrete_error_keeps_its_code_and_can_be_raised() -> None:
    class StillNoAck(errors.LedNoAckError):
        pass

    error = StillNoAck("LED 5 did not answer")
    assert (error.code, error.exit_code) == ("LED_NO_ACK", 6)
    assert ERROR_TYPES["LED_NO_ACK"] is errors.LedNoAckError


def test_error_carries_the_envelope_fields() -> None:
    error = errors.AmbiguousDeviceError(
        "2 serial ports match",
        hint="pass --port",
        details={"candidates": ["/dev/cu.usbserial-1", "/dev/cu.usbserial-2"]},
    )
    assert str(error) == "2 serial ports match"
    assert error.to_dict() == {
        "code": "AMBIGUOUS_DEVICE",
        "message": "2 serial ports match",
        "hint": "pass --port",
        "details": {"candidates": ["/dev/cu.usbserial-1", "/dev/cu.usbserial-2"]},
    }


def test_hint_and_details_default_to_empty() -> None:
    error = errors.LedNoAckError("LED 5 did not answer")
    assert error.to_dict() == {
        "code": "LED_NO_ACK",
        "message": "LED 5 did not answer",
        "hint": None,
        "details": {},
    }


def test_public_names_are_exported() -> None:
    exported = set(errors.__all__)
    assert {cls.__name__ for cls in ERROR_TYPES.values()} <= exported
    assert "ERROR_TYPES" in exported


def test_docs_table_lists_exactly_the_defined_codes() -> None:
    rows = re.findall(
        r"^\| `([A-Z_]+)` \| (\d+) \| `(\w+)` \|", ERRORS_DOC.read_text("utf-8"), flags=re.M
    )
    documented = {code: (int(exit_code), class_name) for code, exit_code, class_name in rows}
    defined = {code: (cls.exit_code, cls.__name__) for code, cls in ERROR_TYPES.items()}
    assert documented == defined
    assert len(rows) == len(documented), "duplicate rows in docs/errors.md"
