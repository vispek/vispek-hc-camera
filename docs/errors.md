# Errors and exit codes

Every error this package raises on purpose is a subclass of `vispek_hc.errors.VispekHCError`
and carries four fields: `code`, `message`, `hint`, `details`. With `--json` the command
line prints them as the `error` object of the envelope and exits with the code's family
exit code. `tests/test_errors.py` keeps this table and `src/vispek_hc/errors.py` identical.

## Exit-code families

| Exit | Family | Meaning |
|---|---|---|
| 0 | - | success |
| 1 | - | unexpected failure (a bug) |
| 2 | - | wrong usage |
| 3 | `EnvironmentProblem` | the host is missing something |
| 4 | `DeviceProblem` | the device cannot be found or opened |
| 5 | `PolicyRefusal` | the request is understood and refused |
| 6 | `HardwareFault` | talking to the light source or the camera failed |
| 7 | `QualityProblem` | finished only in part, or the result is not good enough |
| 130 | - | cancelled by the user |

## Codes

| Code | Exit | Class | Raised when |
|---|---|---|---|
| `INTERNAL_ERROR` | 1 | `VispekHCError` | Something failed that nobody anticipated. Please report it. |
| `USAGE_ERROR` | 2 | `UsageError` | The command line or an argument makes no sense. |
| `BACKEND_MISSING` | 3 | `BackendMissingError` | Neither ffmpeg nor OpenCV is available to read frames. |
| `CONTROLS_UNAVAILABLE` | 3 | `ControlsUnavailableError` | No way to set camera parameters on this host (for example uvc-util is not built). |
| `PERMISSION_DENIED` | 3 | `PermissionDeniedError` | The operating system refused access to the camera or the serial port. |
| `DEVICE_NOT_FOUND` | 4 | `DeviceNotFoundError` | No matching camera or serial port. |
| `AMBIGUOUS_DEVICE` | 4 | `AmbiguousDeviceError` | More than one candidate and none was named; `details.candidates` lists them. |
| `PORT_BUSY` | 4 | `PortBusyError` | Another process holds the serial port. |
| `UV_NOT_ALLOWED` | 5 | `UVNotAllowedError` | LEDs 1-4 were requested without `allow_uv` / `--allow-uv`. |
| `AWB_NOT_CONFIRMED` | 5 | `AWBNotConfirmedError` | Automatic white balance could not be confirmed off before a scan. |
| `EXPOSURE_NOT_CONFIRMED` | 5 | `ExposureNotConfirmedError` | Manual exposure could not be confirmed before a scan. |
| `GAIN_NOT_CONFIRMED` | 5 | `GainNotConfirmedError` | The camera saw light when its parameters were locked, so the gain it keeps from that moment is unknown. Close the lid and lock again. |
| `CONFIG_REJECTED` | 5 | `ConfigRejectedError` | A parameter is out of range or names something that does not exist. |
| `CALIBRATION_MISMATCH` | 5 | `CalibrationMismatchError` | The calibration was made under different settings; `details` lists each differing key. |
| `INVALID_STATE` | 5 | `InvalidStateError` | The operation does not apply to the data or device in its current state: for example an output folder that already holds a capture (pass `--overwrite`) or anything else (never replaced), or a file that cannot be written. |
| `SCHEMA_UNSUPPORTED` | 5 | `SchemaUnsupportedError` | A file declares a format version this release cannot read. |
| `LED_NO_ACK` | 6 | `LedNoAckError` | The light source did not answer after all retries; the LED state is unknown. |
| `LED_REJECTED` | 6 | `LedRejectedError` | The light source answered with a failure status. |
| `LED_SHUTDOWN_UNCONFIRMED` | 6 | `LedShutdownUnconfirmedError` | An OFF command was not confirmed; an LED may still be lit. |
| `NO_PROTOCOL_REPLY` | 6 | `NoProtocolReplyError` | No baud rate produced a valid reply. |
| `CAMERA_CONTROL_FAILED` | 6 | `CameraControlFailedError` | A camera parameter could not be set or read back. |
| `CAMERA_CHANGED` | 6 | `CameraChangedError` | Camera parameters differ before and after a scan. |
| `FRAME_INCOMPLETE` | 6 | `FrameIncompleteError` | No fresh frame arrived in time. |
| `FRAME_NOT_FOUND` | 6 | `FrameNotFoundError` | A frame that should exist is missing. |
| `TIMING_UNSTABLE` | 6 | `TimingUnstableError` | Frames did not settle after an LED change within the retry limit. |
| `RECORDING_PARTIAL` | 7 | `RecordingPartialError` | A recording failed after at least one output was finished, or its files could not be written. The finished outputs are on disk with `status: partial`; `details.cause` is the code of what went wrong, `details.outputs` how many are usable. (A recording that fails before its first output raises the cause itself.) |
| `CALIBRATION_QUALITY` | 7 | `CalibrationQualityError` | The white reference fails the quality gates. |
| `STORAGE_LOW` | 7 | `StorageLowError` | Less free disk space than twice what the recording is expected to need, or the disk filled up during it. |
| `CANCELLED` | 130 | `OperationCancelledError` | The user stopped the operation; raised only after every LED was switched off. |
