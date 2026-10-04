# SPDX-License-Identifier: Apache-2.0
"""Recording: one dark frame plus one frame per LED, each chosen only once it has settled.

For every channel: wait out the pause, switch the LED on, wait at least as long as light
needs to show up in the frames that reach the host, then take frames until two in a row
agree. The frame read out while the LED switched (lit in one part, dark in the other) is
what this is for. OFF is sent in a ``finally`` block, and all waiting for the disk happens
with the LED off.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import math
import queue
import shutil
import threading
from collections.abc import Callable, Iterator, Mapping
from dataclasses import asdict, dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, Literal

import numpy as np
from numpy.typing import NDArray

from vispek_hc import safety, video
from vispek_hc.calibration import roi_pixels
from vispek_hc.controls import changed_controls
from vispek_hc.cube import Capture, CaptureWriter, ReductionName, check_target, load_capture
from vispek_hc.errors import (
    AWBNotConfirmedError,
    CameraChangedError,
    ConfigRejectedError,
    ControlsUnavailableError,
    ExposureNotConfirmedError,
    FrameIncompleteError,
    OperationCancelledError,
    RecordingPartialError,
    StorageLowError,
    TimingUnstableError,
    VispekHCError,
)
from vispek_hc.framecheck import compare, nearly_black, reduce, saturated_fraction
from vispek_hc.illuminator import UV_MAX_ON_S
from vispek_hc.interfaces import Frame, Transaction
from vispek_hc.models import HC1500, DeviceModel, Led
from vispek_hc.session import DEFAULT_UNIT, SETTLE_S, Session, open_session

log = logging.getLogger(__name__)

Mode = Literal["scan", "single", "dark", "video"]
ATTEMPTS = 3
FALLBACK_MIN_ON_MS = 500  # measured: light shows ~350 ms after the reply, whole frame ~450
DEFAULT_EXPOSURE_S = 0.0157
QUEUE_SIZE = 4
KEPT_REJECTS = 3
AUTO_PWM_TRIES = 10
AUTO_PWM_RANGE = (150.0, 235.0)  # accepted 99th percentile of the white board, code values
STOP_POLL_S = 0.1  # a stop request is noticed within this time, even in a long wait
MIN_ON_STEP_MS = 10  # automatic on-times are rounded up to this, so equal setups plan equally
# The sensor reads its rows out one after another: a frame is dark only if the LEDs were
# off for its exposure and for its whole read-out. An allowance for a read-out not known.
READOUT_ALLOWANCE_S = 0.050
SUGGESTION_STEP_MS = 50
UV_MIN_ON_LIMIT_MS = 5000  # leaves the selection its 5 s inside the 10 s UV watchdog
Progress = Callable[[dict[str, Any]], None]


@dataclass(frozen=True, slots=True)
class ScanSettings:
    """How to scan. The defaults are the recommended values."""

    channels: tuple[int, ...] | None = None  # None: the model's default (5-17, no UV)
    pwm: Mapping[int, int] | int | None = None  # None: the unit's profile, else the model's
    min_on_ms: int | Literal["auto"] = "auto"
    stable_frames: int = 2
    max_extra_frames: int = 10
    inter_channel_ms: int = 20
    stability_tolerance: float = 8.0
    allow_dark_signal: bool = False
    require_awb_off: bool = True
    white_roi: tuple[float, float, float, float] | None = None
    max_saturated_fraction: float = 0.0
    reduction: ReductionName = "dominant"
    average: int = 1
    cycles: int = 1  # 0: until stopped
    role: Literal["white"] | None = None
    setup_label: str | None = None
    enclosure: Literal["darkbox", "open", "unknown"] = "unknown"

    def __post_init__(self) -> None:
        whole = {"stable_frames", "max_extra_frames", "inter_channel_ms", "average", "cycles"}
        limits: dict[str, tuple[float, float]] = {
            "stable_frames": (2, 8),
            "max_extra_frames": (0, 60),
            "inter_channel_ms": (0, 10000),
            "stability_tolerance": (1, 64),
            "max_saturated_fraction": (0, 0.05),
            "average": (1, 1000),
            "cycles": (0, 10000),
        }
        if self.min_on_ms != "auto":
            limits["min_on_ms"] = (0, 10000)
            whole.add("min_on_ms")
        for name, (low, high) in limits.items():
            value = getattr(self, name)
            kinds: tuple[type, ...] = (int,) if name in whole else (int, float)
            if type(value) not in kinds or not low <= value <= high:
                raise ConfigRejectedError(
                    f"{name}={value!r} is not a number in {low:g}-{high:g}",
                    details={"setting": name, "value": value},
                )
        choices = {
            "reduction": ("dominant", "luma"),
            "role": (None, "white"),
            "enclosure": ("darkbox", "open", "unknown"),
        }
        for name, allowed in choices.items():
            if getattr(self, name) not in allowed:
                raise ConfigRejectedError(
                    f"{name}={getattr(self, name)!r} is not one of {allowed}",
                    details={"setting": name, "value": getattr(self, name)},
                )
        if self.white_roi is not None:
            roi_pixels(self.white_roi, 4096, 4096)  # refuses a region outside the picture


def check_plan(
    settings: ScanSettings,
    *,
    allow_uv: bool,
    model: DeviceModel = HC1500,
    profile_pwm: Mapping[int, int] | None = None,
) -> tuple[list[int], dict[int, int]]:
    """The part of planning that needs no device: channels, UV permission, PWM values.

    Run it before a device is opened, so that a plan which must be refused lights
    nothing. Each channel's PWM comes from the settings, else from the unit's profile,
    else from the model's table. Returns the channels and their PWM values.
    """
    chosen = settings.channels or model.default_channels
    if any(type(led_id) is not int for led_id in chosen):
        raise ConfigRejectedError("LED numbers are whole numbers", details={"led_ids": chosen})
    ids = safety.check_channels(model, chosen, allow_uv=allow_uv)
    gated = [led_id for led_id in ids if model.led(led_id).needs_uv_permission]
    if gated and settings.min_on_ms != "auto" and settings.min_on_ms > UV_MIN_ON_LIMIT_MS:
        raise ConfigRejectedError(
            f"min_on_ms={settings.min_on_ms} is too long for ultraviolet LEDs: they are "
            f"switched off after {UV_MAX_ON_S:g} s whatever happens",
            hint=f"use at most {UV_MIN_ON_LIMIT_MS} ms, or scan the UV channels separately",
            details={"setting": "min_on_ms", "value": settings.min_on_ms, "led_ids": gated},
        )
    given = settings.pwm
    if isinstance(given, Mapping):
        for led_id, value in given.items():
            if type(led_id) is not int or type(value) is not int or not 0 <= value <= 1023:
                raise ConfigRejectedError(
                    f"PWM {value!r} for LED {led_id!r} is not a whole number in 0-1023",
                    details={"led_id": led_id, "pwm": value},
                )
            model.led(led_id)
    pwm = {}
    for led_id in ids:
        level = given.get(led_id) if isinstance(given, Mapping) else given
        if level is None:
            level = (profile_pwm or {}).get(led_id, model.led(led_id).starting_pwm)
        if type(level) is not int or not 0 < level <= 1023:
            raise ConfigRejectedError(
                f"PWM {level!r} for LED {led_id} is not a whole number in 1-1023",
                details={"led_id": led_id, "pwm": level},
            )
        pwm[led_id] = level
    return ids, pwm


@dataclass(slots=True)
class _Plan:
    leds: list[Led]
    pwm: dict[int, int]
    min_on_s: dict[int, float]
    min_on_source: str
    floor_s: float  # earliest a frame can show the LED: latency + one period + exposure
    period_s: float
    dark_pause_s: float  # every LED off for this long before the next one is lit


def _round_up(value: float, step: int) -> int:
    return math.ceil(value / step - 1e-9) * step


def plan(session: Session, settings: ScanSettings, *, learned: bool = True) -> _Plan:
    """Check a scan before anything is lit: channels, UV permission, PWM values, timing.

    The frame period comes from the frame rate rounded to a whole number and automatic
    on-times are rounded up to 10 ms, so two sessions of the same setup plan the same
    values although each measures a slightly different rate. ``learned=False`` ignores
    the on-times an earlier ``tune`` stored.
    """
    model, unit = session.model, session.unit
    ids, pwm = check_plan(
        settings, allow_uv=session.light.allow_uv, model=model, profile_pwm=unit.pwm_by_led
    )
    fps = session.frames.measured_fps or unit.measured_fps or model.default_fps
    period = 1 / max(1, round(fps))
    report = session.lock_report
    exposure_units = report.effective.get("exposure-time-abs") if report else None
    exposure = exposure_units / 10000 if exposure_units else DEFAULT_EXPOSURE_S
    floor = (unit.pipeline_latency_ms or 0) / 1000 + period + exposure
    if settings.min_on_ms != "auto":
        min_on, source = dict.fromkeys(ids, settings.min_on_ms / 1000), "manual"
    elif unit.pipeline_latency_ms is None:
        min_on, source = dict.fromkeys(ids, FALLBACK_MIN_ON_MS / 1000), "auto"
    else:
        automatic = _round_up(floor * 1000, MIN_ON_STEP_MS)
        stored = unit.min_on_ms_by_led if learned else {}
        min_on = {n: max(stored.get(n, 0), automatic) / 1000 for n in ids}
        source = "auto"
    # The camera's own processing carries over from one picture to the next (measured:
    # LED 9 reads 118 straight after LED 8 and 141 after darkness). One frame exposed
    # entirely in the dark before every channel puts it in the same state each time.
    dark_frame = _round_up(
        (_dark_period(fps) + exposure + READOUT_ALLOWANCE_S) * 1000, MIN_ON_STEP_MS
    )
    pause = max(settings.inter_channel_ms, dark_frame) / 1000
    return _Plan([model.led(n) for n in ids], pwm, min_on, source, floor, period, pause)


def _dark_period(fps: float) -> float:
    """The frame period the dark pause is planned with.

    A rate within 2 % of a whole number counts as that number, so that two sessions of
    one setup plan the same pause (it is part of what binds a white reference to its
    samples). Any other rate is rounded down, never up: 5.6 fps, the camera's rate at
    1080p, has a longer period than 6 fps.
    """
    nearest = max(1, round(fps))
    if abs(fps - nearest) <= 0.02 * nearest:
        return 1 / nearest
    return 1 / math.floor(fps) if fps >= 1 else float(math.ceil(1 / fps))


class _DiskWorker:
    """Runs disk work on its own thread, a few jobs deep.

    ``submit`` blocks while the queue is full; the scan only calls it with every LED off.
    After a failure no further job runs, and every later ``submit`` or ``flush`` raises it.
    """

    def __init__(self) -> None:
        self._jobs: queue.Queue[Callable[[], object] | None] = queue.Queue(QUEUE_SIZE)
        self._failure: BaseException | None = None
        self._thread = threading.Thread(target=self._run, name="vispek-hc-disk", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while (job := self._jobs.get()) is not None:
            try:
                if self._failure is None:
                    job()
            except BaseException as error:
                self._failure = error
            finally:
                self._jobs.task_done()
        self._jobs.task_done()

    def submit(self, job: Callable[[], object]) -> None:
        if self._failure is not None:
            raise self._failure
        self._jobs.put(job)

    def drain(self) -> None:
        """Wait until everything submitted has been tried."""
        self._jobs.join()

    def flush(self) -> None:
        self.drain()
        if self._failure is not None:
            raise self._failure

    def close(self) -> None:
        self._jobs.put(None)
        self._thread.join()


@dataclass(slots=True)
class _Selection:
    frame: Frame
    rgb: NDArray[np.uint8]
    observed: int
    history: list[dict[str, Any]]
    check: dict[str, Any]


class _Selector:
    """Takes frames from the camera and picks the first one that has settled."""

    def __init__(self, session: Session, settings: ScanSettings, stop: threading.Event) -> None:
        self.session = session
        self.settings = settings
        self.stop = stop
        self.clock = session.clock
        self.size: tuple[int, int] | None = None

    def check_stop(self) -> None:
        if self.stop.is_set():
            raise OperationCancelledError("recording was stopped")

    def sleep(self, seconds: float) -> None:
        """Wait, noticing a stop request within ``STOP_POLL_S``."""
        end = self.clock.now() + seconds
        while (left := end - self.clock.now()) > 1e-9:
            self.check_stop()
            self.clock.sleep(min(left, STOP_POLL_S))

    def next(self) -> tuple[Frame, NDArray[np.uint8] | None]:
        """The next frame and its picture; no picture if the frame cannot be decoded."""
        self.check_stop()
        frame = self.session.frames.wait_new()
        try:
            rgb = video.decode(frame)
        except FrameIncompleteError:
            return frame, None
        if self.size is None:
            self.size = (frame.width, frame.height)
        if (frame.width, frame.height) != self.size:
            raise TimingUnstableError(
                f"frame size changed to {frame.width}x{frame.height} during the recording"
            )
        return frame, rgb

    def select(self, earliest: float, floor: float, channel: Any, min_on_s: float) -> _Selection:
        """Wait until ``earliest``, then return the first frame that agrees with its predecessor.

        ``floor`` is the time before which no frame can show the new light: frames received
        earlier never count as settled, whatever they look like. A frame that cannot be
        decoded is a rejected frame: the comparison starts again with the one after it.
        """
        settings = self.settings
        self.sleep(earliest - self.clock.now())
        started = self.clock.now()
        previous: NDArray[np.uint8] | None = None
        passes = 0
        history: list[dict[str, Any]] = []
        for observed in range(1, settings.stable_frames + settings.max_extra_frames + 1):
            frame, rgb = self.next()
            if rgb is None:
                passes = 0
                history.append({"frame": frame.seq, "rejected": "undecodable"})
            elif previous is not None:
                result = compare(
                    reduce(previous, channel), reduce(rgb, channel), settings.stability_tolerance
                )
                settled = result.stable and frame.received_at >= floor
                passes = passes + 1 if settled else 0
                history.append(
                    {
                        "frame": frame.seq,
                        "mean_difference": round(result.mean_difference, 3),
                        "large_change_fraction": round(result.large_change_fraction, 5),
                        "max_line_difference": round(result.max_line_difference, 3),
                    }
                )
                if passes >= settings.stable_frames - 1:
                    return _Selection(frame, rgb, observed, history, history[-1])
            previous = rgb
            if self.clock.now() - started > max(5.0, min_on_s + 3.0):
                break
        raise TimingUnstableError("frames did not settle", details={"observed_frames": observed})


def _stamp(moment: Any) -> str | None:
    return moment.isoformat(timespec="milliseconds") if moment else None


class _Recorder(_Selector):
    def __init__(
        self,
        session: Session,
        settings: ScanSettings,
        plan_: _Plan,
        writer: CaptureWriter,
        stop: threading.Event,
        progress: Progress,
    ) -> None:
        super().__init__(session, settings, stop)
        self.writer = writer
        self.progress = progress
        self.plan = plan_
        self.disk = _DiskWorker()
        sent = session.light.transactions  # commands before the recording are not its story
        self._logged: Transaction | None = sent[-1] if sent else None

    # --- one group ---------------------------------------------------------------------

    def _pause(self) -> None:
        self.sleep(self.plan.dark_pause_s)

    def _log_commands(self, group_id: str) -> None:
        """Log every command sent since the last call. Only called with the LED off:
        writing an event can wait for the disk."""
        recent: list[Transaction] = []
        for transaction in reversed(self.session.light.transactions):
            if transaction is self._logged:
                break
            recent.append(transaction)
        for transaction in reversed(recent):
            self.writer.event(
                "led_command",
                group_id=group_id,
                led_id=transaction.led_id,
                pwm=transaction.pwm,
                sent=transaction.sent.hex(" "),
                received=transaction.received.hex(" "),
                attempts=transaction.attempts,
                accepted=transaction.accepted,
                failure=transaction.failure,
                duration_ms=round(transaction.duration_s * 1000, 2),
            )
            self._logged = transaction

    def _store(self, group_id: str, name: str, chosen: _Selection, info: dict[str, Any]) -> None:
        writer = self.writer

        def job() -> None:
            entry = writer.write_frame(f"{group_id}/{name}", chosen.frame)
            writer.record_frame(group_id, {**info, **entry}, dark="led_id" not in info)

        self.disk.submit(job)

    def _dark(self, group_id: str) -> NDArray[np.uint8]:
        """Record the group's dark frame and return its picture."""
        floor = self.plan.floor_s
        try:
            self.session.light.all_off()
        finally:
            self._log_commands(group_id)
        self._pause()
        switched_off = self.clock.now()
        chosen = self.select(switched_off + floor, switched_off + floor, "G", floor)
        median = [int(np.median(chosen.rgb[..., index])) for index in range(3)]
        self._store(group_id, "dark", chosen, {"median_rgb": median})
        return chosen.rgb

    def _channel(
        self, group_id: str, led: Led, dark: NDArray[np.uint8], rejects: list[int]
    ) -> None:
        settings, light, plan_ = self.settings, self.session.light, self.plan
        pwm, min_on = plan_.pwm[led.led_id], plan_.min_on_s[led.led_id]
        channel = led.dominant_channel if settings.reduction == "dominant" else "luma"
        dark_values = reduce(dark, channel)
        dark_mean, dark_median = float(dark_values.mean()), int(np.median(dark_values))
        stem = f"led{led.led_id:02d}_{led.nm}nm"
        failure: VispekHCError | None = None
        for attempt in range(1, ATTEMPTS + 1):
            self.check_stop()
            self._pause()
            chosen = None
            try:
                acked = light.on(led.led_id, pwm)
                assert acked.acked_at is not None
                t0 = acked.acked_at
                chosen = self.select(t0 + min_on, t0 + plan_.floor_s, channel, min_on)
            except TimingUnstableError as error:
                failure = error
            finally:
                try:
                    light.off(led.led_id)
                finally:
                    self._log_commands(group_id)
            if chosen is not None:
                values = reduce(chosen.rgb, channel)
                response = float(values.mean()) - dark_mean
                no_response = response <= 1 or nearly_black(values, dark_median)
                if not (
                    no_response and settings.role == "white" and not settings.allow_dark_signal
                ):
                    break
                failure = TimingUnstableError(
                    f"LED {led.led_id} gave no light on the white reference",
                    hint="is the USB2 power port connected (UV LEDs need it)? is the lens covered?",
                )
                if len(rejects) < KEPT_REJECTS:
                    rejects.append(led.led_id)
                    name = f"{group_id}/rejected/{stem}_a{attempt}"
                    self.disk.submit(partial(self.writer.write_frame, name, chosen.frame))
            self.writer.event(
                "frame_rejected", group_id=group_id, led_id=led.led_id, attempt=attempt,
                reason=failure.message if failure else "",
            )  # fmt: skip
            self.sleep(min(1.0, 0.2 * attempt))
        else:
            assert failure is not None
            failure.details["led_id"] = led.led_id
            raise failure

        saturated = saturated_fraction(chosen.rgb)
        on_to_selected = (chosen.frame.received_at - t0) * 1000
        events = self.writer.event
        if no_response:
            events("no_led_response", group_id=group_id, led_id=led.led_id, response_dn=response)
        if saturated > max(settings.max_saturated_fraction, 0.01):
            events("saturation_warning", group_id=group_id, led_id=led.led_id, fraction=saturated)
        # Waiting this long would have gone straight to the pair of frames that agreed:
        # the selected frame is the second of that pair, two frame periods later.
        pair_started = max(plan_.floor_s * 1000, on_to_selected - 2000 * plan_.period_s)
        suggested = _round_up(pair_started, SUGGESTION_STEP_MS)
        events("timing_history", group_id=group_id, led_id=led.led_id, history=chosen.history)
        events(
            "channel_timing",
            group_id=group_id,
            led_id=led.led_id,
            on_ack_to_selected_ms=round(on_to_selected, 1),
            suggested_min_on_ms=suggested,
        )
        info = {
            "led_id": led.led_id,
            "nm": led.nm,
            "pwm": pwm,
            "min_on_ms": round(min_on * 1000),
            "on_ack_at_utc": _stamp(acked.acked_at_utc),
            "selected_at_utc": _stamp(self.clock.utc()),
            "frame_check": {
                "attempts": attempt,
                "observed_frames": chosen.observed,
                "on_ack_to_selected_ms": round(on_to_selected, 1),
                **{k: v for k, v in chosen.check.items() if k != "frame"},
                "rgb_saturated_fraction": round(saturated, 6),
                "response_dn": round(response, 2),
                "suggested_min_on_ms": suggested,
            },
        }
        self._store(group_id, stem, chosen, info)

    def _verify_controls(self, group_id: str, before: dict[str, int] | None) -> None:
        """Read the camera parameters back after a group; a locked scan fails if they moved."""
        controls = self.session.controls
        if before is None or controls is None:
            return
        after = {name: info.current for name, info in controls.read_all().items()}
        changed = changed_controls(before, after)
        self.writer.event("camera_control_verification", group_id=group_id, changed=changed)
        if changed and self.writer.document["config"]["controls_locked"]:
            raise CameraChangedError(
                f"camera parameters changed during the scan: {', '.join(changed)}",
                details={"changed": changed},
            )

    def group(self, group_id: str, before: dict[str, int] | None) -> None:
        """Scan one group; on any failure the group is marked and the error goes on."""
        rejects: list[int] = []
        try:
            dark = self._dark(group_id)
            for index, led in enumerate(self.plan.leds):
                self._channel(group_id, led, dark, rejects)
                self.progress({"group_id": group_id, "band": index + 1,
                               "bands": len(self.plan.leds), "led_id": led.led_id})  # fmt: skip
            self._pause()
            self._verify_controls(group_id, before)
            self.disk.flush()
        except BaseException as error:
            self.disk.drain()
            failed = isinstance(error, VispekHCError) and not isinstance(
                error, OperationCancelledError
            )
            if isinstance(error, VispekHCError):
                code, message = error.code, error.message
            else:  # an OSError's text holds the full path; its reason does not
                code = type(error).__name__
                message = getattr(error, "strerror", None) or code
            failure = {"code": code, "message": message,
                       "led_id": getattr(error, "details", {}).get("led_id")}  # fmt: skip
            with contextlib.suppress(OSError):
                self.writer.finish_group(group_id, "failed" if failed else "partial", failure)
            raise
        self.writer.finish_group(group_id)


def _lock(session: Session, settings: ScanSettings) -> tuple[dict[str, int] | None, bool]:
    """Lock and confirm the camera parameters, as every scan requires unless told otherwise.

    Returns the values read back (None if they cannot be read) and whether white balance,
    exposure and the gain the camera keeps are confirmed fixed. A lock that sees light is
    refused (``GAIN_NOT_CONFIRMED``) unless the scan was told to go on unlocked.
    """
    try:
        report = session.lock_controls(require_dark=settings.require_awb_off, keep=True)
    except ControlsUnavailableError:
        if settings.require_awb_off:
            raise
        return None, False
    if settings.require_awb_off and not report.awb_off:
        raise AWBNotConfirmedError(
            "automatic white balance could not be confirmed off",
            hint="scan anyway with --unlocked; the result is then not comparable",
        )
    if settings.require_awb_off and report.exposure_manual is False:
        raise ExposureNotConfirmedError("manual exposure could not be confirmed")
    settled = report.gain_state == "dark" or report.exposure_manual is None
    return dict(report.values), report.awb_off and report.exposure_manual is not False and settled


_CONVERTERS = {"yuyv422": "bt601-integer", "mjpeg": "Pillow"}


def _header(
    session: Session, settings: ScanSettings, mode: Mode, locked: bool, plan_: _Plan | None = None
) -> dict[str, Any]:
    """Everything known before the first frame is recorded, so that ``events.jsonl``
    alone can rebuild the capture."""
    latest = session.frames.latest()
    report, described = session.lock_report, session.describe()
    camera = dict(described["camera"])
    fps, latency = session.frames.measured_fps, session.unit.pipeline_latency_ms
    config = {key: value for key, value in asdict(settings).items() if key != "pwm"}
    config.update(
        ack_timeout_ms=round(session.light.ack_timeout_s * 1000),
        requested_size=camera.pop("requested_size", None),
        requested_fps=camera.pop("requested_fps", None),
        actual_size=[latest.width, latest.height] if latest else None,
        actual_fps=round(fps, 3) if fps else None,
        pipeline_latency_ms=round(latency, 1) if latency is not None else None,
        pixel_format=latest.pixel_format if latest else None,
        color_range=latest.color_range if latest else None,
        yuv_matrix="bt601",
        converter=_CONVERTERS.get(latest.pixel_format) if latest else None,
        source=camera.get("source"),
        controls_locked=locked,
        exposure_locked=report.exposure_manual if report else None,
        exposure={"requested": report.requested.get("exposure-time-abs"),
                  "effective": report.effective.get("exposure-time-abs")} if report else None,
        gain=report.values.get("gain") if report else None,
        camera_gain_state=report.gain_state if report else None,
        lock_dark_level=list(report.dark_level) if report and report.dark_level else None,
        auto_white_balance=(not report.awb_off) if report else None,
        camera_controls=report.values if report else None,
        camera_controls_after=None,
        white_roi_px=None,
        acquisition={"enclosure": settings.enclosure,
                     "setup_label": settings.setup_label or session.unit.setup_label,
                     "white_patch_roi": None},
    )  # fmt: skip
    if settings.white_roi and latest:
        edges = roi_pixels(settings.white_roi, latest.width, latest.height)
        config["white_roi_px"] = dict(zip(("x0", "y0", "x1", "y1"), edges, strict=True))
    if plan_ is not None:
        config["channels"] = [led.led_id for led in plan_.leds]
        config["pwm_by_led"] = {str(k): v for k, v in plan_.pwm.items()}
        config["min_on_ms"] = {
            "source": plan_.min_on_source,
            "by_led": {str(k): round(v * 1000) for k, v in plan_.min_on_s.items()},
        }
        config["dark_pause_ms"] = round(plan_.dark_pause_s * 1000)
    return {
        "unit_id": session.unit.unit_id,
        "reduction": settings.reduction,
        "recording": {"mode": mode, "role": settings.role, "cycles": settings.cycles,
                      "average": settings.average},
        "device": {"camera": camera, "serial": described["serial"],
                   "controls": described["controls"], "simulated": described["simulated"]},
        "config": config,
    }  # fmt: skip


def _check_space(directory: Path, session: Session, frames: int) -> None:
    latest = session.frames.latest()
    width, height = (latest.width, latest.height) if latest else session.model.default_size
    per_frame = width * height * (0.15 if latest and latest.pixel_format == "mjpeg" else 1.2)
    needed = int(2 * per_frame * frames)
    existing = next(path for path in (directory, *directory.parents) if path.exists())
    free = shutil.disk_usage(existing).free
    if free < needed:
        raise StorageLowError(
            f"about {needed // 2:,} bytes will be written and only {free:,} are free",
            details={"needed_bytes": needed, "free_bytes": free},
        )


def _write_failure(error: OSError) -> VispekHCError:
    reason = error.strerror or type(error).__name__  # str(error) would hold the full path
    if error.errno == errno.ENOSPC:
        return StorageLowError("the disk filled up during the recording")
    return RecordingPartialError(f"the recording could not be written: {reason}")


def _scans(
    session: Session,
    directory: Path,
    settings: ScanSettings,
    overwrite: bool,
    stop: threading.Event | None,
    progress: Progress | None,
    learned: bool = True,
) -> Iterator[int]:
    stop = stop or threading.Event()
    plan(session, settings)  # refuse a bad plan before touching the camera or the disk
    check_target(directory, overwrite)
    before, locked = _lock(session, settings)
    plan_ = plan(session, settings, learned=learned)  # again: the lock settled the exposure
    outputs = settings.cycles or 1
    _check_space(directory, session, outputs * settings.average * (len(plan_.leds) + 1))
    header = _header(session, settings, "scan", locked, plan_)
    try:
        with CaptureWriter(directory, header, overwrite=overwrite) as writer:
            recorder = _Recorder(
                session, settings, plan_, writer, stop, progress or (lambda update: None)
            )
            number = output = 0
            try:
                while settings.cycles == 0 or output < settings.cycles:
                    for _ in range(settings.average):
                        number += 1
                        recorder.group(f"g{number:04d}", before)
                    output += 1
                    yield output
            except OperationCancelledError:
                if settings.cycles != 0:  # a continuous recording ends by being stopped
                    raise
            except VispekHCError as error:
                if output == 0:
                    raise
                raise RecordingPartialError(
                    f"{output} output(s) were recorded, then: {error.message}",
                    hint=error.hint,
                    details={**error.details, "cause": error.code, "outputs": output},
                ) from error
            finally:
                try:
                    session.light.all_off()
                finally:
                    recorder.disk.close()
                    if session.controls is not None:
                        with contextlib.suppress(VispekHCError):
                            after = session.controls.read_all()
                            writer.document["config"]["camera_controls_after"] = {
                                name: info.current for name, info in after.items()
                            }
    except OSError as error:  # from a frame, from capture.json, or from closing the capture
        raise _write_failure(error) from error


def iter_scans(
    session: Session,
    directory: str | Path,
    settings: ScanSettings | None = None,
    *,
    overwrite: bool = False,
    stop: threading.Event | None = None,
    progress: Progress | None = None,
) -> Iterator[int]:
    """Record scans into ``directory``, yielding the number of each finished output.

    With ``cycles=0`` this goes on until ``stop`` is set. Whatever ends the recording,
    every LED is off afterwards and what was recorded is on disk. A failure after at
    least one finished output is ``RECORDING_PARTIAL`` (its cause is in ``details``).
    """
    return _scans(session, Path(directory), settings or ScanSettings(), overwrite, stop, progress)


def _record(scans: Iterator[int], directory: Path) -> Capture:
    try:
        for _ in scans:
            pass
    except VispekHCError as error:
        if (directory / "capture.json").exists():
            error.details.setdefault("capture", directory.name)
        raise
    return load_capture(directory)


def record(
    session: Session,
    directory: str | Path,
    settings: ScanSettings | None = None,
    *,
    overwrite: bool = False,
    stop: threading.Event | None = None,
    progress: Progress | None = None,
) -> Capture:
    """Record ``settings.cycles`` scans and return the capture that is now on disk."""
    scans = iter_scans(
        session, directory, settings, overwrite=overwrite, stop=stop, progress=progress
    )
    return _record(scans, Path(directory))


def record_frames(
    session: Session,
    directory: str | Path,
    mode: Literal["single", "dark", "video"],
    *,
    count: int = 1,
    overwrite: bool = False,
    stop: threading.Event | None = None,
    setup_label: str | None = None,
    enclosure: Literal["darkbox", "open", "unknown"] = "unknown",
) -> Capture:
    """Save camera frames without scanning.

    ``single``: one frame as it is. ``dark``: one frame with every LED off; cover the lens
    first and it shows how much light leaks in. ``video``: ``count`` frames in a row (0:
    until stopped).
    """
    directory = Path(directory)
    stop = stop or threading.Event()
    report = session.lock_report
    locked = bool(report and report.awb_off and report.exposure_manual is not False)
    described = ScanSettings(cycles=count, setup_label=setup_label, enclosure=enclosure)
    document = _header(session, described, mode, locked)
    document["recording"]["role"] = "dark" if mode == "dark" else None
    try:
        with CaptureWriter(directory, document, overwrite=overwrite) as writer:
            if mode == "dark":
                session.light.all_off()
                session.clock.sleep(SETTLE_S)
            if mode != "video":
                writer.record_single(writer.write_frame("frame", session.frames.wait_new()))
            else:
                written = 0
                writer.document["frames_index"] = "frames.jsonl"
                index_path = directory / "frames.jsonl"
                with index_path.open("a", encoding="utf-8", newline="\n") as index:
                    while (count == 0 or written < count) and not stop.is_set():
                        frame = session.frames.wait_new()
                        entry = writer.write_frame(f"frames/{written + 1:06d}", frame)
                        index.write(json.dumps(entry) + "\n")
                        index.flush()
                        written += 1
                        writer.document["recording"]["frames"] = written
    except OSError as error:
        raise _write_failure(error) from error
    return load_capture(directory)


def auto_pwm(
    session: Session,
    settings: ScanSettings | None = None,
    stop: threading.Event | None = None,
    notes: list[str] | None = None,
    *,
    store: bool = True,
) -> dict[int, int]:
    """Find, per channel, a PWM that makes the white board bright but not clipped.

    Locks the camera parameters first, then looks at the 99th percentile of the brightest
    RGB component inside the white region (raw code values) and accepts 150-235 with no
    more clipped pixels than ``max_saturated_fraction``. Too bright: halve, then bisect;
    too dark: double, then bisect; at most 10 tries. A channel that never gets into range
    takes the brightest value seen that did not clip, and says so: in the log and, if
    given, in ``notes``. The result is stored in the unit's profile and used by later
    scans, unless ``store=False``: a caller that goes on to record the white reference
    keeps the values only once that reference succeeded (:func:`keep_pwm`).
    """
    settings = settings or ScanSettings(role="white")
    plan(session, settings)  # refuse a bad plan before touching the camera
    _lock(session, settings)  # the search is only valid for the exposure the scan will use
    plan_ = plan(session, settings)
    selector = _Selector(session, settings, stop or threading.Event())
    light = session.light
    light.all_off()
    chosen: dict[int, int] = {}
    for led in plan_.leds:
        pwm, low, high = plan_.pwm[led.led_id], 0, 1024
        safe: list[int] = []
        in_range = False
        for _ in range(AUTO_PWM_TRIES):
            selector.sleep(plan_.dark_pause_s)  # the same camera state as in a scan
            try:
                acked = light.on(led.led_id, pwm)
                assert acked.acked_at is not None
                floor = acked.acked_at + plan_.floor_s
                picked = selector.select(
                    acked.acked_at + plan_.min_on_s[led.led_id], floor, led.dominant_channel, 0.3
                )
            finally:
                light.off(led.led_id)
            height, width = picked.rgb.shape[:2]
            x0, y0, x1, y1 = (
                roi_pixels(settings.white_roi, width, height)
                if settings.white_roi
                else (0, 0, width, height)
            )
            region = picked.rgb[y0:y1, x0:x1]
            peak = float(np.percentile(region.max(axis=-1), 99))
            clipped = saturated_fraction(region) > settings.max_saturated_fraction
            too_bright = clipped or peak > AUTO_PWM_RANGE[1]
            if not too_bright:
                safe.append(pwm)
            if in_range := not too_bright and peak >= AUTO_PWM_RANGE[0]:
                break
            if too_bright:
                high = pwm
                following = pwm // 2 if low == 0 else (low + high) // 2
            else:
                low = pwm
                following = min(1023, pwm * 2) if high == 1024 else (low + high) // 2
            if following in (pwm, 0):
                break
            pwm = following
        chosen[led.led_id] = pwm if in_range else max(safe, default=max(low, 1))
        if not in_range:
            reason = (
                "the brightest value that did not clip" if safe else "every value tried clipped"
            )
            note = (
                f"LED {led.led_id}: no PWM puts the white board at 150-235; "
                f"using {chosen[led.led_id]} ({reason})"
            )
            log.warning(note)
            if notes is not None:
                notes.append(note)
    if store:
        keep_pwm(session, chosen)
    return chosen


def keep_pwm(session: Session, pwm: Mapping[int, int]) -> None:
    """Store PWM values in the unit's profile: what later scans of this unit use."""
    session.unit.pwm_by_led.update(pwm)
    session.save_unit()


def tune(
    session: Session,
    directory: str | Path,
    settings: ScanSettings | None = None,
    *,
    overwrite: bool = False,
    stop: threading.Event | None = None,
    notes: list[str] | None = None,
) -> Capture:
    """Learn this unit on a white board: per-channel PWM, then how long each LED must be lit.

    Runs :func:`auto_pwm`, records one white scan with the result, and stores the PWM
    values and each channel's suggested minimum on-time in the unit's profile. The scan
    ignores on-times learned earlier, so tuning twice gives the same values. If the white
    scan fails (something else than the white board is in view), the profile stays as it
    was.
    """
    directory = Path(directory)
    settings = replace(settings or ScanSettings(), role="white")
    plan(session, settings)
    check_target(directory, overwrite)  # before the search lights anything
    pwm = auto_pwm(session, settings, stop, notes, store=False)
    scans = _scans(
        session, directory, replace(settings, pwm=pwm), overwrite, stop, None, learned=False
    )
    capture = _record(scans, directory)
    session.unit.pwm_by_led.update(pwm)
    session.unit.min_on_ms_by_led.update(
        {
            entry["led_id"]: entry["frame_check"]["suggested_min_on_ms"]
            for entry in capture.groups[0]["frames"]
        }
    )
    session.save_unit()
    return capture


def capture(
    directory: str | Path,
    settings: ScanSettings | None = None,
    *,
    simulate: bool = False,
    port: str | None = None,
    camera: str | None = None,
    unit: str = DEFAULT_UNIT,
    allow_uv: bool = False,
    overwrite: bool = False,
) -> Capture:
    """Open the device, lock the camera, scan, save, close: the whole thing in one call.

    Runs the pairing probe first when the unit's timing is not known yet. For more than
    one recording per connection use :func:`vispek_hc.open_session` and :func:`record`.
    """
    settings = settings or ScanSettings()
    check_plan(settings, allow_uv=allow_uv)  # refuse a bad plan before a device is opened
    check_target(Path(directory), overwrite)
    with open_session(
        simulate=simulate, port=port, camera=camera, unit=unit, allow_uv=allow_uv
    ) as session:
        plan(session, settings)  # with this unit's stored PWM values; nothing is lit yet
        if session.unit.pipeline_latency_ms is None:
            session.pair_probe()
        return record(session, directory, settings, overwrite=overwrite)
