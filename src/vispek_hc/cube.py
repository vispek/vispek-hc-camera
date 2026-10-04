# SPDX-License-Identifier: Apache-2.0
"""The data: cubes in memory, capture folders on disk, and exports.

A capture folder holds the source frames exactly as recorded plus ``capture.json`` and an
append-only ``events.jsonl``. Everything derived (scalars, masks, averages) is recomputed
from the source frames when a capture is loaded. Axis order is always Y, X, band.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import shutil
import threading
import time
import uuid
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Literal, Self

import numpy as np
import PIL
from numpy.typing import NDArray
from PIL import Image, features

from vispek_hc import __version__
from vispek_hc.errors import (
    ConfigRejectedError,
    FrameNotFoundError,
    InvalidStateError,
    SchemaUnsupportedError,
)
from vispek_hc.framecheck import MaskBit, Reduction, clip_mask, reduce
from vispek_hc.interfaces import Frame
from vispek_hc.models import HC1500, Channel, DeviceModel, Led
from vispek_hc.video import MAX_IMAGE_SIDE, decode

ReductionName = Literal["dominant", "luma"]  # how a band's RGB becomes one value per pixel
CAPTURE_SCHEMA = "vispek-hc.capture/1"
CUBE_SCHEMA = "vispek-hc.cube/1"
SIGNAL_DOMAINS = {"dominant": "dominant_channel_v1", "luma": "luma_bt601_v1"}
INVALID = MaskBit.INVALID | MaskBit.SATURATED | MaskBit.SMALL_DENOMINATOR


@dataclass(frozen=True, slots=True)
class Band:
    band_id: str  # "led_NN": the stable key; the wavelength is only a label
    name: str
    center_nm: int  # nominal LED centre wavelength, not a sensor response
    dominant_channel: Channel
    rgb_white_median: tuple[float, float, float] | None = None


@dataclass(slots=True)
class Cube:
    rgb: NDArray[np.uint8]  # (H, W, B, 3) raw code values: the base data
    dark_rgb: NDArray[np.uint8]  # (H, W, 3) the group's own dark frame
    values: NDArray[np.float32]  # (H, W, B) one scalar per band, see metadata["reduction"]
    mask: NDArray[np.uint8]  # (H, W, B) MaskBit flags
    bands: list[Band]
    metadata: dict[str, Any]

    @property
    def valid(self) -> NDArray[np.bool_]:
        """Pixels that count in statistics: finite, and not invalid, saturated or undivided."""
        valid: NDArray[np.bool_] = np.isfinite(self.values) & ((self.mask & INVALID) == 0)
        return valid

    def band_index(self, band_id: str) -> int:
        ids = [band.band_id for band in self.bands]
        if band_id not in ids:
            raise ConfigRejectedError(
                f"this cube has no band {band_id}", details={"band_id": band_id, "available": ids}
            )
        return ids.index(band_id)

    def save(self, path: str | Path) -> Path:
        """Write ``cube.npz`` (the suffix is added if missing). Plain numpy, no pickle."""
        path = Path(path)
        path = path if path.suffix == ".npz" else path.with_name(path.name + ".npz")
        np.savez_compressed(
            path,
            schema=CUBE_SCHEMA,
            rgb=self.rgb,
            dark_rgb=self.dark_rgb,
            values=self.values,
            mask=self.mask,
            band_ids=np.array([band.band_id for band in self.bands]),
            wavelengths_nm=np.array([band.center_nm for band in self.bands], np.float32),
            dominant_channel=np.array([band.dominant_channel for band in self.bands]),
            axes="YXB",
            layer=self.metadata["layer"],
            unit=self.metadata["unit"],
            signal_domain=self.metadata["signal_domain"],
            reduction=self.metadata["reduction"],
            metadata_json=json.dumps(describe(self)),
        )
        return path

    @classmethod
    def load(cls, path: str | Path) -> Self:
        """Read ``cube.npz``. Unknown fields are ignored; pickled content is refused."""
        try:
            with np.load(path, allow_pickle=False) as stored:
                if str(stored["schema"]) != CUBE_SCHEMA:
                    raise SchemaUnsupportedError(
                        f"{Path(path).name} has schema {stored['schema']}, expected {CUBE_SCHEMA}"
                    )
                metadata = json.loads(str(stored["metadata_json"]))
                bands = []
                for band in metadata.pop("bands"):
                    white = band.get("rgb_white_median")
                    bands.append(
                        Band(
                            band["band_id"],
                            band["name"],
                            band["center_nm"],
                            band["dominant_channel"],
                            tuple(white) if white else None,
                        )
                    )
                return cls(
                    stored["rgb"],
                    stored["dark_rgb"],
                    stored["values"].astype(np.float32),
                    stored["mask"],
                    bands,
                    metadata,
                )
        except (ValueError, KeyError, EOFError, zipfile.BadZipFile) as error:
            raise SchemaUnsupportedError(
                f"{Path(path).name} is not a readable cube file: {type(error).__name__}"
            ) from error


def _plain(value: Any) -> Any:
    """Make metadata strict JSON: numpy scalars to numbers, non-finite numbers to null."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    if isinstance(value, np.generic):
        value = value.item()
    if isinstance(value, float) and not np.isfinite(value):
        return None
    return value


def describe(cube: Cube) -> dict[str, Any]:
    """A cube's metadata with its bands, as it is written into files."""
    bands = [
        {**asdict(band), "fwhm_nm": None, "source": "nominal_LED_not_sensor_response"}
        for band in cube.bands
    ]
    described: dict[str, Any] = _plain({**cube.metadata, "bands": bands})
    return described


def build_cube(
    frames: Sequence[NDArray[np.uint8]],
    dark_rgb: NDArray[np.uint8],
    leds: Sequence[Led],
    reduction: ReductionName,
    metadata: Mapping[str, Any],
    dominant_by_led: Mapping[int, Channel] | None = None,
    model: DeviceModel = HC1500,
) -> Cube:
    """Stack one RGB frame per LED into an L1 cube (raw code values, dark not subtracted)."""
    if len(frames) != len(leds):
        raise ValueError(f"need one frame per LED: {len(frames)} frames, {len(leds)} LEDs")
    if any(frame.shape != dark_rgb.shape for frame in frames):
        raise ValueError("frames and dark frame differ in size")
    channels = [(dominant_by_led or {}).get(led.led_id, led.dominant_channel) for led in leds]
    scalars: list[Reduction] = [c if reduction == "dominant" else "luma" for c in channels]
    return Cube(
        rgb=np.stack(frames, axis=2),
        dark_rgb=dark_rgb,
        values=np.stack(
            [reduce(f, s) for f, s in zip(frames, scalars, strict=True)], axis=2
        ).astype(np.float32),
        mask=np.stack([clip_mask(f, s) for f, s in zip(frames, scalars, strict=True)], axis=2),
        bands=[
            Band(led.band_id, led.name, led.nm, channel)
            for led, channel in zip(leds, channels, strict=True)
        ],
        metadata={
            "layer": "L1",
            "unit": "image_code_value",
            "signal_domain": SIGNAL_DOMAINS[reduction],
            "reduction": reduction,
            "axes": "YXB",
            "synchronization": "sequential_not_simultaneous",
            "registration": "not_verified",
            "led_table": {"version": model.led_table.version, "verified": model.led_table.verified},
            "software": {"name": "vispek-hc", "version": __version__},
            **metadata,
        },
    )


# --- capture folders -------------------------------------------------------------------


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _write_atomically(path: Path, data: bytes) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def check_target(directory: Path, overwrite: bool) -> None:
    """Refuse a capture folder that is in use. ``overwrite`` only ever replaces a capture.

    A non-empty folder that does not look like a capture is never deleted, whatever the
    flag says: a mistyped ``-o`` must not cost anybody their files.
    """
    if directory.is_symlink():
        raise InvalidStateError(f"{directory.name} is a link; give the folder itself")
    if directory.is_file():
        raise InvalidStateError(f"{directory.name} is a file, not a folder")
    if not directory.exists() or all(entry.name == ".DS_Store" for entry in directory.iterdir()):
        return
    is_capture = (directory / "capture.json").exists() or (directory / "events.jsonl").exists()
    if not is_capture:
        raise InvalidStateError(
            f"{directory.name} is not empty and is not a capture folder; it is left alone",
            hint="choose a new folder name",
        )
    if not overwrite:
        raise InvalidStateError(
            f"{directory.name} already holds a capture",
            hint="choose a new folder, or pass --overwrite to replace that capture",
        )


class CaptureWriter:
    """Writes one capture folder while recording, so that a crash loses nothing recorded.

    Frames go to disk at once (temporary file, then rename). ``capture.json`` is rewritten
    atomically after every recorded frame with ``status: partial`` and only the final
    :meth:`close` marks it complete. ``events.jsonl`` is appended and flushed per event.
    """

    def __init__(
        self,
        directory: str | Path,
        header: Mapping[str, Any],
        *,
        overwrite: bool = False,
        model: DeviceModel = HC1500,
    ) -> None:
        directory = Path(directory)
        check_target(directory, overwrite)
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True, exist_ok=True)
        self.directory = directory
        reduction = header.get("reduction", "dominant")
        self.document: dict[str, Any] = {
            "schema": CAPTURE_SCHEMA,
            "capture_id": str(uuid.uuid4()),
            "software": {
                "name": "vispek-hc",
                "version": __version__,
                "decoder": {
                    "library": "Pillow",
                    "version": PIL.__version__,
                    "libjpeg_turbo": features.version("libjpeg_turbo"),
                },
            },
            "model": model.name,
            "led_table": {"version": model.led_table.version, "verified": model.led_table.verified},
            "signal_domain": SIGNAL_DOMAINS[reduction],
            "groups": [],
            "notes": "",
            **header,
            "recording": {
                **header.get("recording", {}),
                "started_at_utc": _stamp(datetime.now(UTC)),
                "finished_at_utc": None,
                "status": "partial",
                "groups_done": 0,
            },
        }
        self._events = (directory / "events.jsonl").open("a", encoding="utf-8", newline="\n")
        self._events_lock = threading.Lock()  # the scan and its disk thread both log
        try:
            self.event("recording_started", document=self.document)
            self._save()
        except BaseException:
            self._events.close()
            raise

    def event(self, kind: str, **fields: Any) -> None:
        line = {
            "t_utc": _stamp(datetime.now(UTC)),
            "t_mono_ns": time.monotonic_ns(),
            "type": kind,
            **fields,
        }
        with self._events_lock:
            self._events.write(json.dumps(line, default=str) + "\n")
            self._events.flush()

    def write_frame(self, name: str, frame: Frame) -> dict[str, Any]:
        """Store one frame losslessly as ``<name>.png`` (or the camera's ``.jpg`` bytes).

        Returns its file entry; the hash is of the bytes that went to disk.
        """
        if frame.pixel_format == "mjpeg":
            data, suffix, encoding = frame.data, ".jpg", "jpeg"
        else:
            buffer = io.BytesIO()
            Image.fromarray(decode(frame)).save(buffer, "PNG", compress_level=1)
            data, suffix, encoding = buffer.getvalue(), ".png", "png"
        path = self.directory / (name + suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        _write_atomically(path, data)
        return {
            "file": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "encoding": encoding,
            "width": frame.width,
            "height": frame.height,
            "host_frame_counter": frame.seq,
            "received_at_utc": _stamp(frame.received_utc),
            "received_mono_ns": round(frame.received_at * 1e9),
        }

    def record_single(self, entry: Mapping[str, Any]) -> None:
        """Set the one frame of a ``single`` or ``dark`` capture."""
        self.document["frame"] = dict(entry)
        self.event("frame_selected", group_id=None, role="single", **entry)
        self._save()

    def _group(self, group_id: str) -> dict[str, Any]:
        groups: list[dict[str, Any]] = self.document["groups"]
        if not groups or groups[-1]["group_id"] != group_id:
            groups.append(
                {"group_id": group_id, "status": "partial", "failure": None, "dark": None,
                 "frames": []}
            )  # fmt: skip
        return groups[-1]

    def record_frame(self, group_id: str, entry: Mapping[str, Any], *, dark: bool = False) -> None:
        """Add a selected frame (dark frame or LED frame) to its group and save."""
        group = self._group(group_id)
        if dark:
            group["dark"] = dict(entry)
        else:
            group["frames"].append(dict(entry))
        self.event("frame_selected", group_id=group_id, role="dark" if dark else "led", **entry)
        self._save()

    def finish_group(
        self, group_id: str, status: str = "complete", failure: Mapping[str, Any] | None = None
    ) -> None:
        group = self._group(group_id)
        group["status"], group["failure"] = status, failure
        self.document["recording"]["groups_done"] = sum(
            each["status"] == "complete" for each in self.document["groups"]
        )
        self.event("group_finished", group_id=group_id, status=status, failure=failure)
        self._save()

    def _save(self) -> None:
        text = json.dumps(self.document, indent=1, default=str)
        _write_atomically(self.directory / "capture.json", text.encode("utf-8"))

    def close(self, complete: bool = True) -> None:
        if complete:
            self.document["recording"]["status"] = "complete"
        self.document["recording"]["finished_at_utc"] = _stamp(datetime.now(UTC))
        try:
            self._save()
        finally:
            self._events.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(complete=exc_type is None)


@dataclass(slots=True)
class Capture:
    """A capture folder that was read back. ``document`` is the content of capture.json."""

    path: Path
    document: dict[str, Any]

    @property
    def capture_id(self) -> str:
        return str(self.document["capture_id"])

    @property
    def mode(self) -> str:
        return str(self.document["recording"]["mode"])

    @property
    def status(self) -> str:
        return str(self.document["recording"]["status"])

    @property
    def unit_id(self) -> str:
        return str(self.document.get("unit_id", ""))

    @property
    def groups(self) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = self.document["groups"]
        return groups

    @property
    def average(self) -> int:
        return int(self.document["recording"].get("average", 1))

    @property
    def outputs(self) -> int:
        """How many cubes this capture holds: complete groups, ``average`` at a time."""
        return int(sum(group["status"] == "complete" for group in self.groups) // self.average)

    def frame_path(self, group_id: str, entry: Mapping[str, Any]) -> Path:
        """Where a frame entry's file is. Names that leave the capture folder are refused."""
        name = str(entry.get("file", ""))
        if not name or Path(name).name != name or name in (".", ".."):
            raise ConfigRejectedError(
                "a frame entry names a file outside its folder", details={"file": name}
            )
        if not isinstance(group_id, str) or not _GROUP_FOLDER.fullmatch(group_id):
            raise ConfigRejectedError(
                "a group names a folder outside the capture", details={"group_id": group_id}
            )
        path = self.path / group_id / name
        if self.path.resolve() not in path.resolve().parents:
            raise ConfigRejectedError(
                "a frame file lies outside the capture folder", details={"file": name}
            )
        return path

    def picture(self, group_id: str, entry: Mapping[str, Any]) -> NDArray[np.uint8]:
        path = self.frame_path(group_id, entry)
        try:
            with Image.open(path) as image:
                if max(image.size) > MAX_IMAGE_SIDE:
                    raise ConfigRejectedError(f"{path.name} is larger than supported")
                return np.asarray(image.convert("RGB"))
        except (OSError, Image.DecompressionBombError) as error:
            reason = getattr(error, "strerror", None) or type(error).__name__
            raise FrameNotFoundError(
                f"cannot read frame {group_id}/{path.name}: {reason}"
            ) from error

    def frame(self) -> NDArray[np.uint8]:
        """The picture of a single-frame capture (modes ``single`` and ``dark``)."""
        if "frame" not in self.document:
            raise FrameNotFoundError(f"{self.path.name} is a {self.mode} capture, not one frame")
        return self.picture("", self.document["frame"])

    def _entries(self) -> list[tuple[str, dict[str, Any]]]:
        """Every frame entry of the capture with the folder it lives in."""
        entries = [
            (group["group_id"], entry)
            for group in self.groups
            for entry in [group["dark"], *group["frames"]]
            if entry
        ]
        if "frame" in self.document:
            entries.append(("", self.document["frame"]))
        index = self.document.get("frames_index")
        if index:
            if not isinstance(index, str) or Path(index).name != index:
                raise ConfigRejectedError(
                    "the frame index is named outside the capture folder",
                    details={"frames_index": index},
                )
            entries += [("frames", entry) for entry in _json_lines(self.path / index)]
        return entries

    def verify(self) -> None:
        """Check every listed frame against its recorded sha256."""
        for folder, entry in self._entries():
            path = self.frame_path(folder, entry)
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as error:
                raise FrameNotFoundError(
                    f"{folder}/{path.name} is missing or unreadable"
                ) from error
            if digest != entry.get("sha256"):
                raise FrameNotFoundError(f"{folder}/{path.name} does not match its recorded sha256")

    def cube(
        self,
        output: int = 0,
        reduction: ReductionName | None = None,
        dominant_by_led: Mapping[int, Channel] | None = None,
    ) -> Cube:
        """Build the L1 cube of one output from its source frames.

        With ``average`` above 1 the groups of the output are averaged one at a time
        (values as means, RGB and dark frame as means rounded half up, masks merged), so
        memory stays at one cube whatever the number of scans.
        """
        if self.mode != "scan":
            raise FrameNotFoundError(f"{self.path.name} is a {self.mode} capture; it holds no scan")
        complete = [group for group in self.groups if group["status"] == "complete"]
        chosen = complete[output * self.average : (output + 1) * self.average]
        if output < 0 or len(chosen) < self.average:
            raise FrameNotFoundError(
                f"capture has {self.outputs} complete output(s), there is no number {output}",
                details={"outputs": self.outputs},
            )
        document = self.document
        first: Cube | None = None
        rgb_sum = values_sum = dark_sum = None
        for group in chosen:
            group_id = group["group_id"]
            cube = build_cube(
                [self.picture(group_id, entry) for entry in group["frames"]],
                self.picture(group_id, group["dark"]),
                [HC1500.led(entry["led_id"]) for entry in group["frames"]],
                reduction or document.get("reduction", "dominant"),
                {
                    "capture_id": self.capture_id,
                    "unit_id": self.unit_id,
                    "group_id": group_id,
                    "config": document.get("config", {}),
                    "acquired_at_utc": group["dark"]["received_at_utc"],
                },
                dominant_by_led,
            )
            if first is None:
                first = cube
                if len(chosen) > 1:
                    rgb_sum, values_sum = cube.rgb.astype(np.float32), cube.values.copy()
                    dark_sum = cube.dark_rgb.astype(np.float32)
                continue
            if [band.band_id for band in cube.bands] != [band.band_id for band in first.bands]:
                raise InvalidStateError("the scans of this output do not have the same channels")
            assert rgb_sum is not None
            assert values_sum is not None
            assert dark_sum is not None
            rgb_sum += cube.rgb
            values_sum += cube.values
            dark_sum += cube.dark_rgb
            first.mask |= cube.mask
        assert first is not None
        if rgb_sum is not None and values_sum is not None and dark_sum is not None:
            first.rgb = np.floor(rgb_sum / len(chosen) + 0.5).astype(np.uint8)
            first.dark_rgb = np.floor(dark_sum / len(chosen) + 0.5).astype(np.uint8)
            first.values = (values_sum / np.float32(len(chosen))).astype(np.float32)
        first.metadata["group_ids"] = [group["group_id"] for group in chosen]
        return first


_GROUP_FOLDER = re.compile(r"(g\d{4,}|frames|)")


def load_capture(directory: str | Path, verify: bool = False) -> Capture:
    """Read a capture folder. ``verify`` also checks every frame's sha256."""
    directory = Path(directory)
    try:
        document = json.loads((directory / "capture.json").read_text("utf-8"))
    except (FileNotFoundError, NotADirectoryError) as error:
        raise FrameNotFoundError(
            f"{directory.name} has no capture.json",
            hint="a recording interrupted before its first frame leaves none; if events.jsonl "
            "exists, 'vispek-hc repair' rebuilds it",
        ) from error
    except ValueError as error:
        raise SchemaUnsupportedError(
            f"capture.json in {directory.name} is damaged",
            hint="'vispek-hc repair' rebuilds it from events.jsonl",
        ) from error
    if not isinstance(document, dict) or document.get("schema") != CAPTURE_SCHEMA:
        schema = document.get("schema") if isinstance(document, dict) else None
        raise SchemaUnsupportedError(
            f"capture has schema {schema!r}, expected {CAPTURE_SCHEMA!r}",
            details={"schema": schema},
        )
    capture = Capture(directory, document)
    if verify:
        capture.verify()
    return capture


def _json_lines(path: Path) -> list[dict[str, Any]]:
    """The JSON objects of a .jsonl file. A line cut short by a crash is skipped."""
    try:
        lines = path.read_text("utf-8").splitlines()
    except (FileNotFoundError, NotADirectoryError) as error:
        raise FrameNotFoundError(f"{path.parent.name} has no {path.name}") from error
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def read_events(directory: str | Path) -> list[dict[str, Any]]:
    """The events of a capture, oldest first (``events.jsonl``)."""
    return [event for event in _json_lines(Path(directory) / "events.jsonl") if "type" in event]


def repair(directory: str | Path) -> Capture:
    """Rebuild ``capture.json`` from ``events.jsonl`` and the frame files that exist.

    The result is always ``partial``. The ``capture.json`` found by the first repair is
    kept as ``capture.json.bak``; later repairs leave that backup alone.
    """
    directory = Path(directory)
    events = read_events(directory)
    started = [event for event in events if event["type"] == "recording_started"]
    if not started:
        raise FrameNotFoundError(f"events.jsonl in {directory.name} does not start a recording")
    document: dict[str, Any] = started[0]["document"]
    probe = Capture(directory, document)
    groups: dict[str, dict[str, Any]] = {}
    lost: set[str] = set()
    for event in events:
        group_id = event.get("group_id")
        if event["type"] == "frame_selected":
            entry = {key: value for key, value in event.items() if key not in _EVENT_KEYS}
            folder = group_id or ""
            if not probe.frame_path(folder, entry).is_file():  # also refuses odd names
                lost.add(folder)
            elif group_id is None:
                document["frame"] = entry
            else:
                group = groups.setdefault(
                    group_id, {"group_id": group_id, "status": "partial", "failure": None,
                               "dark": None, "frames": []},
                )  # fmt: skip
                if event["role"] == "dark":
                    group["dark"] = entry
                else:
                    group["frames"].append(entry)
        elif event["type"] == "group_finished" and group_id in groups:
            groups[group_id]["status"] = "partial" if group_id in lost else event["status"]
            groups[group_id]["failure"] = event["failure"]
    document["groups"] = [group for group in groups.values() if group["dark"]]
    if (directory / "frames.jsonl").is_file():
        document["frames_index"] = "frames.jsonl"
    document["recording"]["status"] = "partial"
    document["recording"]["groups_done"] = sum(
        group["status"] == "complete" for group in document["groups"]
    )
    target = directory / "capture.json"
    backup = directory / "capture.json.bak"
    if target.exists() and not backup.exists():
        target.replace(backup)
    _write_atomically(target, json.dumps(document, indent=1).encode("utf-8"))
    return Capture(directory, document)


_EVENT_KEYS = {"t_utc", "t_mono_ns", "type", "group_id", "role"}


# --- export ----------------------------------------------------------------------------

_TRUE_COLOUR = ("led_09", "led_07", "led_05")  # 630 / 560 / 455 nm


def _envi_header(cube: Cube, data_type: int, shape: tuple[int, int, int]) -> str:
    height, width, count = shape
    ids = [band.band_id for band in cube.bands]
    meta = cube.metadata
    lines = [
        "ENVI",
        f"description = {{vispek-hc capture {meta.get('capture_id', 'unknown')}, "
        f"layer {meta['layer']}, unit {meta['unit']}}}",
        f"samples = {width}",
        f"lines = {height}",
        f"bands = {count}",
        "header offset = 0",
        "file type = ENVI Standard",
        f"data type = {data_type}",
        "interleave = bsq",
        "byte order = 0",
        f"band names = {{{', '.join(ids)}}}",
        "wavelength units = Nanometers",
        f"wavelength = {{{', '.join(str(band.center_nm) for band in cube.bands)}}}",
    ]
    if all(band_id in ids for band_id in _TRUE_COLOUR):
        positions = ", ".join(str(ids.index(band_id) + 1) for band_id in _TRUE_COLOUR)
        lines.append(f"default bands = {{{positions}}}")
    return "\n".join(lines) + "\n"


def export_envi(cube: Cube, directory: str | Path) -> list[Path]:
    """Write ENVI Standard files: float32 data, uint8 quality mask, and the metadata."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for stem, array, data_type in (
        ("data", cube.values.astype("<f4"), 4),
        ("quality-mask", cube.mask.astype(np.uint8), 1),
    ):
        sequential = np.ascontiguousarray(array.transpose(2, 0, 1))  # band sequential
        data_path = directory / f"{stem}.bsq"
        sequential.tofile(data_path)
        if not np.array_equal(
            np.fromfile(data_path, sequential.dtype).reshape(sequential.shape),
            sequential,
            equal_nan=array.dtype.kind == "f",
        ):
            raise InvalidStateError(f"{data_path.name} did not read back as written")
        header_path = directory / f"{stem}.hdr"
        header_path.write_text(_envi_header(cube, data_type, cube.values.shape), "ascii")
        written += [data_path, header_path]
    metadata_path = directory / "metadata.json"
    metadata_path.write_text(json.dumps(describe(cube), indent=1), "utf-8")
    return [*written, metadata_path]


def export_tiff(cube: Cube, directory: str | Path) -> tuple[Path, Path]:
    """Write a multi-page float32 TIFF (one page per band) and a mask TIFF beside it."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    data_path, mask_path = directory / "data.tif", directory / "quality-mask.tif"
    description = json.dumps({**describe(cube), "mask_file": mask_path.name})
    for path, array, info in (
        (data_path, cube.values.astype(np.float32), {270: description}),
        (mask_path, cube.mask, {}),
    ):
        pages = [Image.fromarray(array[:, :, index]) for index in range(array.shape[2])]
        pages[0].save(path, save_all=True, append_images=pages[1:], tiffinfo=info)
    return data_path, mask_path
