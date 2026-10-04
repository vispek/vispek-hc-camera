# SPDX-License-Identifier: Apache-2.0
"""Every error this package raises on purpose, and the process exit code it maps to.

One class per error code. Classes are grouped under family base classes that fix the exit
code; a family base cannot be raised itself. ``docs/errors.md`` lists the same table.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar


class VispekHCError(Exception):
    """Base class. Raised directly only for failures nobody anticipated."""

    code: ClassVar[str] = "INTERNAL_ERROR"
    exit_code: ClassVar[int] = 1

    def __init__(
        self,
        message: str,
        *,
        hint: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        if type(self).code is VispekHCError.code and type(self) is not VispekHCError:
            raise TypeError(f"{type(self).__name__} is an error family; raise one of its codes")
        super().__init__(message)
        self.message = message
        self.hint = hint
        self.details: dict[str, Any] = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        """The ``error`` object of the CLI JSON envelope."""
        return {
            "code": self.code,
            "message": self.message,
            "hint": self.hint,
            "details": self.details,
        }


class UsageError(VispekHCError):
    """The command line or a function argument makes no sense."""

    code = "USAGE_ERROR"
    exit_code = 2


class EnvironmentProblem(VispekHCError):
    """Family: something the host must provide is missing."""

    exit_code = 3


class BackendMissingError(EnvironmentProblem):
    code = "BACKEND_MISSING"


class ControlsUnavailableError(EnvironmentProblem):
    code = "CONTROLS_UNAVAILABLE"


class PermissionDeniedError(EnvironmentProblem):
    code = "PERMISSION_DENIED"


class DeviceProblem(VispekHCError):
    """Family: the device cannot be found or opened."""

    exit_code = 4


class DeviceNotFoundError(DeviceProblem):
    code = "DEVICE_NOT_FOUND"


class AmbiguousDeviceError(DeviceProblem):
    code = "AMBIGUOUS_DEVICE"


class PortBusyError(DeviceProblem):
    code = "PORT_BUSY"


class PolicyRefusal(VispekHCError):
    """Family: the request is understood and refused."""

    exit_code = 5


class UVNotAllowedError(PolicyRefusal):
    code = "UV_NOT_ALLOWED"


class AWBNotConfirmedError(PolicyRefusal):
    code = "AWB_NOT_CONFIRMED"


class ExposureNotConfirmedError(PolicyRefusal):
    code = "EXPOSURE_NOT_CONFIRMED"


class GainNotConfirmedError(PolicyRefusal):
    code = "GAIN_NOT_CONFIRMED"


class ConfigRejectedError(PolicyRefusal):
    code = "CONFIG_REJECTED"


class CalibrationMismatchError(PolicyRefusal):
    code = "CALIBRATION_MISMATCH"


class InvalidStateError(PolicyRefusal):
    code = "INVALID_STATE"


class SchemaUnsupportedError(PolicyRefusal):
    code = "SCHEMA_UNSUPPORTED"


class HardwareFault(VispekHCError):
    """Family: talking to the light source or the camera failed."""

    exit_code = 6


class LedNoAckError(HardwareFault):
    code = "LED_NO_ACK"


class LedRejectedError(HardwareFault):
    code = "LED_REJECTED"


class LedShutdownUnconfirmedError(HardwareFault):
    code = "LED_SHUTDOWN_UNCONFIRMED"


class NoProtocolReplyError(HardwareFault):
    code = "NO_PROTOCOL_REPLY"


class CameraControlFailedError(HardwareFault):
    code = "CAMERA_CONTROL_FAILED"


class CameraChangedError(HardwareFault):
    code = "CAMERA_CHANGED"


class FrameIncompleteError(HardwareFault):
    code = "FRAME_INCOMPLETE"


class FrameNotFoundError(HardwareFault):
    code = "FRAME_NOT_FOUND"


class TimingUnstableError(HardwareFault):
    code = "TIMING_UNSTABLE"


class QualityProblem(VispekHCError):
    """Family: the work finished only in part, or its result is not good enough."""

    exit_code = 7


class RecordingPartialError(QualityProblem):
    code = "RECORDING_PARTIAL"


class CalibrationQualityError(QualityProblem):
    code = "CALIBRATION_QUALITY"


class StorageLowError(QualityProblem):
    code = "STORAGE_LOW"


class OperationCancelledError(VispekHCError):
    """The user stopped the operation. Raised only after every LED was switched off."""

    code = "CANCELLED"
    exit_code = 130


def choose_one(
    explicit: str | None,
    candidates: Sequence[str],
    *,
    what: str,
    option: str,
    hint: str,
    seen: Mapping[str, Any] | None = None,
) -> str:
    """The device to use: the one that was named, else the only candidate.

    Never guesses and never asks: no candidate is ``DEVICE_NOT_FOUND``, several are
    ``AMBIGUOUS_DEVICE`` with the candidates listed.
    """
    if explicit:
        return explicit
    if not candidates:
        raise DeviceNotFoundError(f"no {what} found", hint=hint, details=seen)
    if len(candidates) > 1:
        raise AmbiguousDeviceError(
            f"{len(candidates)} candidates for the {what}",
            hint=f"this version supports one HC1500 per computer; name one with {option}",
            details={"candidates": list(candidates), "option": option},
        )
    return candidates[0]


def _concrete(cls: type[VispekHCError]) -> list[type[VispekHCError]]:
    own = [cls] if "code" in vars(cls) else []
    return own + [found for sub in cls.__subclasses__() for found in _concrete(sub)]


ERROR_TYPES: dict[str, type[VispekHCError]] = {cls.code: cls for cls in _concrete(VispekHCError)}
"""Error code to class, for every code that exists."""

__all__ = [
    "ERROR_TYPES",
    "AWBNotConfirmedError",
    "AmbiguousDeviceError",
    "BackendMissingError",
    "CalibrationMismatchError",
    "CalibrationQualityError",
    "CameraChangedError",
    "CameraControlFailedError",
    "ConfigRejectedError",
    "ControlsUnavailableError",
    "DeviceNotFoundError",
    "DeviceProblem",
    "EnvironmentProblem",
    "ExposureNotConfirmedError",
    "FrameIncompleteError",
    "FrameNotFoundError",
    "GainNotConfirmedError",
    "HardwareFault",
    "InvalidStateError",
    "LedNoAckError",
    "LedRejectedError",
    "LedShutdownUnconfirmedError",
    "NoProtocolReplyError",
    "OperationCancelledError",
    "PermissionDeniedError",
    "PolicyRefusal",
    "PortBusyError",
    "QualityProblem",
    "RecordingPartialError",
    "SchemaUnsupportedError",
    "StorageLowError",
    "TimingUnstableError",
    "UVNotAllowedError",
    "UsageError",
    "VispekHCError",
    "choose_one",
]
