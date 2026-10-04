# SPDX-License-Identifier: Apache-2.0
"""Frame acquisition: pixel conversion, JPEG splitting, ffmpeg plumbing, freshness."""

from __future__ import annotations

import dataclasses
import io
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st
from PIL import Image

from vispek_hc import video
from vispek_hc.errors import (
    AmbiguousDeviceError,
    BackendMissingError,
    DeviceNotFoundError,
    FrameIncompleteError,
    InvalidStateError,
    PermissionDeniedError,
)
from vispek_hc.interfaces import Frame, SystemClock

# --- YUYV to RGB -----------------------------------------------------------------------


def yuyv(*pixels: tuple[int, int, int, int]) -> bytes:
    """Pack (Y0, U, Y1, V) pixel pairs."""
    return bytes(value for pair in pixels for value in pair)


@pytest.mark.parametrize("level", [0, 5, 20, 128, 235, 254, 255])
def test_full_range_grey_keeps_the_code_value(level: int) -> None:
    rgb = video.yuyv_to_rgb(yuyv((level, 128, level, 128)), 2, 1)
    assert rgb.tolist() == [[[level] * 3] * 2]


def test_output_shape_and_type() -> None:
    rgb = video.yuyv_to_rgb(yuyv(*[(5, 128, 5, 128)] * 6), 4, 3)
    assert rgb.shape == (3, 4, 3)
    assert rgb.dtype == np.uint8


@pytest.mark.parametrize(
    ("y", "u", "v", "expected"),
    [
        (76, 85, 255, (254, 0, 0)),  # red
        (150, 44, 21, (0, 255, 1)),  # green
        (29, 255, 107, (0, 0, 254)),  # blue
        (128, 128, 255, (255, 37, 128)),  # clips high
        (128, 0, 128, (128, 172, 0)),  # clips low
    ],
)
def test_full_range_bt601_fixed_vectors(
    y: int, u: int, v: int, expected: tuple[int, int, int]
) -> None:
    rgb = video.yuyv_to_rgb(yuyv((y, u, y, v)), 2, 1)
    assert tuple(rgb[0, 0]) == expected


def test_both_pixels_of_a_pair_share_the_chroma_sample() -> None:
    rgb = video.yuyv_to_rgb(yuyv((76, 85, 150, 255)), 2, 1)
    assert tuple(rgb[0, 0]) == (254, 0, 0)
    assert tuple(rgb[0, 1]) == (255, 74, 74)


def reference_full_range(y: int, u: int, v: int) -> tuple[int, int, int]:
    def clip(value: float) -> int:
        return int(min(255, max(0, np.floor(value + 0.5))))

    return (
        clip(y + 1.402 * (v - 128)),
        clip(y - 0.344136 * (u - 128) - 0.714136 * (v - 128)),
        clip(y + 1.772 * (u - 128)),
    )


byte_values = st.integers(min_value=0, max_value=255)


@given(byte_values, byte_values, byte_values)
def test_full_range_matches_the_real_valued_bt601_formula(y: int, u: int, v: int) -> None:
    rgb = video.yuyv_to_rgb(yuyv((y, u, y, v)), 2, 1)
    assert tuple(int(c) for c in rgb[0, 0]) == reference_full_range(y, u, v)


@pytest.mark.parametrize(("y", "expected"), [(16, 0), (5, 0), (126, 128), (235, 255), (250, 255)])
def test_limited_range_grey_is_stretched(y: int, expected: int) -> None:
    rgb = video.yuyv_to_rgb(yuyv((y, 128, y, 128)), 2, 1, color_range="limited")
    assert rgb[0, 0].tolist() == [expected] * 3


@pytest.mark.parametrize(("y", "expected"), [(16, 0), (20, 5), (126, 128), (235, 255), (240, 255)])
def test_limited_range_puts_the_measured_black_level_of_20_at_5(y: int, expected: int) -> None:
    # Measured on an HC1500, 2026-10-02: every LED off reads Y = 20 on the uncompressed
    # path and 5, 5, 5 through the camera's JPEG path. (20 - 16) * 255 / 219 = 4.66.
    rgb = video.yuyv_to_rgb(yuyv((y, 128, y, 128)), 2, 1, "limited")
    assert rgb.tolist() == [[[expected] * 3] * 2]


def test_buffer_of_the_wrong_size_is_refused() -> None:
    with pytest.raises(ValueError, match="1280x720"):
        video.yuyv_to_rgb(b"\x00" * 100, 1280, 720)


# --- decoding frames -------------------------------------------------------------------


def frame(pixel_format: str, data: bytes, width: int, height: int) -> Frame:
    clock = SystemClock()
    return Frame(1, 1, clock.now(), clock.utc(), width, height, pixel_format, data)  # type: ignore[arg-type]


def jpeg(color: tuple[int, int, int], size: tuple[int, int] = (16, 8), **options: Any) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, "JPEG", quality=95, **options)
    return out.getvalue()


def test_decode_yuyv_frame() -> None:
    rgb = video.decode(frame("yuyv422", yuyv((5, 128, 5, 128)), 2, 1))
    assert rgb.tolist() == [[[5, 5, 5], [5, 5, 5]]]


def test_decode_uses_the_range_the_frame_says_it_has() -> None:
    data = yuyv((20, 128, 20, 128))
    plain = frame("yuyv422", data, 2, 1)
    assert plain.color_range == "full"
    assert video.decode(plain)[0, 0].tolist() == [20, 20, 20]
    limited = dataclasses.replace(plain, color_range="limited")
    assert video.decode(limited)[0, 0].tolist() == [5, 5, 5]
    assert video.decode(limited, "full")[0, 0].tolist() == [20, 20, 20], "an explicit choice wins"


def test_decode_rgb_frame() -> None:
    rgb = video.decode(frame("rgb24", bytes(range(12)), 2, 2))
    assert rgb.shape == (2, 2, 3)
    assert rgb[1, 1].tolist() == [9, 10, 11]


def test_decode_jpeg_frame() -> None:
    rgb = video.decode(frame("mjpeg", jpeg((5, 5, 5)), 16, 8))
    assert rgb.shape == (8, 16, 3)
    assert np.all(rgb == 5)


def test_decode_refuses_a_jpeg_of_another_size() -> None:
    with pytest.raises(FrameIncompleteError):
        video.decode(frame("mjpeg", jpeg((5, 5, 5)), 32, 8))


def test_decode_refuses_a_jpeg_whose_picture_data_is_cut_short() -> None:
    whole = jpeg((200, 30, 30), (64, 48))
    cut = whole[: len(whole) - 40]
    assert video.jpeg_size(cut) == (64, 48), "the header alone is fine"
    with pytest.raises(FrameIncompleteError):
        video.decode(frame("mjpeg", cut, 64, 48))


def test_decode_refuses_a_broken_jpeg() -> None:
    with pytest.raises(FrameIncompleteError):
        video.decode(frame("mjpeg", jpeg((5, 5, 5))[:60], 16, 8))


def test_jpeg_size_is_read_from_its_header() -> None:
    assert video.jpeg_size(jpeg((1, 2, 3), (48, 20))) == (48, 20)


def absurd_jpeg() -> bytes:
    """A real JPEG whose header claims 65535 x 65535 pixels."""
    data = bytearray(jpeg((5, 5, 5), (16, 8)))
    start = data.index(b"\xff\xc0")
    data[start + 5 : start + 9] = b"\xff\xff\xff\xff"
    return bytes(data)


def test_absurdly_large_jpeg_is_refused_not_decoded() -> None:
    with pytest.raises(FrameIncompleteError):
        video.jpeg_size(absurd_jpeg())
    with pytest.raises(FrameIncompleteError):
        video.decode(frame("mjpeg", absurd_jpeg(), 65535, 65535))


def test_jpeg_larger_than_the_supported_side_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(video, "MAX_IMAGE_SIDE", 15)
    with pytest.raises(FrameIncompleteError, match="larger"):
        video.jpeg_size(jpeg((5, 5, 5), (16, 8)))
    assert video.jpeg_size(jpeg((5, 5, 5), (15, 8))) == (15, 8)


# --- splitting an MJPEG byte stream ----------------------------------------------------


def synthetic_jpeg(marker_in_segment: bool = True) -> bytes:
    """A structurally valid stream with end-of-image bytes hidden where they do not count."""
    app = b"thumb\xff\xd9\xff\xd8more" if marker_in_segment else b"plain"
    return (
        b"\xff\xd8"
        + b"\xff\xe1"
        + (len(app) + 2).to_bytes(2, "big")
        + app
        + b"\xff\xda\x00\x04\x01\x02"  # SOS with a 2-byte header payload
        + b"\x12\xff\x00\x34\xff\xd0\x56\xff\xd7\x78\xff\xff\x00"  # stuffing, RST0, RST7, fill
        + b"\xff\xd9"
    )


def test_split_returns_each_complete_jpeg() -> None:
    first, second = jpeg((200, 10, 10)), jpeg((10, 200, 10), (32, 16))
    buffer = bytearray(first + second)
    assert video.split_jpegs(buffer) == [first, second]
    assert buffer == b""


def test_split_keeps_an_unfinished_jpeg_for_later() -> None:
    first, second = jpeg((200, 10, 10)), jpeg((10, 200, 10))
    buffer = bytearray(first + second[:50])
    assert video.split_jpegs(buffer) == [first]
    assert bytes(buffer) == second[:50]
    buffer += second[50:]
    assert video.split_jpegs(buffer) == [second]


def test_split_is_not_fooled_by_markers_inside_segments_or_scan_data() -> None:
    one = synthetic_jpeg()
    buffer = bytearray(one + one)
    assert video.split_jpegs(buffer) == [one, one]


def test_split_skips_bytes_before_the_first_start_marker() -> None:
    one = synthetic_jpeg(marker_in_segment=False)
    buffer = bytearray(b"\x00\x01garbage" + one)
    assert video.split_jpegs(buffer) == [one]


def test_split_accepts_fill_bytes_before_a_marker() -> None:
    padded = b"\xff\xd8\xff\xff\xff\xe1\x00\x04ab\xff\xff\xda\x00\x02\x12\xff\xd9"
    buffer = bytearray(padded + padded)
    assert video.split_jpegs(buffer) == [padded, padded]


@pytest.mark.parametrize("keep", ["header", "scan"])
def test_split_cuts_a_truncated_jpeg_where_the_next_one_starts(keep: str) -> None:
    good = jpeg((10, 200, 10))
    truncated = good[:30] if keep == "header" else good[: good.index(b"\xff\xda") + 40]
    buffer = bytearray(truncated + good)
    assert video.split_jpegs(buffer) == [truncated, good]


def test_split_handles_restart_markers_from_a_real_encoder() -> None:
    noisy = Image.effect_noise((64, 48), 80).convert("RGB")
    out = io.BytesIO()
    noisy.save(out, "JPEG", quality=90, restart_marker_blocks=2)
    data = out.getvalue()
    assert b"\xff\xd0" in data, "encoder produced no restart markers"
    buffer = bytearray(data + data)
    assert video.split_jpegs(buffer) == [data, data]


@given(st.data())
def test_split_does_not_depend_on_chunking(data: st.DataObject) -> None:
    frames = [synthetic_jpeg(), jpeg((1, 2, 3)), synthetic_jpeg(False)]
    stream = b"".join(frames)
    cuts = sorted(data.draw(st.lists(st.integers(0, len(stream)), max_size=10)))
    buffer = bytearray()
    found: list[bytes] = []
    for start, end in zip([0, *cuts], [*cuts, len(stream)], strict=True):
        buffer += stream[start:end]
        found += video.split_jpegs(buffer)
    assert found == frames
    assert buffer == b""


# --- ffmpeg command line ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("banner", "version"),
    [
        ("ffmpeg version 9.0.1 Copyright (c) 2000-2026 the FFmpeg developers", (9, 0)),
        ("ffmpeg version n6.1.1 Copyright", (6, 1)),
        ("ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright", (4, 4)),
        ("ffmpeg version 5.1 Copyright", (5, 1)),
        ("ffmpeg version N-112345-g0123abcd Copyright", None),
        ("ffmpeg version 2026-09-01-git-abcdef-full_build-www.gyan.dev", None),
        ("something else entirely", None),
    ],
)
def test_ffmpeg_version_is_parsed_from_the_banner(
    banner: str, version: tuple[int, int] | None
) -> None:
    assert video.parse_ffmpeg_version(banner) == version


@pytest.mark.parametrize(
    ("version", "flags"),
    [
        ((9, 0), ["-fps_mode", "passthrough"]),
        ((5, 1), ["-fps_mode", "passthrough"]),
        ((5, 0), ["-vsync", "passthrough"]),
        ((4, 4), ["-vsync", "passthrough"]),
        (None, ["-fps_mode", "passthrough"]),  # development builds are recent
    ],
)
def test_passthrough_flag_depends_on_the_ffmpeg_version(
    version: tuple[int, int] | None, flags: list[str]
) -> None:
    assert video.passthrough_flags(version) == flags


def command(platform: str, source: str = "uncompressed", version: Any = (9, 0)) -> list[str]:
    return video.capture_command(
        "ffmpeg",
        platform=platform,
        camera="LRCP 1080P-60FPS" if platform != "linux" else "/dev/video2",
        size=(1280, 720),
        fps=10,
        source=source,  # type: ignore[arg-type]
        version=version,
    )


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
@pytest.mark.parametrize("source", ["uncompressed", "mjpeg"])
def test_every_capture_command_passes_frames_through_untouched(platform: str, source: str) -> None:
    argv = command(platform, source)
    assert argv[0] == "ffmpeg"
    assert "-nostdin" in argv
    assert "-nostats" in argv, "the progress line is never terminated by a newline"
    assert "passthrough" in argv
    assert argv.index("-fps_mode") > argv.index("-i"), "passthrough is an output option"
    assert "-r" not in argv
    assert argv[-1] == "-"
    assert "gray" not in argv
    assert "rgb24" not in argv


def test_old_ffmpeg_gets_the_old_flag() -> None:
    argv = command("darwin", version=(4, 4))
    assert "-vsync" in argv
    assert "-fps_mode" not in argv


def test_macos_uncompressed_command() -> None:
    assert command("darwin") == [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
        "-f", "avfoundation", "-framerate", "10", "-video_size", "1280x720",
        "-pixel_format", "yuyv422", "-i", "LRCP 1080P-60FPS:none",
        "-fps_mode", "passthrough", "-pix_fmt", "yuyv422", "-f", "rawvideo", "-",
    ]  # fmt: skip


def test_linux_uncompressed_command() -> None:
    assert command("linux") == [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
        "-f", "v4l2", "-input_format", "yuyv422", "-framerate", "10",
        "-video_size", "1280x720", "-i", "/dev/video2",
        "-fps_mode", "passthrough", "-pix_fmt", "yuyv422", "-f", "rawvideo", "-",
    ]  # fmt: skip


def test_windows_uncompressed_command() -> None:
    assert command("win32") == [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats",
        "-f", "dshow", "-rtbufsize", "128M", "-pixel_format", "yuyv422", "-framerate", "10",
        "-video_size", "1280x720", "-i", "video=LRCP 1080P-60FPS",
        "-fps_mode", "passthrough", "-pix_fmt", "yuyv422", "-f", "rawvideo", "-",
    ]  # fmt: skip


def test_camera_jpeg_bytes_are_copied_where_the_platform_allows() -> None:
    for platform in ("linux", "win32"):
        argv = command(platform, "mjpeg")
        tail = argv[argv.index("-i") + 2 :]
        assert tail == [
            "-fps_mode", "passthrough", "-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-f", "mjpeg", "-",
        ]  # fmt: skip
    assert "mjpeg" in command("linux", "mjpeg")[: command("linux", "mjpeg").index("-i")]
    assert "-vcodec" in command("win32", "mjpeg")


def test_macos_jpeg_path_has_to_re_encode() -> None:
    argv = command("darwin", "mjpeg")
    tail = argv[argv.index("-i") + 2 :]
    assert tail == [
        "-fps_mode", "passthrough", "-c:v", "mjpeg", "-q:v", "2", "-f", "mjpeg", "-",
    ]  # fmt: skip
    assert video.pixel_path("darwin", "mjpeg") == "mjpeg_reencode"
    assert video.pixel_path("linux", "mjpeg") == "mjpeg"
    assert video.pixel_path("darwin", "uncompressed") == "uncompressed"


def test_exact_frame_rates_are_passed_without_rounding() -> None:
    argv = video.capture_command(
        "ffmpeg", platform="darwin", camera="c", size=(1280, 720), fps=60.00024,
        source="mjpeg", version=(9, 0),
    )  # fmt: skip
    assert argv[argv.index("-framerate") + 1] == "60.00024"


def test_unknown_platform_has_no_backend() -> None:
    with pytest.raises(BackendMissingError):
        command("sunos5")


AVFOUNDATION_LISTING = """\
[AVFoundation indev @ 0x7fa] AVFoundation video devices:
[AVFoundation indev @ 0x7fa] [0] FaceTime HD Camera
[AVFoundation indev @ 0x7fa] [1] LRCP 1080P-60FPS
[AVFoundation indev @ 0x7fa] [2] Capture screen 0
[AVFoundation indev @ 0x7fa] AVFoundation audio devices:
[AVFoundation indev @ 0x7fa] [0] MacBook Pro Microphone
[in#0 @ 0x7fb] Error opening input: Input/output error
"""


def test_avfoundation_listing_yields_cameras_only() -> None:
    assert video.parse_avfoundation_devices(AVFOUNDATION_LISTING) == [
        "FaceTime HD Camera",
        "LRCP 1080P-60FPS",
    ]


STREAM_LINES = [
    ("Video: rawvideo (YUY2 / 0x32595559), yuyv422, 1280x720, 10 fps", (1280, 720)),
    ("Video: rawvideo, yuyv422, 1920x1080, 1000k tbr, 1000k tbn", (1920, 1080)),
    ("Video: mjpeg (Baseline), yuvj422p(pc), 640x480 [SAR 1:1 DAR 4:3]", (640, 480)),
    ("Audio: pcm_f32le, 48000 Hz", None),
]


@pytest.mark.parametrize(("line", "size"), [*STREAM_LINES, ("nothing useful", None)])
def test_stream_size_is_parsed_from_ffmpeg_messages(
    line: str, size: tuple[int, int] | None
) -> None:
    assert video.parse_stream_size(f"Input #0, avfoundation\n  Stream #0:0: {line}\n") == size


def test_frame_rate_is_measured_from_arrival_times() -> None:
    arrivals = [10.0 + i * 0.178 for i in range(20)]  # a "60 fps" mode that delivers 5.6
    assert video.measure_fps(arrivals) == pytest.approx(5.618, abs=0.001)
    assert video.measure_fps([1.0]) is None
    assert video.measure_fps([]) is None


@pytest.mark.parametrize(
    ("count", "fps", "timeout"),
    [(1, 10.0, 3.0), (2, 10.0, 3.0), (20, 10.0, 4.0), (5, 1.0, 7.0), (3, None, 3.0)],
)
def test_default_wait_follows_the_frame_rate(count: int, fps: float | None, timeout: float) -> None:
    assert video.wait_timeout(count, fps) == pytest.approx(timeout)


# --- the ffmpeg-backed source, with a fake process -------------------------------------


class ManualClock:
    def __init__(self) -> None:
        self.t = 100.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def utc(self) -> datetime:
        return datetime(2026, 1, 1, tzinfo=UTC)


class FakeProcess:
    """Stands in for ``subprocess.Popen``: the test writes what ffmpeg would output.

    Like the real thing, its pipe stays open until the process ends.
    """

    banner = b"  Stream #0:0: Video: rawvideo, yuyv422, 4x2, 10 fps, 10 tbr\n"

    def __init__(self, argv: list[str], **options: Any) -> None:
        self.args, self.options = argv, options
        os.write(options["stderr"], self.banner)
        read_end, self._write_end = os.pipe()
        self.stdout = os.fdopen(read_end, "rb", buffering=0)
        self.returncode: int | None = None
        self.signals: list[str] = []
        self.ignores_terminate = False
        self._lock = threading.Lock()

    def feed(self, data: bytes) -> None:
        os.write(self._write_end, data)

    def die(self, returncode: int = 1) -> None:
        with self._lock:
            if self.returncode is None:
                self.returncode = returncode
                os.close(self._write_end)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.signals.append("terminate")
        if not self.ignores_terminate:
            self.die(-15)

    def kill(self) -> None:
        self.signals.append("kill")
        self.die(-9)

    def wait(self, timeout: float | None = None) -> int:
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.args, timeout or 0)
        return self.returncode


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[FakeProcess]:
    processes: list[FakeProcess] = []

    def spawn(argv: list[str], **options: Any) -> FakeProcess:
        processes.append(FakeProcess(argv, **options))
        return processes[-1]

    monkeypatch.setattr(subprocess, "Popen", spawn)
    monkeypatch.setattr(video, "RECONNECT_FIRST_S", 0.01)
    monkeypatch.setattr(video, "RECONNECT_MAX_S", 0.04)
    monkeypatch.setattr(video, "STOP_GRACE_S", 0.01)
    return processes


RAW = yuyv(*[(5, 128, 5, 128)] * 4)  # one 4x2 frame


def wait_for(condition: Any, seconds: float = 10.0) -> None:
    deadline = time.monotonic() + seconds
    while not condition():
        assert time.monotonic() < deadline, "condition not reached in time"
        time.sleep(0.002)


def seq_is(source: video.FfmpegSource, seq: int) -> Any:
    return lambda: (latest := source.latest()) is not None and latest.seq == seq


@pytest.fixture
def source(spawned: list[FakeProcess]) -> Iterator[video.FfmpegSource]:
    made = make_source()
    made.start()
    yield made
    made.stop()


def make_source(**options: Any) -> video.FfmpegSource:
    return video.FfmpegSource(
        "LRCP 1080P-60FPS", size=(4, 2), fps=10, platform="darwin", version=(9, 0), **options
    )


def test_source_spawns_ffmpeg_detached_with_its_output_piped(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    (process,) = spawned
    assert process.args == [*command("darwin")[:9], "4x2", *command("darwin")[10:]]
    options = dict(process.options)
    assert isinstance(options.pop("stderr"), int), "messages go to a file, not a pipe"
    assert options == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "start_new_session": True,
    }
    source.stop()
    assert process.signals == ["terminate"]
    assert process.stdout.closed


def test_each_received_frame_gets_the_next_sequence_number(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    assert source.latest() is None
    spawned[0].feed(RAW)
    wait_for(seq_is(source, 1))
    first = source.latest()
    assert first is not None
    assert (first.generation, first.width, first.height) == (1, 4, 2)
    assert first.pixel_format == "yuyv422"
    assert first.data == RAW
    # Identical bytes are still a new frame: this camera's dark frames never differ.
    spawned[0].feed(RAW)
    wait_for(seq_is(source, 2))


def test_wait_ignores_frames_that_arrived_before_the_call(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    spawned[0].feed(RAW)
    wait_for(seq_is(source, 1))
    with pytest.raises(FrameIncompleteError) as caught:
        source.wait_new(timeout=0.05)
    assert (caught.value.details["wanted"], caught.value.details["arrived"]) == (1, 0)


def test_wait_returns_once_enough_new_frames_arrived(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    result: list[Frame] = []
    waiter = threading.Thread(target=lambda: result.append(source.wait_new(3, timeout=10)))
    waiter.start()
    fed = 0
    while waiter.is_alive():  # keep feeding: frames before the wait began do not count
        spawned[0].feed(RAW)
        fed += 1
        wait_for(seq_is(source, fed))
    waiter.join()
    assert 3 <= result[0].seq <= fed


def test_wait_for_less_than_one_frame_is_a_programming_error(source: video.FfmpegSource) -> None:
    with pytest.raises(ValueError, match="at least one"):
        source.wait_new(0)


def test_partial_frame_is_not_delivered(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    spawned[0].feed(RAW + RAW[:10])
    wait_for(seq_is(source, 1))
    time.sleep(0.02)
    assert source.latest().seq == 1  # type: ignore[union-attr]
    spawned[0].feed(RAW[10:])
    wait_for(seq_is(source, 2))
    assert source.latest().data == RAW  # type: ignore[union-attr]


def test_frames_are_cut_at_the_size_ffmpeg_reports_not_the_requested_one(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(FakeProcess, "banner", b"  Stream #0:0: Video: rawvideo, yuyv422, 8x2\n")
    source = make_source()
    source.start()
    try:
        spawned[0].feed(RAW * 2)
        wait_for(seq_is(source, 1))
        latest = source.latest()
        assert latest is not None
        assert (latest.width, latest.height, len(latest.data)) == (8, 2, 32)
        assert source.negotiated_size == (8, 2)
    finally:
        source.stop()


def test_stream_is_reopened_after_ffmpeg_dies(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    spawned[0].feed(RAW)
    wait_for(seq_is(source, 1))
    spawned[0].die()
    wait_for(lambda: len(spawned) == 2)
    assert spawned[0].stdout.closed
    spawned[1].feed(RAW)
    wait_for(seq_is(source, 2))
    assert source.latest().generation == 2  # type: ignore[union-attr]
    assert source.generation == 2
    source.stop()
    assert all(process.returncode is not None for process in spawned)


def test_reopening_backs_off_and_starts_over_after_a_frame(
    source: video.FfmpegSource, spawned: list[FakeProcess], caplog: pytest.LogCaptureFixture
) -> None:
    def pauses() -> list[str]:
        return [r.getMessage().split()[-2] for r in caplog.records if "reopening" in r.message]

    with caplog.at_level(logging.WARNING, logger="vispek_hc.video"):
        for expected in range(1, 5):  # four streams that die without a frame
            wait_for(lambda: len(spawned) == expected)  # noqa: B023
            spawned[-1].die()
        wait_for(lambda: len(spawned) == 5)
        assert pauses() == ["0.01", "0.02", "0.04", "0.04"]
        spawned[-1].feed(RAW)
        wait_for(seq_is(source, 1))
        spawned[-1].die()
        wait_for(lambda: len(spawned) == 6)
        assert pauses()[-1] == "0.01"


def test_stop_reaches_a_process_that_was_being_started(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    entered, gate = threading.Event(), threading.Event()

    def slow_respawn(argv: list[str], **options: Any) -> FakeProcess:
        if spawned:
            entered.set()
            assert gate.wait(10)
        spawned.append(FakeProcess(argv, **options))
        return spawned[-1]

    monkeypatch.setattr(subprocess, "Popen", slow_respawn)
    source = make_source()
    source.start()
    spawned[0].die()
    assert entered.wait(10)
    stopper = threading.Thread(target=source.stop)
    stopper.start()
    gate.set()
    stopper.join(10)
    assert not stopper.is_alive(), "stop() hangs"
    assert [process.returncode is not None for process in spawned] == [True, True]


def test_stop_during_the_reopen_pause_starts_nothing_new(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "RECONNECT_FIRST_S", 30.0)
    source = make_source()
    source.start()
    spawned[0].die()
    wait_for(lambda: spawned[0].stdout.closed)
    source.stop()
    assert len(spawned) == 1


def test_starting_twice_is_refused(source: video.FfmpegSource, spawned: list[FakeProcess]) -> None:
    with pytest.raises(InvalidStateError):
        source.start()
    assert len(spawned) == 1


def test_source_can_be_started_again_after_stop(spawned: list[FakeProcess]) -> None:
    source = make_source()
    source.start()
    source.stop()
    source.start()
    try:
        spawned[1].feed(RAW)
        wait_for(seq_is(source, 1))
        assert source.generation == 2
    finally:
        source.stop()


def test_mjpeg_source_takes_the_size_from_each_jpeg(spawned: list[FakeProcess]) -> None:
    source = make_source(source="mjpeg")
    source.start()
    try:
        picture = jpeg((5, 5, 5), (16, 8))
        spawned[0].feed(picture[:40])
        spawned[0].feed(picture[40:] + picture)
        wait_for(seq_is(source, 2))
        latest = source.latest()
        assert latest is not None
        assert (latest.width, latest.height, latest.pixel_format) == (16, 8, "mjpeg")
        assert latest.data == picture
    finally:
        source.stop()


def test_damaged_jpeg_is_dropped_and_the_stream_goes_on(spawned: list[FakeProcess]) -> None:
    source = make_source(source="mjpeg")
    source.start()
    try:
        picture = jpeg((5, 5, 5), (16, 8))
        spawned[0].feed(absurd_jpeg() + picture[:30] + picture)
        wait_for(seq_is(source, 1))
        assert source.latest().data == picture  # type: ignore[union-attr]
        assert len(spawned) == 1, "no reconnect for a bad frame"
    finally:
        source.stop()


def test_mjpeg_buffer_is_capped(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(video, "MAX_BUFFER", 64)
    sizes: list[int] = []
    real_split = video.split_jpegs

    def watching(buffer: bytearray) -> list[bytes]:
        sizes.append(len(buffer))
        return real_split(buffer)

    monkeypatch.setattr(video, "split_jpegs", watching)
    source = make_source(source="mjpeg")
    source.start()
    try:
        spawned[0].feed(b"\xff\xd8\xff\xda\x00\x04\x01\x02" + b"\x11" * 200)  # never ends
        wait_for(lambda: sizes == [208])
        spawned[0].feed(b"\x11" * 10)
        wait_for(lambda: sizes == [208, 10])  # the 208 bytes were thrown away
    finally:
        source.stop()


def test_reader_survives_an_unexpected_failure_by_reopening_the_stream(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_split = video.split_jpegs
    failures = iter([RuntimeError("bug in the splitter")])

    def flaky(buffer: bytearray) -> list[bytes]:
        for failure in failures:
            raise failure
        return real_split(buffer)

    monkeypatch.setattr(video, "split_jpegs", flaky)
    source = make_source(source="mjpeg")
    source.start()
    try:
        picture = jpeg((5, 5, 5), (16, 8))
        spawned[0].feed(picture)
        wait_for(lambda: len(spawned) == 2)
        spawned[1].feed(picture)
        wait_for(seq_is(source, 1))
    finally:
        source.stop()


@pytest.mark.parametrize("claimed", [b"0x0", b"99999x99999"])
def test_nonsense_stream_size_is_ignored(
    claimed: bytes, spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    banner = b"  Stream #0:0: Video: rawvideo, yuyv422, " + claimed + b"\n"
    monkeypatch.setattr(FakeProcess, "banner", banner)
    source = make_source()
    source.start()
    try:
        spawned[0].feed(RAW)
        wait_for(seq_is(source, 1))
        assert source.negotiated_size == (4, 2)
    finally:
        source.stop()


@pytest.fixture
def temp_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def test_log_file_is_closed_in_the_parent_and_removed_on_stop(
    spawned: list[FakeProcess], temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    closed: list[int] = []
    real_close = os.close
    monkeypatch.setattr(os, "close", lambda fd: (closed.append(fd), real_close(fd))[1])
    source = make_source()
    source.start()
    assert spawned[0].options["stderr"] in closed
    assert len(list(temp_dir.iterdir())) == 1
    spawned[0].die()
    wait_for(lambda: source.generation == 2)
    assert len(list(temp_dir.iterdir())) == 1, "one log per running process"
    source.stop()
    assert list(temp_dir.iterdir()) == []


def test_failed_start_leaves_no_log_file_behind(
    temp_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def missing(argv: list[str], **options: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", missing)
    with pytest.raises(BackendMissingError):
        make_source().start()
    assert list(temp_dir.iterdir()) == []


def test_unwritable_temp_dir_is_reported_and_survived(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    real_mkstemp = tempfile.mkstemp
    refuse = iter([True, False, True])  # first start, second start, first reopen attempt

    def sometimes(**options: Any) -> Any:
        if next(refuse, False):
            raise PermissionError(13, "read-only")
        return real_mkstemp(**options)

    monkeypatch.setattr(tempfile, "mkstemp", sometimes)
    source = make_source()
    with pytest.raises(BackendMissingError, match="log file"):
        source.start()
    source.start()  # the failed start left nothing half-running
    try:
        spawned[0].die()  # the next refusal hits the reopen, which must retry instead of dying
        wait_for(lambda: len(spawned) == 2)
    finally:
        source.stop()


def test_clean_stop_logs_no_reopening(
    spawned: list[FakeProcess], caplog: pytest.LogCaptureFixture
) -> None:
    source = make_source()
    with caplog.at_level(logging.WARNING, logger="vispek_hc.video"):
        source.start()
        source.stop()
    assert caplog.records == []


def test_stop_wakes_whoever_is_waiting_for_frames(spawned: list[FakeProcess]) -> None:
    source = make_source()
    source.start()
    errors: list[Exception] = []

    def wait() -> None:
        try:
            source.wait_new(timeout=30)
        except FrameIncompleteError as error:
            errors.append(error)

    waiter = threading.Thread(target=wait)
    waiter.start()
    time.sleep(0.05)
    source.stop()
    waiter.join(5)
    assert not waiter.is_alive()
    assert len(errors) == 1


def test_no_process_is_started_once_stopping_even_if_the_pause_just_ended(
    spawned: list[FakeProcess],
) -> None:
    class StopsWhileWaiting(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            self.set()  # stop() arrives just as the pause ends
            return False

    source = make_source()
    source._stopping = StopsWhileWaiting()
    source.start()
    spawned[0].die()
    source._reader.join(5)  # type: ignore[union-attr]
    assert len(spawned) == 1
    source.stop()


def test_frame_rate_is_measured_from_frame_arrival(spawned: list[FakeProcess]) -> None:
    clock = ManualClock()
    source = make_source(clock=clock)
    source.start()
    try:
        assert source.measured_fps is None
        for number in range(1, 6):
            spawned[0].feed(RAW)
            wait_for(seq_is(source, number))
            clock.t += 0.1
        assert source.measured_fps == pytest.approx(10.0)
    finally:
        source.stop()


def test_stop_escalates_to_kill_when_terminate_is_ignored(spawned: list[FakeProcess]) -> None:
    source = make_source()
    source.start()
    spawned[0].ignores_terminate = True
    source.stop()
    assert spawned[0].signals == ["terminate", "kill"]


def test_ffmpeg_messages_are_kept_for_diagnostics(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    assert "rawvideo" in source.messages
    source.stop()
    assert "rawvideo" in source.messages, "still there after the stream is stopped"


def test_missing_ffmpeg_is_reported_as_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv: list[str], **options: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "Popen", missing)
    source = make_source()
    with pytest.raises(BackendMissingError):
        source.start()
    with pytest.raises(BackendMissingError):
        source.start()


def test_stream_goes_stale_three_seconds_after_its_last_frame(spawned: list[FakeProcess]) -> None:
    clock = ManualClock()
    source = make_source(clock=clock)
    source.start()
    try:
        clock.t += 14
        assert not source.is_stale(), "a stream that is still starting is not stale"
        clock.t += 2
        assert source.is_stale(), "nothing after 15 s: it never started"
        spawned[0].feed(RAW)
        wait_for(seq_is(source, 1))
        assert not source.is_stale()
        clock.t += 3.1
        assert source.is_stale()
        spawned[0].die()
        wait_for(lambda: source.generation == 2)
        assert not source.is_stale(), "a reopened stream gets the start-up allowance again"
    finally:
        source.stop()


def test_timeout_error_carries_what_ffmpeg_said(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    with pytest.raises(FrameIncompleteError) as caught:
        source.wait_new(timeout=0.02)
    assert "rawvideo" in caught.value.details["ffmpeg"]
    assert caught.value.details["wanted"] == 1


def test_messages_of_an_ended_stream_are_kept_across_the_reopen(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_source()
    source.start()
    try:
        monkeypatch.setattr(FakeProcess, "banner", b"second stream\n")
        spawned[0].die()
        wait_for(lambda: source.generation == 2)
        assert "rawvideo" in source.messages
        assert source.messages.endswith("second stream\n")
    finally:
        source.stop()


def test_waiting_on_a_stale_stream_restarts_it(spawned: list[FakeProcess]) -> None:
    clock = ManualClock()
    source = make_source(clock=clock)
    source.start()
    try:
        spawned[0].feed(RAW)
        wait_for(seq_is(source, 1))
        with pytest.raises(FrameIncompleteError):
            source.wait_new(timeout=0.02)
        assert len(spawned) == 1, "merely slow: left alone"
        clock.t += 4
        with pytest.raises(FrameIncompleteError):
            source.wait_new(timeout=0.02)
        wait_for(lambda: len(spawned) == 2)
        assert spawned[0].signals == ["kill"]
    finally:
        source.stop()


def test_camera_permission_refusal_is_recognised(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    refusal = b"[avfoundation @ 0x1] Failed to create AV capture input device: not authorized\n"
    monkeypatch.setattr(FakeProcess, "banner", refusal)
    source = make_source()
    source.start()
    try:
        with pytest.raises(PermissionDeniedError) as caught:
            source.wait_new(timeout=0.05)
        assert "Privacy" in (caught.value.hint or "")
    finally:
        source.stop()


def test_uncompressed_frames_of_the_camera_are_limited_range(
    source: video.FfmpegSource, spawned: list[FakeProcess]
) -> None:
    spawned[0].feed(yuyv(*[(20, 128, 20, 128)] * 4))
    wait_for(seq_is(source, 1))
    latest = source.latest()
    assert latest is not None
    assert (latest.pixel_format, latest.color_range) == ("yuyv422", "limited")
    assert np.all(video.decode(latest) == 5), "the black level of the HC1500"


def test_range_of_uncompressed_frames_can_be_overridden(spawned: list[FakeProcess]) -> None:
    source = make_source(color_range="full")
    source.start()
    try:
        spawned[0].feed(RAW)
        wait_for(seq_is(source, 1))
        latest = source.latest()
        assert latest is not None
        assert latest.color_range == "full"
    finally:
        source.stop()


def test_silent_camera_on_macos_points_at_the_camera_permission(
    spawned: list[FakeProcess], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Measured: without camera access macOS delivers no frame and ffmpeg says nothing.
    monkeypatch.setattr(FakeProcess, "banner", b"")
    source = make_source()
    source.start()
    try:
        with pytest.raises(FrameIncompleteError) as caught:
            source.wait_new(timeout=0.05)
        hint = caught.value.hint or ""
        assert "Privacy & Security > Camera" in hint
        assert "cable" in hint
    finally:
        source.stop()


# --- discovery -------------------------------------------------------------------------


def fake_run(stdout: str = "", stderr: str = "") -> Any:
    calls: list[list[str]] = []

    def run(argv: list[str], **options: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        assert options["timeout"] == 12.0
        return subprocess.CompletedProcess(argv, 1, stdout, stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def test_ffmpeg_version_asks_the_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    run = fake_run(stdout="ffmpeg version 7.1 Copyright (c) the FFmpeg developers\n")
    monkeypatch.setattr(subprocess, "run", run)
    assert video.ffmpeg_version("/opt/ffmpeg") == (7, 1)
    assert run.calls == [["/opt/ffmpeg", "-version"]]


def test_a_program_that_is_not_ffmpeg_is_a_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", fake_run(stdout="command not understood\n"))
    with pytest.raises(BackendMissingError):
        video.ffmpeg_version()


def test_missing_ffmpeg_binary_is_a_missing_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(argv: list[str], **options: Any) -> None:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(BackendMissingError) as caught:
        video.ffmpeg_version()
    assert "brew install ffmpeg" in (caught.value.hint or "")
    with pytest.raises(BackendMissingError):
        video.list_cameras(platform="darwin")


def test_macos_cameras_are_listed_through_ffmpeg(monkeypatch: pytest.MonkeyPatch) -> None:
    run = fake_run(stderr=AVFOUNDATION_LISTING)
    monkeypatch.setattr(subprocess, "run", run)
    assert video.list_cameras(platform="darwin") == ["FaceTime HD Camera", "LRCP 1080P-60FPS"]
    (argv,) = run.calls
    assert argv[-5:] == ["avfoundation", "-list_devices", "true", "-i", ""]
    assert "-nostdin" in argv


# What `ffmpeg -list_devices true -f dshow -i dummy` prints: ffmpeg 5 and later mark every
# device with its kind; older ones print two headed lists.
DSHOW_LISTING = r"""
[dshow @ 000001f3c2] "Integrated Webcam" (video)
[dshow @ 000001f3c2]   Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_5650#6&1#{65e8}\global"
[dshow @ 000001f3c2] "LRCP 1080P-60FPS" (video)
[dshow @ 000001f3c2]   Alternative name "@device_pnp_\\?\usb#vid_0c45&pid_6370#7&2#{65e8}\global"
[dshow @ 000001f3c2] "OBS Virtual Camera" (none)
[dshow @ 000001f3c2] "Microphone (Realtek Audio)" (audio)
[dshow @ 000001f3c2]   Alternative name "@device_cm_{33D9A762}\wave_{1D5A}"
dummy: Immediate exit requested
"""
DSHOW_LISTING_OLD = r"""
[dshow @ 0000020f] DirectShow video devices (some may be both video and audio devices)
[dshow @ 0000020f]  "Integrated Webcam"
[dshow @ 0000020f]     Alternative name "@device_pnp_\\?\usb#vid_0bda&pid_5650#6&1#{65e8}\global"
[dshow @ 0000020f]  "LRCP 1080P-60FPS"
[dshow @ 0000020f]     Alternative name "@device_pnp_\\?\usb#vid_0c45&pid_6370#7&2#{65e8}\global"
[dshow @ 0000020f] DirectShow audio devices
[dshow @ 0000020f]  "Microphone (Realtek Audio)"
[dshow @ 0000020f]     Alternative name "@device_cm_{33D9A762}\wave_{1D5A}"
"""


@pytest.mark.parametrize("listing", [DSHOW_LISTING, DSHOW_LISTING_OLD])
def test_windows_cameras_are_listed_through_ffmpeg(
    listing: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert video.parse_dshow_devices(listing) == ["Integrated Webcam", "LRCP 1080P-60FPS"]
    run = fake_run(stderr=listing)
    monkeypatch.setattr(subprocess, "run", run)
    names = video.list_cameras(platform="win32")
    assert names == ["Integrated Webcam", "LRCP 1080P-60FPS"]
    (argv,) = run.calls
    assert argv[-6:] == ["-list_devices", "true", "-f", "dshow", "-i", "dummy"]
    assert "-nostdin" in argv
    assert video.choose_camera(None, names) == "LRCP 1080P-60FPS"


def test_a_windows_host_without_cameras_lists_none() -> None:
    assert video.parse_dshow_devices("[dshow @ 01] Could not enumerate video devices") == []


def test_named_camera_wins() -> None:
    assert video.choose_camera("/dev/video4", ["LRCP 1080P-60FPS"]) == "/dev/video4"


def test_the_hc1500_camera_is_picked_by_name() -> None:
    names = ["FaceTime HD Camera", "LRCP 1080P-60FPS"]
    assert video.choose_camera(None, names) == "LRCP 1080P-60FPS"


def test_no_matching_camera_is_device_not_found() -> None:
    with pytest.raises(DeviceNotFoundError) as caught:
        video.choose_camera(None, ["FaceTime HD Camera"])
    assert caught.value.details == {"cameras": ["FaceTime HD Camera"]}


def test_two_matching_cameras_are_never_guessed() -> None:
    with pytest.raises(AmbiguousDeviceError) as caught:
        video.choose_camera(None, ["LRCP 1080P-60FPS", "LRCP 1080P-60FPS"])
    assert caught.value.details["option"] == "--camera"


# --- Linux: which /dev/video node is the camera -----------------------------------------


def linux_host(tmp_path: Path, cameras: list[tuple[str, str, str | None]]) -> Path:
    """A /sys/class/video4linux tree: (node, index, "vid:pid" or None for no USB device)."""
    if sys.platform == "win32":
        pytest.skip("making the links of a sysfs tree needs a privilege on Windows")
    sysfs = tmp_path / "sys" / "class" / "video4linux"
    for number, (node, index, usb_id) in enumerate(cameras):
        interface = tmp_path / "sys" / "devices" / f"usb-{usb_id or 'none'}-{number // 2}" / "1.0"
        interface.mkdir(parents=True, exist_ok=True)
        if usb_id:
            vendor, product = usb_id.split(":")
            (interface.parent / "idVendor").write_text(vendor + "\n")
            (interface.parent / "idProduct").write_text(product + "\n")
        entry = sysfs / node
        entry.mkdir(parents=True)
        (entry / "index").write_text(index + "\n")
        (entry / "name").write_text("some camera\n")
        (entry / "device").symlink_to(interface, target_is_directory=True)
    return sysfs


def test_linux_lists_capture_nodes_and_leaves_metadata_nodes_out(tmp_path: Path) -> None:
    # A UVC camera makes two nodes: the pictures (index 0) and their metadata (index 1).
    sysfs = linux_host(
        tmp_path,
        [
            ("video0", "0", "05ac:8514"), ("video1", "1", "05ac:8514"),
            ("video2", "0", "0c45:6370"), ("video3", "1", "0c45:6370"),
            ("video10", "0", None),
        ],
    )  # fmt: skip
    found = video.list_cameras(platform="linux", sysfs=sysfs)
    assert found == ["/dev/video0", "/dev/video2", "/dev/video10"], "in the order of their numbers"
    assert video.usb_id_of("/dev/video2", sysfs) == "0c45:6370"
    assert video.usb_id_of("/dev/video10", sysfs) is None
    assert video.usb_id_of("LRCP 1080P-60FPS", sysfs) is None, "a name is not a node"


def test_linux_picks_the_camera_by_its_usb_id(tmp_path: Path) -> None:
    sysfs = linux_host(tmp_path, [("video0", "0", "05ac:8514"), ("video2", "0", "0c45:6370")])
    names = video.list_cameras(platform="linux", sysfs=sysfs)
    assert video.matching_cameras(names, sysfs=sysfs) == ["/dev/video2"]
    assert video.choose_camera(None, names, sysfs=sysfs) == "/dev/video2"
    with pytest.raises(DeviceNotFoundError) as caught:
        video.choose_camera(None, ["/dev/video0"], sysfs=sysfs)
    assert caught.value.details == {"cameras": ["/dev/video0"]}


def test_two_hc1500_cameras_on_linux_are_never_guessed(tmp_path: Path) -> None:
    sysfs = linux_host(tmp_path, [("video0", "0", "0c45:6370"), ("video2", "0", "0c45:6370")])
    with pytest.raises(AmbiguousDeviceError):
        video.choose_camera(None, video.list_cameras(platform="linux", sysfs=sysfs), sysfs=sysfs)
    assert video.choose_camera("/dev/video2", [], sysfs=sysfs) == "/dev/video2"


def test_linux_without_sysfs_falls_back_to_nothing_found(tmp_path: Path) -> None:
    assert video.list_cameras(platform="linux", sysfs=tmp_path / "missing") == []
