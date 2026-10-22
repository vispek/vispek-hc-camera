# SPDX-License-Identifier: Apache-2.0
"""Windows COM bridge using the standard library, without import-time COM setup.

Interface identifiers, vtable slots and units follow strmif.h and objidl.h.
Every returned interface owns one reference, released before its apartment ends.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from ctypes import (
    POINTER,
    Structure,
    Union,
    byref,
    c_int32,
    c_int64,
    c_ubyte,
    c_uint16,
    c_uint32,
    c_void_p,
    c_wchar_p,
    cast,
    wstring_at,
)
from types import TracebackType
from typing import TYPE_CHECKING, Any, Self, TypeVar
from uuid import UUID

if sys.platform == "win32":
    from ctypes import WINFUNCTYPE, WinDLL
elif TYPE_CHECKING:
    # Equivalent call signatures let other hosts type-check this Windows-only module.
    from ctypes import CDLL as WinDLL
    from ctypes import CFUNCTYPE as WINFUNCTYPE
else:
    raise ImportError("DirectShow controls require Windows")

from vispek_hc._windows_controls import PROPERTIES, NativeControl
from vispek_hc.errors import (
    AmbiguousDeviceError,
    CameraControlFailedError,
    ControlsUnavailableError,
    DeviceNotFoundError,
)

T = TypeVar("T")
HRESULT = LONG = c_int32
ULONG = c_uint32
_UNSUPPORTED = {0x80004001, 0x80070032, 0x80070057, 0x80070490, 0x80070492}


class GUID(Structure):
    _fields_ = (
        ("data1", c_uint32),
        ("data2", c_uint16),
        ("data3", c_uint16),
        ("data4", c_ubyte * 8),
    )


def _guid(value: str) -> GUID:
    return GUID.from_buffer_copy(UUID(value).bytes_le)


_CATEGORY = _guid("860BB310-5D01-11D0-BD3B-00A0C911CE86")
_ENUMERATOR = _guid("62BE5D10-60EB-11D0-BD3B-00A0C911CE86")
_CREATE_ENUM = _guid("29840822-5B84-11D0-BD3B-00A0C911CE86")
_UNKNOWN = _guid("00000000-0000-0000-C000-000000000046")
_PROPERTY_BAG = _guid("55272A00-42CB-11CE-8135-00AA004BB851")
_INTERFACES = {
    "camera": _guid("C6E13370-30AC-11D0-A18C-00A0C9118956"),
    "video": _guid("C6E13360-30AC-11D0-A18C-00A0C9118956"),
}


class _Payload(Union):
    # BRECORD has two pointers: VARIANT is 24 bytes on x64, 16 on x86.
    _fields_ = (("integer", c_int64), ("bstr", c_void_p), ("record", c_void_p * 2))


class VARIANT(Structure):
    _fields_ = (("kind", c_uint16), ("reserved", c_uint16 * 3), ("payload", _Payload))


_ole32 = WinDLL("ole32")
_oleaut32 = WinDLL("oleaut32")
_ole32.CoInitializeEx.argtypes = [c_void_p, ULONG]
_ole32.CoInitializeEx.restype = HRESULT
_ole32.CoUninitialize.argtypes = []
_ole32.CoUninitialize.restype = None
_ole32.CoCreateInstance.argtypes = [
    POINTER(GUID),
    c_void_p,
    ULONG,
    POINTER(GUID),
    POINTER(c_void_p),
]
_ole32.CoCreateInstance.restype = HRESULT
_oleaut32.VariantClear.argtypes = [POINTER(VARIANT)]
_oleaut32.VariantClear.restype = HRESULT


class _ComError(Exception):
    def __init__(self, hresult: int) -> None:
        self.hresult = hresult & 0xFFFFFFFF
        super().__init__(f"COM error 0x{self.hresult:08X}")


def _check(result: int) -> None:
    if result < 0:
        raise _ComError(result)


class _Pointer:
    def __init__(self, value: c_void_p) -> None:
        self.value = value

    def call(self, slot: int, types: list[Any], *arguments: Any) -> int:
        if not self.value:
            raise CameraControlFailedError("Windows returned an empty COM interface")
        table = cast(self.value, POINTER(POINTER(c_void_p))).contents
        method = WINFUNCTYPE(HRESULT, c_void_p, *types)(table[slot])
        result: int = method(self.value, *arguments)
        _check(result)
        return result

    def query(self, interface: GUID) -> _Pointer:
        value = c_void_p()
        self.call(0, [POINTER(GUID), POINTER(c_void_p)], byref(interface), byref(value))
        return _Pointer(value)

    def bind(self, slot: int, interface: GUID) -> _Pointer:
        value = c_void_p()
        self.call(
            slot,
            [c_void_p, c_void_p, POINTER(GUID), POINTER(c_void_p)],
            None,
            None,
            byref(interface),
            byref(value),
        )
        return _Pointer(value)

    def detach(self) -> _Pointer:
        result = _Pointer(self.value)
        self.value = c_void_p()
        return result

    def close(self) -> None:
        if self.value:
            table = cast(self.value, POINTER(POINTER(c_void_p))).contents
            release = WINFUNCTYPE(ULONG, c_void_p)(table[2])
            release(self.value)
            self.value = c_void_p()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


@contextmanager
def _apartment() -> Iterator[None]:
    result = _ole32.CoInitializeEx(None, 0)
    if result & 0xFFFFFFFF != 0x80010106:
        _check(result)
    try:
        yield
    finally:
        # S_OK and S_FALSE both add a reference. An existing STA adds none.
        if result >= 0:
            _ole32.CoUninitialize()


def _property(moniker: _Pointer, name: str) -> str:
    with moniker.bind(9, _PROPERTY_BAG) as bag:
        value = VARIANT()
        try:
            bag.call(3, [c_wchar_p, POINTER(VARIANT), c_void_p], name, byref(value), None)
            if value.kind != 8 or not value.payload.bstr:
                raise CameraControlFailedError(f"camera property {name} is not a string")
            return wstring_at(value.payload.bstr)
        finally:
            _check(_oleaut32.VariantClear(byref(value)))


def _find(device: str) -> tuple[_Pointer, str]:
    with ExitStack() as resources:
        value = c_void_p()
        _check(
            _ole32.CoCreateInstance(byref(_ENUMERATOR), None, 1, byref(_CREATE_ENUM), byref(value))
        )
        system = resources.enter_context(_Pointer(value))
        value = c_void_p()
        system.call(3, [POINTER(GUID), POINTER(c_void_p), ULONG], byref(_CATEGORY), byref(value), 0)
        enumerator = resources.enter_context(_Pointer(value))
        matches = []
        if enumerator.value:
            while True:
                value, fetched = c_void_p(), ULONG()
                result = enumerator.call(
                    3, [ULONG, POINTER(c_void_p), POINTER(ULONG)], 1, byref(value), byref(fetched)
                )
                moniker = resources.enter_context(_Pointer(value))
                if result == 1 or not fetched.value or not value:
                    break
                name = _property(moniker, "FriendlyName")
                try:
                    path = _property(moniker, "DevicePath")
                except _ComError:
                    if name != device:
                        continue
                    raise
                if device in (name, path):
                    matches.append((moniker, path))
        if not matches:
            raise DeviceNotFoundError(f"the DirectShow camera {device!r} is not connected")
        if len(matches) != 1:
            raise AmbiguousDeviceError(f"more than one DirectShow camera is named {device!r}")
        moniker, path = matches[0]
        return moniker.detach(), path


class NativeCamera:
    """Only a device path is retained; no native pointers survive a method call."""

    def __init__(self, device: str) -> None:
        self._device = device
        self._path = self._call(lambda source: self._path)
        self.device_id = f"directshow:{self._path}"

    def _call(self, action: Callable[[_Pointer], T]) -> T:
        try:
            with _apartment(), ExitStack() as resources:
                moniker, self._path = _find(getattr(self, "_path", self._device))
                resources.enter_context(moniker)
                source = resources.enter_context(moniker.bind(8, _UNKNOWN))
                return action(source)
        except _ComError as error:
            raise CameraControlFailedError(
                "Windows could not access the camera controls",
                hint="close other camera applications and check the camera's "
                "desktop-app permission",
                details={"hresult": f"0x{error.hresult:08X}"},
            ) from None

    def read_all(self) -> dict[str, NativeControl]:
        def read(source: _Pointer) -> dict[str, NativeControl]:
            result = {}
            for kind, interface in _INTERFACES.items():
                with ExitStack() as resources:
                    try:
                        control = resources.enter_context(source.query(interface))
                    except _ComError as error:
                        if error.hresult == 0x80004002:
                            continue
                        raise
                    for name, (group, number) in PROPERTIES.items():
                        if group != kind:
                            continue
                        limits = [LONG() for _ in range(5)]
                        try:
                            control.call(
                                3, [LONG, *[POINTER(LONG)] * 5], number, *[byref(v) for v in limits]
                            )
                        except _ComError as error:
                            if error.hresult in _UNSUPPORTED:
                                continue
                            raise
                        value, flags = LONG(), LONG()
                        control.call(
                            5,
                            [LONG, POINTER(LONG), POINTER(LONG)],
                            number,
                            byref(value),
                            byref(flags),
                        )
                        minimum, maximum, step, default, caps = [v.value for v in limits]
                        result[name] = NativeControl(
                            value.value, minimum, maximum, step, default, caps, flags.value
                        )
            if not result:
                raise ControlsUnavailableError("the camera exposes no DirectShow controls")
            return result

        return self._call(read)

    def write(self, name: str, value: int, flags: int) -> None:
        def write(source: _Pointer) -> None:
            kind, number = PROPERTIES[name]
            with source.query(_INTERFACES[kind]) as control:
                control.call(4, [LONG, LONG, LONG], number, value, flags)

        self._call(write)
