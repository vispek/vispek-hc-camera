# SPDX-License-Identifier: Apache-2.0
"""Frames from the camera: ffmpeg as the capture process, numpy for the pixel maths.

Two pixel paths exist. ``uncompressed`` (default) takes the raw YUYV 4:2:2 buffer and
converts it here with a fixed integer formula, so no decoder or colour-range guess sits
between the camera and the numbers. ``mjpeg`` takes the camera's own JPEG frames.

A frame is new because it arrived, never because its bytes differ: this camera's dark
frames are byte-identical.
"""

from __future__ import annotations

import contextlib
import io
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
from collections import deque
from collections.abc import Sequence
from pathlib import Path
from typing import IO, Literal

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from vispek_hc.errors import (
    BackendMissingError,
    FrameIncompleteError,
    InvalidStateError,
    PermissionDeniedError,
    choose_one,
)
from vispek_hc.interfaces import Clock, ColorRange, Frame, SystemClock
from vispek_hc.models import HC1500, DeviceModel

log = logging.getLogger(__name__)

Source = Literal["uncompressed", "mjpeg"]

RECONNECT_FIRST_S = 1.5
RECONNECT_MAX_S = 10.0
STOP_GRACE_S = 2.0
STALE_S = 3.0  # no frame for this long means the stream is gone
STARTUP_S = 15.0  # a camera may take this long to deliver its first frame
LIST_TIMEOUT_S = 12.0
MAX_BUFFER = 32 * 1024 * 1024
MAX_IMAGE_SIDE = 4096
INSTALL_HINT = (
    "install ffmpeg: 'brew install ffmpeg', 'apt install ffmpeg' or 'winget install ffmpeg'"
)

# --- pixels ----------------------------------------------------------------------------


def yuyv_to_rgb(
    data: bytes, width: int, height: int, color_range: ColorRange = "full"
) -> NDArray[np.uint8]:
    """Convert one YUYV 4:2:2 buffer to RGB, shape (height, width, 3).

    BT.601, computed exactly in integers and rounded half up. ``full`` keeps Y as it is
    (0-255) with chroma centred on 128; ``limited`` stretches Y 16-235 and chroma 16-240.
    Both pixels of a pair use the pair's one chroma sample.
    """
    if len(data) != width * height * 2:
        raise ValueError(f"expected {width * height * 2} bytes for {width}x{height} YUYV")
    packed = np.frombuffer(data, np.uint8).reshape(height, width // 2, 4).astype(np.int64)
    y = packed[:, :, (0, 2)].reshape(height, width)
    cb = np.repeat(packed[:, :, 1], 2, axis=1) - 128
    cr = np.repeat(packed[:, :, 3], 2, axis=1) - 128
    if color_range == "full":
        luma, chroma, unit = y * 1_000_000, 1, 1_000_000
    else:
        luma, chroma, unit = (y - 16) * (255 * 224 * 1_000_000), 255 * 219, 219 * 224 * 1_000_000
    numerators = (
        luma + 1_402_000 * chroma * cr,
        luma - 344_136 * chroma * cb - 714_136 * chroma * cr,
        luma + 1_772_000 * chroma * cb,
    )
    rgb = np.stack([(2 * n + unit) // (2 * unit) for n in numerators], axis=-1)
    return np.clip(rgb, 0, 255).astype(np.uint8)


def jpeg_size(data: bytes) -> tuple[int, int]:
    """Width and height from a JPEG header. Unreadable or oversized pictures are refused."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            size = image.size
    except Exception as error:  # Pillow raises many types, also for absurd header sizes
        raise FrameIncompleteError(f"not a readable JPEG: {error}") from error
    if max(size) > MAX_IMAGE_SIDE:
        raise FrameIncompleteError(f"JPEG of {size[0]}x{size[1]} is larger than supported")
    return size


def decode(frame: Frame, color_range: ColorRange | None = None) -> NDArray[np.uint8]:
    """The frame as RGB code values, shape (height, width, 3).

    Uncompressed frames are converted with the range the frame carries, unless one is given.
    """
    if frame.pixel_format == "yuyv422":
        return yuyv_to_rgb(frame.data, frame.width, frame.height, color_range or frame.color_range)
    if frame.pixel_format == "rgb24":
        return np.frombuffer(frame.data, np.uint8).reshape(frame.height, frame.width, 3)
    size = jpeg_size(frame.data)
    if size != (frame.width, frame.height):
        raise FrameIncompleteError(
            f"JPEG is {size[0]}x{size[1]}, expected {frame.width}x{frame.height}"
        )
    try:
        with Image.open(io.BytesIO(frame.data)) as image:
            return np.asarray(image.convert("RGB"))
    except Exception as error:
        raise FrameIncompleteError(f"frame {frame.seq} cannot be decoded: {error}") from error


_NO_LENGTH = {0x01, *range(0xD0, 0xDA)}  # TEM, RST0-7, SOI, EOI carry no length field


def _jpeg_end(buffer: bytearray) -> int | None:
    """Length of the JPEG that starts at ``buffer[0]``, or None while it is incomplete.

    Walks the segments by their length fields instead of searching for the end marker,
    which also occurs inside embedded thumbnails. A damaged picture is cut where the
    damage shows (the caller drops what does not decode).
    """
    position = 2
    while position + 2 <= len(buffer):
        marker = buffer[position + 1]
        if buffer[position] != 0xFF or marker == 0xD8:
            # Damaged or truncated. Cut where the next picture starts, if it was walked over.
            restart = buffer.find(b"\xff\xd8", 2, position + 2)
            return restart if restart > 0 else position
        if marker == 0xD9:
            return position + 2
        if marker == 0xFF:
            position += 1  # fill byte before a marker
            continue
        if marker in _NO_LENGTH:
            position += 2
            continue
        if position + 4 > len(buffer):
            return None
        position += 2 + int.from_bytes(buffer[position + 2 : position + 4], "big")
        if marker != 0xDA:
            continue
        # Entropy-coded data follows a scan header: it runs until a real marker.
        while True:
            position = buffer.find(0xFF, position)
            if position < 0 or position + 1 >= len(buffer):
                return None
            follower = buffer[position + 1]
            if follower == 0xFF:
                position += 1  # fill byte
            elif follower == 0x00 or 0xD0 <= follower <= 0xD7:
                position += 2  # stuffed byte or restart marker: still scan data
            else:
                break
    return None


def split_jpegs(buffer: bytearray) -> list[bytes]:
    """Remove and return every complete JPEG at the front of ``buffer``."""
    frames: list[bytes] = []
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            del buffer[: -1 if buffer.endswith(b"\xff") else len(buffer)]
            return frames
        del buffer[:start]
        end = _jpeg_end(buffer)
        if end is None:
            return frames
        frames.append(bytes(buffer[:end]))
        del buffer[:end]


# --- ffmpeg ----------------------------------------------------------------------------


def parse_ffmpeg_version(banner: str) -> tuple[int, int] | None:
    """(major, minor) of a release build; None for development builds and non-ffmpeg text."""
    match = re.match(r"ffmpeg version n?(\d+)\.(\d+)", banner)
    return (int(match[1]), int(match[2])) if match else None


def _run_ffmpeg(arguments: list[str], ffmpeg: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [ffmpeg, *arguments],
            capture_output=True, text=True, errors="replace", timeout=LIST_TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.TimeoutExpired) as error:
        raise BackendMissingError(f"ffmpeg cannot be run: {error}", hint=INSTALL_HINT) from error


def ffmpeg_version(ffmpeg: str = "ffmpeg") -> tuple[int, int] | None:
    banner = _run_ffmpeg(["-version"], ffmpeg).stdout
    if not banner.startswith("ffmpeg version"):
        raise BackendMissingError(f"{ffmpeg} does not behave like ffmpeg", hint=INSTALL_HINT)
    return parse_ffmpeg_version(banner)


def passthrough_flags(version: tuple[int, int] | None) -> list[str]:
    """The flag that stops ffmpeg from duplicating or dropping frames to fake a rate."""
    return (
        ["-vsync", "passthrough"] if version and version < (5, 1) else ["-fps_mode", "passthrough"]
    )


def pixel_path(platform: str, source: Source) -> str:
    """What ends up in metadata: macOS cannot hand over the camera's JPEG bytes untouched."""
    return "mjpeg_reencode" if (platform, source) == ("darwin", "mjpeg") else source


def capture_command(
    ffmpeg: str,
    *,
    platform: str,
    camera: str,
    size: tuple[int, int],
    fps: float,
    source: Source,
    version: tuple[int, int] | None,
) -> list[str]:
    exact_rate = f"{fps:.6f}".rstrip("0").rstrip(".")  # modes like 60.00024 must not be rounded
    rate = ["-framerate", exact_rate, "-video_size", f"{size[0]}x{size[1]}"]
    raw = source == "uncompressed"
    if platform == "darwin":
        device = ["-f", "avfoundation", *rate, "-pixel_format", "yuyv422", "-i", f"{camera}:none"]
    elif platform == "linux":
        device = ["-f", "v4l2", "-input_format", "yuyv422" if raw else "mjpeg", *rate, "-i", camera]
    elif platform == "win32":
        pixels = ["-pixel_format", "yuyv422"] if raw else ["-vcodec", "mjpeg"]
        device = ["-f", "dshow", "-rtbufsize", "128M", *pixels, *rate, "-i", f"video={camera}"]
    else:
        raise BackendMissingError(f"no capture backend for platform {platform!r}")
    if raw:
        output = ["-pix_fmt", "yuyv422", "-f", "rawvideo", "-"]
    elif platform == "darwin":
        output = ["-c:v", "mjpeg", "-q:v", "2", "-f", "mjpeg", "-"]
    else:
        output = ["-c:v", "copy", "-bsf:v", "mjpeg2jpeg", "-f", "mjpeg", "-"]
    quiet = ["-hide_banner", "-nostdin", "-nostats"]  # the progress line never ends in "\n"
    return [ffmpeg, *quiet, *device, *passthrough_flags(version), *output]


def parse_avfoundation_devices(listing: str) -> list[str]:
    """Camera names from ``ffmpeg -f avfoundation -list_devices true``; screens are skipped."""
    names = []
    in_video = False
    for line in listing.splitlines():
        if "AVFoundation video devices" in line:
            in_video = True
        elif "AVFoundation audio devices" in line:
            in_video = False
        elif (
            in_video
            and (match := re.search(r"\] \[\d+\] (.+)$", line))
            and not match[1].startswith("Capture screen")
        ):
            names.append(match[1])
    return names


def parse_dshow_devices(listing: str) -> list[str]:
    """Camera names from ``ffmpeg -list_devices true -f dshow -i dummy``.

    ffmpeg 5 and later mark each device ``(video)``, ``(audio)`` or ``(none)``; older
    versions print a list under "DirectShow video devices" and one under "DirectShow
    audio devices". Alternative names (the device paths) are skipped.
    """
    names = []
    in_video = False
    for line in listing.splitlines():
        if "DirectShow video devices" in line:
            in_video = True
        elif "DirectShow audio devices" in line:
            in_video = False
        elif "Alternative name" in line:
            continue
        elif (match := re.search(r'\] +"(.+)"(?: \((\w+)\))?\s*$', line)) and (
            match[2] == "video" or (match[2] is None and in_video)
        ):
            names.append(match[1])
    return names


SYSFS_VIDEO = Path("/sys/class/video4linux")


def _node_number(entry: Path) -> int:
    digits = entry.name.removeprefix("video")
    return int(digits) if digits.isdigit() else -1


def linux_capture_nodes(sysfs: Path = SYSFS_VIDEO) -> list[str]:
    """Device nodes that deliver pictures. A UVC camera has a second node for metadata
    (index 1), which is left out."""
    nodes = []
    for entry in sorted(sysfs.glob("video*"), key=_node_number):
        try:
            index = (entry / "index").read_text("ascii").strip()
        except OSError:
            continue
        if index == "0":
            nodes.append(f"/dev/{entry.name}")
    return nodes


def usb_id_of(node: str, sysfs: Path = SYSFS_VIDEO) -> str | None:
    """``vid:pid`` of the USB device behind a Linux video node; None for anything else."""
    if not node.startswith("/dev/video"):
        return None
    try:
        device = (sysfs / Path(node).name / "device").resolve().parent  # the USB device
        vendor = (device / "idVendor").read_text("ascii").strip().lower()
        product = (device / "idProduct").read_text("ascii").strip().lower()
    except OSError:
        return None
    return f"{vendor}:{product}"


def list_cameras(
    ffmpeg: str = "ffmpeg", platform: str = sys.platform, sysfs: Path = SYSFS_VIDEO
) -> list[str]:
    """Cameras this host can open: names on macOS, capture device nodes on Linux."""
    if platform == "darwin":
        listing = ["-hide_banner", "-nostdin", "-f", "avfoundation", "-list_devices", "true"]
        return parse_avfoundation_devices(_run_ffmpeg([*listing, "-i", ""], ffmpeg).stderr)
    if platform == "linux":
        return linux_capture_nodes(sysfs)
    if platform == "win32":
        listing = ["-hide_banner", "-nostdin", "-list_devices", "true", "-f", "dshow"]
        return parse_dshow_devices(_run_ffmpeg([*listing, "-i", "dummy"], ffmpeg).stderr)
    return []


def matching_cameras(
    names: Sequence[str], model: DeviceModel = HC1500, sysfs: Path = SYSFS_VIDEO
) -> list[str]:
    """The entries that are this model's camera: by name where cameras have names (macOS),
    by USB id where they are device nodes (Linux)."""
    return [
        name
        for name in names
        if name == model.camera_name or usb_id_of(name, sysfs) == model.camera_usb_id
    ]


def choose_camera(
    explicit: str | None,
    names: Sequence[str],
    model: DeviceModel = HC1500,
    sysfs: Path = SYSFS_VIDEO,
) -> str:
    return choose_one(
        explicit,
        matching_cameras(names, model, sysfs),
        what=f"camera named {model.camera_name!r} (USB id {model.camera_usb_id})",
        option="--camera",
        hint="plug the camera in, or name it with --camera",
        seen={"cameras": list(names)},
    )


def parse_stream_size(messages: str) -> tuple[int, int] | None:
    """The frame size ffmpeg reports for the input video stream."""
    match = re.search(r"Stream #.*Video:.*?\b(\d{1,5})x(\d{1,5})\b", messages)
    return (int(match[1]), int(match[2])) if match else None


def measure_fps(arrivals: Sequence[float]) -> float | None:
    """Frame rate from arrival times; what the stream claims about itself is not trusted."""
    if len(arrivals) < 2 or arrivals[-1] <= arrivals[0]:
        return None
    return (len(arrivals) - 1) / (arrivals[-1] - arrivals[0])


def wait_timeout(count: int, fps: float | None) -> float:
    return max(3.0, count / fps + 2.0) if fps else 3.0


class FfmpegSource:
    """A camera stream read through an ffmpeg child process.

    A reader thread keeps only the newest frame. If ffmpeg dies or the camera drops off
    the bus, the stream is reopened with a growing pause and ``generation`` goes up; frame
    sequence numbers keep counting. What ffmpeg prints goes to a temporary file.
    """

    def __init__(
        self,
        camera: str,
        *,
        size: tuple[int, int] = HC1500.default_size,
        fps: float = HC1500.default_fps,
        source: Source = "uncompressed",
        platform: str = sys.platform,
        version: tuple[int, int] | None = None,
        ffmpeg: str = "ffmpeg",
        clock: Clock | None = None,
        color_range: ColorRange = HC1500.yuv_range,
    ) -> None:
        self._platform = platform
        self._color_range: ColorRange = color_range if source == "uncompressed" else "full"
        self._command = capture_command(
            ffmpeg, platform=platform, camera=camera, size=size, fps=fps, source=source,
            version=version,
        )  # fmt: skip
        self._requested_size = size
        self._raw = source == "uncompressed"
        self._clock: Clock = clock or SystemClock()
        self._guard = threading.Lock()  # starting and ending processes
        self._changed = threading.Condition()  # a frame arrived
        self._stopping = threading.Event()
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._log_path: Path | None = None
        self._history = ""  # what ffmpeg said in streams that have ended
        self._spawned_at = 0.0
        self._latest: Frame | None = None
        self._seq = 0
        self._arrivals: deque[float] = deque(maxlen=20)
        self.negotiated_size: tuple[int, int] | None = None
        self.generation = 0

    @property
    def messages(self) -> str:
        """The end of what ffmpeg wrote to stderr, earlier streams of this source included."""
        return (self._history + self._read_log())[-4000:]

    def _read_log(self) -> str:
        if self._log_path is None:
            return ""
        try:
            return self._log_path.read_text("utf-8", "replace")
        except OSError:
            return ""

    @property
    def measured_fps(self) -> float | None:
        with self._changed:
            return measure_fps(list(self._arrivals))

    def latest(self) -> Frame | None:
        return self._latest

    def is_stale(self) -> bool:
        """True when frames stopped: none for 3 s, or none within 15 s of opening."""
        latest = self._latest
        if latest is None or latest.generation != self.generation:
            return self._clock.now() - self._spawned_at > STARTUP_S
        return self._clock.now() - latest.received_at > STALE_S

    def start(self) -> None:
        with self._guard:
            if self._reader is not None:
                raise InvalidStateError("the camera stream is already running")
            self._stopping.clear()
            self._spawn()
            self._reader = threading.Thread(target=self._run, name="vispek-hc-frames", daemon=True)
            self._reader.start()

    def stop(self) -> None:
        self._stopping.set()
        with self._changed:
            self._changed.notify_all()  # whoever waits for frames gets its answer now
        with self._guard:
            self._end(self._process)
            reader, self._reader = self._reader, None
        if reader is not None:
            reader.join()
        with self._guard:
            self._drop_log()

    def wait_new(self, count: int = 1, timeout: float | None = None) -> Frame:
        """Block until ``count`` frames have arrived after this call; return the newest."""
        if count < 1:
            raise ValueError("wait for at least one frame")
        if timeout is None:
            timeout = wait_timeout(count, self.measured_fps)
        with self._changed:
            baseline = self._seq
            self._changed.wait_for(
                lambda: self._seq - baseline >= count or self._stopping.is_set(), timeout
            )
            arrived = self._seq - baseline
            if arrived >= count:
                assert self._latest is not None
                return self._latest
        said = self.messages
        if re.search(r"not authori[sz]ed|permission|denied", said, re.I):
            raise PermissionDeniedError(
                "the operating system refused access to the camera",
                hint="macOS: System Settings > Privacy & Security > Camera, allow your "
                "terminal; Windows: Settings > Privacy > Camera",
                details={"ffmpeg": said[-500:]},
            )
        with self._guard:  # ffmpeg alive but silent: end it, and the reader reopens the stream
            if self.is_stale() and self._process is not None and self._process.poll() is None:
                self._process.kill()
        hint = "check the camera cable; details.ffmpeg holds what ffmpeg reported"
        if self._platform == "darwin" and arrived == 0 and not said.strip():
            # Without camera access macOS delivers no frame and reports nothing.
            hint = (
                "most likely this program may not use the camera: System Settings > "
                "Privacy & Security > Camera, allow your terminal, and run it from that "
                "terminal (a program that cannot ask for access gets no frames and no "
                "error). Otherwise check the camera cable"
            )
        raise FrameIncompleteError(
            f"{arrived} of {count} new frames arrived within {timeout:g} s",
            hint=hint,
            details={"wanted": count, "arrived": arrived, "ffmpeg": said[-500:]},
        )

    # --- internals; _spawn, _end and _drop_log run with self._guard held ---------------

    def _spawn(self) -> None:
        try:
            log_fd, log_name = tempfile.mkstemp(prefix="vispek-hc-ffmpeg-", suffix=".log")
        except OSError as error:
            raise BackendMissingError(f"cannot create a log file for ffmpeg: {error}") from error
        try:
            self._process = subprocess.Popen(
                self._command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,  # parent dies -> pipe breaks -> ffmpeg exits
                stderr=log_fd,
                start_new_session=True,
            )
        except OSError as error:
            os.close(log_fd)  # before the unlink: Windows cannot delete an open file
            with contextlib.suppress(OSError):
                Path(log_name).unlink()
            raise BackendMissingError(
                f"ffmpeg cannot be started: {error}", hint=INSTALL_HINT
            ) from error
        os.close(log_fd)  # the child holds its own copy
        self._drop_log()
        self._log_path = Path(log_name)
        self._spawned_at = self._clock.now()
        self.generation += 1
        with self._changed:
            self._arrivals.clear()

    def _drop_log(self) -> None:
        if self._log_path is not None:
            self._history = (self._history + self._read_log())[-4000:]
            with contextlib.suppress(OSError):
                self._log_path.unlink()
            self._log_path = None

    @staticmethod
    def _end(process: subprocess.Popen[bytes] | None) -> None:
        """Make sure the process is gone: terminate, wait briefly, then kill."""
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _publish(self, data: bytes, size: tuple[int, int]) -> None:
        now = self._clock.now()
        with self._changed:
            self._seq += 1
            self._arrivals.append(now)
            self._latest = Frame(
                self._seq, self.generation, now, self._clock.utc(), size[0], size[1],
                "yuyv422" if self._raw else "mjpeg", data, None, self._color_range,
            )  # fmt: skip
            self._changed.notify_all()

    def _read_stream(self, stdout: IO[bytes]) -> None:
        buffer = bytearray()
        size: tuple[int, int] | None = None
        while chunk := os.read(stdout.fileno(), 1 << 16):
            buffer += chunk
            if not self._raw:
                for picture in split_jpegs(buffer):
                    try:
                        self._publish(picture, jpeg_size(picture))
                    except FrameIncompleteError as error:
                        log.debug("dropping a damaged JPEG: %s", error.message)
                if len(buffer) > MAX_BUFFER:
                    buffer.clear()
                continue
            if size is None:  # ffmpeg reports the stream before it sends the first frame
                reported = parse_stream_size(self._read_log())
                sane = reported is not None and all(0 < side <= MAX_IMAGE_SIDE for side in reported)
                size = self.negotiated_size = (
                    reported if reported and sane else self._requested_size
                )
                if size != self._requested_size:
                    log.warning("camera delivers %dx%d, not the requested size", *size)
            frame_bytes = size[0] * size[1] * 2
            while len(buffer) >= frame_bytes:
                self._publish(bytes(buffer[:frame_bytes]), size)
                del buffer[:frame_bytes]

    def _run(self) -> None:
        pause = RECONNECT_FIRST_S
        while True:
            process = self._process
            assert process is not None
            assert process.stdout is not None
            seen = self._seq
            try:
                self._read_stream(process.stdout)
            except Exception as error:  # whatever went wrong, the cure is to reopen
                log.warning("camera stream broke: %s", error)
            process.stdout.close()
            with self._guard:
                self._end(process)
            if self._seq > seen:
                pause = RECONNECT_FIRST_S
            while True:
                if self._stopping.is_set():
                    return
                last_words = (self._read_log().strip().splitlines() or ["no message"])[-1]
                log.warning("camera stream ended (%s); reopening in %g s", last_words, pause)
                if self._stopping.wait(pause):
                    return
                pause = min(pause * 2, RECONNECT_MAX_S)
                with self._guard:
                    if self._stopping.is_set():
                        return
                    try:
                        self._spawn()
                        break
                    except Exception as error:  # keep trying: a dead reader cannot be revived
                        log.warning("camera stream cannot be reopened: %s", error)
