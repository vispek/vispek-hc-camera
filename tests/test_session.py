# SPDX-License-Identifier: Apache-2.0
"""Sessions: owning the devices, pairing probe, black level, parameter lock, clean-up."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vispek_hc import session as session_module
from vispek_hc import units, video
from vispek_hc.errors import (
    BackendMissingError,
    ConfigRejectedError,
    ControlsUnavailableError,
    DeviceNotFoundError,
    FrameIncompleteError,
    GainNotConfirmedError,
    LedNoAckError,
    UVNotAllowedError,
)
from vispek_hc.models import HC1500
from vispek_hc.protocol import Function
from vispek_hc.session import Session, open_session
from vispek_hc.sim import SimReply, SimulatedRig


def simulated(**options: Any) -> tuple[Session, SimulatedRig]:
    rig = SimulatedRig(**options)
    return Session.from_rig(rig), rig


# --- the simulated rig -----------------------------------------------------------------


def test_rig_wires_light_camera_and_controls_to_one_clock() -> None:
    rig = SimulatedRig()
    rig.light.connect(baud=115200)
    rig.frames.start()
    rig.light.on(9, 36)
    assert rig.device.lit == {9: 36}
    assert rig.frames.wait_new(3).received_at == pytest.approx(rig.clock.now())
    rig.frames.drop_out(1.0)
    rig.controls.write("auto-white-balance-temp", 0)
    rig.frames.wait_new()
    assert rig.controls.values["auto-white-balance-temp"] == 1, "restart resets the camera"


def test_rig_refuses_uv_unless_allowed() -> None:
    with pytest.raises(UVNotAllowedError):
        SimulatedRig().light.on(1, 100)
    SimulatedRig(allow_uv=True).light.on(1, 100)


# --- session life cycle ----------------------------------------------------------------


def test_entering_starts_the_stream_and_switches_every_led_off() -> None:
    session, rig = simulated()
    with session:
        assert [cmd.function for _, cmd in rig.device.log] == [Function.OFF] * 17
        assert session.frames.latest().seq == 1, "entering waits for the first frame"  # type: ignore[union-attr]
        assert session.unit.unit_id == "sim"


def test_leaving_switches_off_closes_the_port_and_stops_the_stream() -> None:
    session, rig = simulated()
    with session:
        session.light.on(9, 36)
    assert rig.device.lit == {}
    assert rig.device.closed
    with pytest.raises(Exception, match="new frames"):
        rig.frames.wait_new(timeout=1.0)


def test_leaving_after_an_error_still_cleans_up() -> None:
    session, rig = simulated()
    with pytest.raises(RuntimeError), session:
        session.light.on(9, 36)
        raise RuntimeError("boom")
    assert rig.device.lit == {}
    assert rig.device.closed


def test_stream_is_stopped_even_if_the_final_all_off_fails() -> None:
    session, rig = simulated()
    with pytest.raises(Exception, match="unconfirmed"), session:
        rig.device.unplugged = True
    with pytest.raises(Exception, match="new frames"):
        rig.frames.wait_new(timeout=1.0)


def test_entering_cleans_up_when_no_frame_ever_arrives() -> None:
    session, rig = simulated()
    rig.frames.stall(60.0)
    started = rig.clock.now()
    with pytest.raises(FrameIncompleteError):
        session.__enter__()
    assert rig.clock.now() - started == pytest.approx(15.0), "start-up allowance"
    assert rig.device.closed
    with pytest.raises(FrameIncompleteError):
        rig.frames.wait_new(timeout=1.0)


def test_entering_closes_the_light_when_the_stream_cannot_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, rig = simulated()

    def no_backend() -> None:
        raise BackendMissingError("ffmpeg cannot be started")

    monkeypatch.setattr(rig.frames, "start", no_backend)
    with pytest.raises(BackendMissingError):
        session.__enter__()
    assert rig.device.closed


def test_stream_is_stopped_even_if_closing_the_port_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    session, rig = simulated()

    def broken_close() -> None:
        raise OSError("device vanished")

    with pytest.raises(OSError, match="vanished"), session:
        monkeypatch.setattr(rig.device, "close", broken_close)
    with pytest.raises(FrameIncompleteError):
        rig.frames.wait_new(timeout=1.0)


# --- pairing probe ---------------------------------------------------------------------


def test_pair_probe_sees_the_led_and_measures_the_pipeline_latency() -> None:
    session, _ = simulated(latency_s=0.150)
    with session:
        result = session.pair_probe()
    assert result.paired
    assert result.led_id == 6
    assert result.rise_dn == pytest.approx(182, abs=1.5), "LED 6 at its usual scan power"
    assert result.pwm == 224
    assert 0.130 <= result.latency_s <= 0.150 + 0.1 + 0.0157 + 1 / 30  # type: ignore[operator]
    assert result.fps == pytest.approx(10.0)
    assert session.unit.pipeline_latency_ms == pytest.approx(result.latency_s * 1000)  # type: ignore[operator]
    assert session.unit.measured_fps == pytest.approx(10.0)


def test_pair_probe_keeps_the_latency_it_stored_first_unless_told_to_relearn() -> None:
    session, rig = simulated()
    with session:
        first = session.pair_probe()
        stored = session.unit.pipeline_latency_ms
        assert first.latency_s is not None
        assert stored == pytest.approx(first.latency_s * 1000)
        rig.frames._latency_s += 0.1  # the pipeline got a frame slower: the next probe sees it
        rig.clock.sleep(1.0)  # the lit frames of the last probe have left the pipeline
        second = session.pair_probe()
        assert second.latency_s == pytest.approx(first.latency_s + 0.1)
        assert session.unit.pipeline_latency_ms == stored, "scan timing stays what it was"
        rig.clock.sleep(1.0)
        third = session.pair_probe(relearn=True)
        assert third.latency_s == pytest.approx(second.latency_s)
        assert session.unit.pipeline_latency_ms == pytest.approx(stored + 100)


def test_pair_probe_uses_the_pwm_the_unit_scans_led_6_with() -> None:
    session, rig = simulated()
    session.unit.pwm_by_led = {6: 120}
    with session:
        result = session.pair_probe()
    assert result.pwm == 120
    assert [entry[1] for entry in rig.device.lit_intervals() if entry[1]] == [120]


def test_pair_probe_waits_for_the_picture_to_settle_before_its_baseline() -> None:
    # Right after an LED went off, frames exposed under it are still arriving. Taken as
    # the baseline, they would hide the probe's own light (seen on the device after the
    # camera parameters had just been changed).
    session, _ = simulated()
    with session:
        session.light.on(6, 224)
        session.frames.wait_new(5)
        session.light.off(6)
        result = session.pair_probe()
    assert result.paired
    assert result.rise_dn == pytest.approx(182, abs=1.5)


def test_pair_probe_lights_one_visible_led_briefly() -> None:
    session, rig = simulated()
    with session:
        session.pair_probe()
        lit = [entry for entry in rig.device.lit_intervals() if entry[1]]
    assert len(lit) == 1
    led_id, pwm, start, end = lit[0]
    assert (led_id, pwm) == (6, HC1500.led(6).starting_pwm)
    assert not HC1500.led(led_id).needs_uv_permission
    assert 0.3 <= end - start <= 0.5


def test_pair_probe_still_pairs_when_the_pipeline_is_slower_than_the_blink() -> None:
    session, _ = simulated(latency_s=0.45)
    with session:
        result = session.pair_probe()
    assert result.paired
    assert result.latency_s > 0.4  # type: ignore[operator]


def test_pair_probe_reports_a_camera_that_does_not_react() -> None:
    session, rig = simulated(reflectance=0.0)  # the camera sees nothing of the LED
    with session:
        result = session.pair_probe()
    assert not result.paired
    assert result.latency_s is None
    assert session.unit.pipeline_latency_ms is None
    assert rig.device.lit == {}


def test_pair_probe_leaves_nothing_lit_when_the_on_is_never_confirmed() -> None:
    session, rig = simulated()
    with session:
        rig.device.script(SimReply(lost=True), SimReply(lost=True), SimReply(lost=True))
        with pytest.raises(LedNoAckError):
            session.pair_probe()
        assert rig.device.lit == {}, "the device did carry out the ON"


def test_pair_probe_latency_is_taken_from_the_frame_arrival_time() -> None:
    session, rig = simulated()
    with session:
        arrivals: list[float] = []
        original = rig.frames.wait_new

        def slow_decode(count: int = 1, timeout: float | None = None) -> Any:
            frame = original(count, timeout)
            arrivals.append(frame.received_at)
            rig.clock.sleep(0.03)  # time spent decoding must not count as latency
            return frame

        session.frames.wait_new = slow_decode  # type: ignore[method-assign]
        result = session.pair_probe()
        acked = next(t for t in rig.light.transactions if t.pwm == 224).acked_at
    assert result.latency_s in [pytest.approx(at - acked) for at in arrivals]


def test_pair_probe_switches_the_led_off_when_frames_stop() -> None:
    session, rig = simulated()
    with session:
        session.frames.wait_new()
        rig.frames.stall(30.0)
        with pytest.raises(Exception, match="new frames"):
            session.pair_probe()
        assert rig.device.lit == {}


# --- black level -----------------------------------------------------------------------


def test_black_level_is_the_median_of_a_dark_frame() -> None:
    session, rig = simulated()
    with session:
        session.light.on(9, 36)
        assert session.black_level() == (5, 5, 5)
        assert rig.device.lit == {}, "measured with every LED off"


# --- camera parameter lock -------------------------------------------------------------


def test_lock_uses_the_model_profile_for_a_new_unit_and_remembers_what_held() -> None:
    session, rig = simulated()
    rig.controls.quantise["exposure-time-abs"] = lambda value: value // 4 * 4
    with session:
        report = session.lock_controls()
        assert report.awb_off
        assert report.exposure_manual
        assert session.unit.camera_controls["exposure-time-abs"] == 156
        assert session.unit.camera_controls == report.effective


def test_unconfirmed_lock_is_not_remembered(home: Path) -> None:
    rig = SimulatedRig()
    rig.controls.quantise["auto-white-balance-temp"] = lambda value: 1
    unit = units.load_profile("bench-1")
    with Session(rig.light, rig.frames, rig.controls, unit=unit, clock=rig.clock) as session:
        report = session.lock_controls()
    assert not report.awb_off
    assert unit.camera_controls == {}
    assert not home.exists(), "nothing saved"


def test_lock_restores_the_values_stored_for_the_unit() -> None:
    session, rig = simulated()
    session.unit.camera_controls = {"exposure-time-abs": 80, "gain": 3}
    with session:
        session.lock_controls()
    assert rig.controls.values["exposure-time-abs"] == 80
    assert rig.controls.values["gain"] == 3
    assert rig.controls.values["auto-white-balance-temp"] == 0


def test_lock_lets_three_frames_pass_before_and_between() -> None:
    session, rig = simulated()
    with session:
        started = rig.clock.now()
        session.lock_controls()
        assert rig.clock.now() - started >= 1.0, "a second of darkness in automatic exposure"
        first = session.frames.wait_new()
        session.lock_controls(keep=True)
        assert session.frames.latest().seq == first.seq + 6  # type: ignore[union-attr]


# The camera keeps a gain its automatic exposure chose, from the moment automatic exposure
# is switched off, and no control reads it back (measured: a factor of 4.4 between a lock
# in darkness and one under light). Only a lock in darkness is repeatable.


def led9_red(session: Session, pwm: int = 36) -> float:
    session.light.on(9, pwm)
    session.frames.wait_new(8)
    level = float(video.decode(session.frames.latest())[..., 0].mean())  # type: ignore[arg-type]
    session.light.off(9)
    session.frames.wait_new(8)
    return level


def test_the_first_lock_passes_through_automatic_exposure_in_the_dark() -> None:
    session, rig = simulated()
    with session:
        rig.controls.write("auto-exposure-mode", 1)  # as an earlier program may have left it
        rig.controls.writes.clear()
        report = session.lock_controls()
        modes = [value for name, value in rig.controls.writes if name == "auto-exposure-mode"]
        assert modes == [8, 1], "automatic first, so that the gain is the camera's ceiling"
        assert report.gain_state == "dark"
        assert report.dark_level == (5.0, 5.0, 5.0)
        assert session.lock_is_current


def test_a_lock_switches_every_led_off_first() -> None:
    session, rig = simulated()
    with session:
        session.light.on(9, 36)
        report = session.lock_controls()
        assert rig.device.lit == {}
        assert report.gain_state == "dark"
        assert led9_red(session) == pytest.approx(HC1500.led(9).white_rgb[0] + 5, abs=1)


def test_a_lock_that_sees_light_is_refused_and_says_why() -> None:
    session, rig = simulated()
    rig.frames.ambient = 40.0  # the lid of the dark box is open
    with session:
        with pytest.raises(GainNotConfirmedError) as caught:
            session.lock_controls()
        assert caught.value.details["dark_level"] == [45.0, 45.0, 45.0]
        assert caught.value.details["limit"] == 12
        assert "lid" in (caught.value.hint or "")
        assert session.lock_report is None
        assert not session.lock_is_current
        rig.frames.ambient = 0.0
        assert session.lock_controls().gain_state == "dark"


def test_a_lock_under_light_can_be_asked_for_and_is_marked() -> None:
    session, rig = simulated()
    rig.frames.ambient = 40.0
    with session:
        report = session.lock_controls(require_dark=False)
        assert report.gain_state == "undetermined"
        assert report.dark_level == (45.0, 45.0, 45.0)
        assert report.awb_off and report.exposure_manual
        rig.frames.ambient = 0.0
        assert led9_red(session) < 60, "the simulated camera kept its low gain"
        # A later lock in darkness makes the state certain again.
        assert session.lock_controls().gain_state == "dark"
        assert led9_red(session) == pytest.approx(HC1500.led(9).white_rgb[0] + 5, abs=1)


def test_a_scan_may_keep_the_lock_of_its_stream_without_leaving_manual_exposure() -> None:
    session, rig = simulated()
    with session:
        session.lock_controls()
        rig.controls.writes.clear()
        session.light.on(9, 36)  # the lock at the start of a scan must not blink or wait
        report = session.lock_controls(keep=True)
        assert ("auto-exposure-mode", 8) not in rig.controls.writes
        assert rig.device.lit == {9: 36}
        assert report.gain_state == "dark"
        session.light.off(9)


def test_a_lock_that_was_asked_for_always_passes_through_darkness_again() -> None:
    # Seen on the device: another program had put the camera in automatic exposure under
    # light and back to manual. Every control read as before, the gain was 3.5 times
    # lower, and "Lock again" kept calling it settled.
    session, rig = simulated()
    with session:
        session.lock_controls()
        session.light.on(5, 1023)
        rig.controls.write("auto-exposure-mode", 8)
        rig.controls.write("auto-exposure-mode", 1)  # manual again, at the low gain
        session.light.off(5)
        assert led9_red(session) < 60
        assert session.lock_is_current, "nothing the camera reports gives it away"
        rig.controls.writes.clear()
        assert session.lock_controls().gain_state == "dark"
        assert ("auto-exposure-mode", 8) in rig.controls.writes
        assert led9_red(session) == pytest.approx(HC1500.led(9).white_rgb[0] + 5, abs=1)


def test_a_reopened_stream_needs_a_new_lock_in_the_dark() -> None:
    session, rig = simulated()
    with session:
        session.lock_controls()
        rig.frames.drop_out(2.0)  # the camera comes back in automatic mode
        session.frames.wait_new(timeout=10.0)
        assert session.lock_stale, "known without asking the camera"
        assert not session.lock_is_current
        rig.controls.writes.clear()
        report = session.lock_controls()
        assert ("auto-exposure-mode", 8) in rig.controls.writes
        assert report.gain_state == "dark"
        assert session.lock_is_current
        assert not session.lock_stale


def test_a_camera_found_in_automatic_exposure_is_locked_in_the_dark_again() -> None:
    session, rig = simulated()
    with session:
        session.lock_controls()
        rig.controls.write("auto-exposure-mode", 8)  # another program touched the camera
        rig.controls.writes.clear()
        assert not session.lock_is_current
        assert session.lock_controls().gain_state == "dark"
        assert rig.device.lit == {}


def test_lock_without_a_control_backend_is_unavailable() -> None:
    rig = SimulatedRig()
    session = Session(rig.light, rig.frames, None, unit=units.UnitProfile("sim"), clock=rig.clock)
    with session, pytest.raises(ControlsUnavailableError):
        session.lock_controls()


def test_simulated_session_never_writes_the_unit_profile(home: Path) -> None:
    session, _ = simulated()
    with session:
        session.lock_controls()
        session.pair_probe()
    assert not home.exists()


def test_real_session_saves_what_it_learned(home: Path) -> None:
    rig = SimulatedRig()
    unit = units.load_profile("bench-1")
    with Session(rig.light, rig.frames, rig.controls, unit=unit, clock=rig.clock) as session:
        session.lock_controls()
        session.pair_probe()
    stored = units.load_profile("bench-1")
    assert stored.camera_controls["exposure-time-abs"] == 157
    assert stored.pipeline_latency_ms is not None


# --- opening ---------------------------------------------------------------------------


def test_open_session_simulated() -> None:
    with open_session(simulate=True) as session:
        assert session.simulated
        assert session.pair_probe().paired
        assert session.describe() == {
            "simulated": True,
            "unit_id": "sim",
            "serial": {"port": "sim", "baud": 115200},
            "camera": {
                "name": "simulated",
                "backend": "sim",
                "source": "uncompressed",
                "requested_size": [64, 36],
                "requested_fps": 10.0,
            },
            "controls": {"backend": "sim", "device_id": "sim"},
        }


def test_open_session_real_devices_are_assembled_and_cleaned_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = SimulatedRig()
    opened: dict[str, Any] = {}

    def open_light(port: str | None, *, baud: int | None, allow_uv: bool) -> Any:
        opened["light"] = (port, baud, allow_uv)
        rig.light.connect(baud=115200)
        rig.light.port, rig.light.allow_uv = "/dev/cu.usbserial-7", allow_uv
        return rig.light

    class Source:
        def __new__(cls, camera: str, **options: Any) -> Any:
            opened["camera"] = (camera, options)
            return rig.frames

    monkeypatch.setattr(session_module.Illuminator, "open", open_light)
    monkeypatch.setattr(session_module.video, "ffmpeg_version", lambda: (9, 0))
    monkeypatch.setattr(session_module.video, "list_cameras", lambda: ["LRCP 1080P-60FPS"])
    monkeypatch.setattr(session_module.video, "FfmpegSource", Source)
    monkeypatch.setattr(session_module, "PLATFORM", "darwin")
    refusals = iter([ControlsUnavailableError("not built"), DeviceNotFoundError("not on the bus")])

    def no_controls() -> None:
        raise next(refusals)

    monkeypatch.setattr(session_module.UvcUtilControls, "open", no_controls)
    with open_session(port="/dev/cu.usbserial-7") as session:
        assert session.controls is None
    rig = SimulatedRig()
    with open_session(port="/dev/cu.usbserial-7", unit="bench-1", allow_uv=True) as session:
        assert not session.simulated
        assert session.controls is None, "preview works without a control backend"
        assert session.unit.unit_id == "bench-1"
        assert opened["light"] == ("/dev/cu.usbserial-7", None, True)
        assert opened["camera"] == (
            "LRCP 1080P-60FPS",
            {"size": (1280, 720), "fps": 10, "source": "uncompressed", "version": (9, 0)},
        )
        assert session.describe()["controls"] is None
    assert rig.device.closed


def test_open_session_on_linux_controls_the_node_it_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = SimulatedRig()

    def open_light(port: str | None, *, baud: int | None, allow_uv: bool) -> Any:
        rig.light.connect(baud=115200)
        return rig.light

    asked: list[str] = []

    class FakeV4l2:
        backend, device_id = "v4l2-ctl", "v4l2:/dev/video2"

        @classmethod
        def open(cls, device: str) -> Any:
            asked.append(device)
            return rig.controls

    monkeypatch.setattr(session_module.Illuminator, "open", open_light)
    monkeypatch.setattr(session_module.video, "ffmpeg_version", lambda: (6, 1))
    monkeypatch.setattr(
        session_module.video, "list_cameras", lambda: ["/dev/video0", "/dev/video2"]
    )
    monkeypatch.setattr(
        session_module.video, "choose_camera", lambda explicit, names: explicit or names[1]
    )
    monkeypatch.setattr(session_module.video, "FfmpegSource", lambda camera, **options: rig.frames)
    monkeypatch.setattr(session_module, "PLATFORM", "linux")
    monkeypatch.setattr(session_module, "V4l2Controls", FakeV4l2)
    rig.controls.backend, rig.controls.device_id = "v4l2-ctl", "v4l2:/dev/video2"  # type: ignore[attr-defined]
    with open_session() as session:
        assert asked == ["/dev/video2"], "the node the frames come from, not the first one"
        assert session.controls is rig.controls
        assert session.describe()["controls"] == {
            "backend": "v4l2-ctl",
            "device_id": "v4l2:/dev/video2",
        }
        assert session.lock_controls().gain_state == "dark"


def test_open_session_checks_the_unit_label_before_touching_hardware(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def never(*args: Any, **kwargs: Any) -> None:
        raise AssertionError("hardware was opened")

    monkeypatch.setattr(session_module.Illuminator, "open", never)
    with pytest.raises(ConfigRejectedError):
        open_session(unit="bad id")


def test_open_session_closes_the_light_when_the_camera_cannot_be_opened(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = SimulatedRig()

    def no_ffmpeg() -> None:
        raise BackendMissingError("ffmpeg cannot be run")

    monkeypatch.setattr(session_module.Illuminator, "open", lambda *a, **k: rig.light)
    monkeypatch.setattr(session_module.video, "ffmpeg_version", no_ffmpeg)
    with pytest.raises(BackendMissingError):
        open_session()
    assert rig.device.closed
