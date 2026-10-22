# SPDX-License-Identifier: Apache-2.0
"""Exercise the native ABI with fake vtables; never open a camera or serial port."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows native COM bindings")


class Interface:
    """Own callback and vtable storage for an in-process fake COM object."""

    def __init__(self) -> None:
        import ctypes as c

        self.callbacks: list[Any] = []
        self.table = (c.c_void_p * 10)()
        self.data = (c.c_void_p * 1)(c.cast(self.table, c.c_void_p).value)
        self.value = c.cast(self.data, c.c_void_p)
        self.releases = 0

        def release(this: Any) -> int:
            self.releases += 1
            return 0

        self.method(2, c.c_uint32, [], release)

    def method(self, slot: int, result: Any, arguments: list[Any], action: Any) -> None:
        import ctypes as c

        callback = c.WINFUNCTYPE(result, c.c_void_p, *arguments)(action)
        self.callbacks.append(callback)
        self.table[slot] = c.cast(callback, c.c_void_p).value


def test_native_layout_uses_windows_widths() -> None:
    import ctypes as c

    from vispek_hc import _directshow as native

    assert c.sizeof(native.LONG) == 4
    assert c.sizeof(native.GUID) == 16
    assert c.sizeof(native.VARIANT) == (24 if c.sizeof(c.c_void_p) == 8 else 16)
    assert native.VARIANT.payload.offset == 8
    assert native._guid("12345678-1122-3344-5566-778899AABBCC").data1 == 0x12345678


@pytest.mark.parametrize("result", [0, 1, -2147417850])
def test_apartment_balances_only_its_own_initialization(
    monkeypatch: pytest.MonkeyPatch, result: int
) -> None:
    from vispek_hc import _directshow as native

    events = []
    api = SimpleNamespace(
        CoInitializeEx=lambda *a: result,
        CoUninitialize=lambda: events.append("uninit"),
    )
    monkeypatch.setattr(native, "_ole32", api)
    with native._apartment():
        events.append("work")
    with pytest.raises(ValueError), native._apartment():
        raise ValueError("failure")
    assert events == (["work", "uninit", "uninit"] if result >= 0 else ["work"])


def test_com_initialization_failure_is_not_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    from vispek_hc import _directshow as native

    monkeypatch.setattr(
        native,
        "_ole32",
        SimpleNamespace(
            CoInitializeEx=lambda *a: -2147024891,
            CoUninitialize=lambda: pytest.fail("not initialized"),
        ),
    )
    with pytest.raises(native._ComError), native._apartment():
        pytest.fail("must not enter")


@pytest.mark.parametrize("mode", [None, 0, 2])
def test_sdk_first_call_preserves_uninitialized_mta_and_sta_threads(
    monkeypatch: pytest.MonkeyPatch, mode: int | None
) -> None:
    import ctypes as c
    import threading

    from vispek_hc import _directshow as native
    from vispek_hc._windows_controls import DirectShowControls
    from vispek_hc.errors import DeviceNotFoundError

    def absent(device: str) -> Any:
        raise DeviceNotFoundError("no test camera")

    monkeypatch.setattr(native, "_find", absent)
    apartment_type = native._ole32.CoGetApartmentType
    apartment_type.argtypes = [c.POINTER(c.c_int32), c.POINTER(c.c_int32)]
    apartment_type.restype = c.c_int32
    errors = []

    def run() -> None:
        try:
            if mode is not None:
                assert native._ole32.CoInitializeEx(None, mode) == 0
            before, qualifier = c.c_int32(), c.c_int32()
            before_result = apartment_type(c.byref(before), c.byref(qualifier))
            try:
                with pytest.raises(DeviceNotFoundError):
                    DirectShowControls.open("not-a-camera")
                after = c.c_int32()
                assert apartment_type(c.byref(after), c.byref(qualifier)) == before_result
                if mode is not None:
                    assert after.value == before.value
            finally:
                if mode is not None:
                    native._ole32.CoUninitialize()
            assert apartment_type(c.byref(before), c.byref(qualifier)) & 0xFFFFFFFF == 0x800401F0
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(5)
    assert not thread.is_alive()
    assert errors == []


def test_query_bind_and_release_use_real_vtable_callbacks() -> None:
    import ctypes as c

    from vispek_hc import _directshow as native

    original, returned = Interface(), Interface()
    slots = []

    def query(this: Any, iid: Any, output: Any) -> int:
        slots.append(0)
        output[0] = returned.value.value
        return 0

    def bind(this: Any, context: Any, left: Any, iid: Any, output: Any) -> int:
        slots.append(8)
        output[0] = returned.value.value
        return 0

    original.method(0, native.HRESULT, [c.POINTER(native.GUID), c.POINTER(c.c_void_p)], query)
    original.method(
        8,
        native.HRESULT,
        [c.c_void_p, c.c_void_p, c.POINTER(native.GUID), c.POINTER(c.c_void_p)],
        bind,
    )
    with native._Pointer(original.value) as pointer:
        with pointer.query(native._UNKNOWN):
            pass
        with pointer.bind(8, native._UNKNOWN):
            pass
    assert slots == [0, 8]
    assert original.releases == 1
    assert returned.releases == 2, "same address, two independently owned references"


@pytest.mark.parametrize("failed", [False, True])
def test_property_always_clears_variant_before_releasing_bag(
    monkeypatch: pytest.MonkeyPatch, failed: bool
) -> None:
    import ctypes as c

    from vispek_hc import _directshow as native

    bag = Interface()
    buffer = c.create_unicode_buffer("HC1500")
    events = []

    def read(this: Any, name: Any, variant: Any, errors: Any) -> int:
        variant.contents.kind = 8
        variant.contents.payload.bstr = c.cast(buffer, c.c_void_p).value
        return -2147024891 if failed else 0

    bag.method(3, native.HRESULT, [c.c_wchar_p, c.POINTER(native.VARIANT), c.c_void_p], read)

    def clear(value: Any) -> int:
        assert bag.releases == 0
        events.append("clear")
        return 0

    monkeypatch.setattr(native, "_oleaut32", SimpleNamespace(VariantClear=clear))
    moniker = SimpleNamespace(bind=lambda *a: native._Pointer(bag.value))
    if failed:
        with pytest.raises(native._ComError):
            native._property(moniker, "FriendlyName")
    else:
        assert native._property(moniker, "FriendlyName") == "HC1500"
    assert events == ["clear"]
    assert bag.releases == 1


@pytest.mark.parametrize("names", [[], ["HC1500"], ["HC1500", "HC1500"]])
def test_enumeration_owns_every_reference_and_handles_empty_and_ambiguous_categories(
    monkeypatch: pytest.MonkeyPatch, names: list[str]
) -> None:
    import ctypes as c

    from vispek_hc import _directshow as native
    from vispek_hc.errors import AmbiguousDeviceError, DeviceNotFoundError

    system, enum = Interface(), Interface()
    cameras = [Interface() for _ in names]
    remaining = list(cameras)

    def create(clsid: Any, outer: Any, context: Any, iid: Any, output: Any) -> int:
        c.cast(output, c.POINTER(c.c_void_p))[0] = system.value.value
        return 0

    def category(this: Any, clsid: Any, output: Any, flags: int) -> int:
        output[0] = enum.value.value if names else None
        return 0 if names else 1

    def next_camera(this: Any, count: int, output: Any, fetched: Any) -> int:
        if not remaining:
            output[0], fetched[0] = None, 0
            return 1
        output[0], fetched[0] = remaining.pop(0).value.value, 1
        return 0

    system.method(
        3, native.HRESULT, [c.POINTER(native.GUID), c.POINTER(c.c_void_p), native.ULONG], category
    )
    enum.method(
        3,
        native.HRESULT,
        [native.ULONG, c.POINTER(c.c_void_p), c.POINTER(native.ULONG)],
        next_camera,
    )
    monkeypatch.setattr(native, "_ole32", SimpleNamespace(CoCreateInstance=create))
    monkeypatch.setattr(
        native,
        "_property",
        lambda pointer, key: "HC1500" if key == "FriendlyName" else str(pointer.value.value),
    )
    if len(names) == 1:
        pointer, path = native._find("HC1500")
        assert path == str(cameras[0].value.value)
        assert cameras[0].releases == 0
        pointer.close()
    else:
        error = DeviceNotFoundError if not names else AmbiguousDeviceError
        with pytest.raises(error):
            native._find("HC1500")
    assert system.releases == 1
    assert enum.releases == (1 if names else 0)
    assert all(camera.releases == 1 for camera in cameras)


def test_native_control_read_distinguishes_unsupported_properties_and_driver_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes as c

    from vispek_hc import _directshow as native

    class Controls:
        def __enter__(self) -> Any:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def call(self, slot: int, types: Any, number: int, *outputs: Any) -> int:
            if slot == 3:
                if number != 7:
                    raise native._ComError(-2147023728)
                values = (2800, 6500, 100, 4600, 3)
            else:
                assert slot == 5
                values = (4600, 1)
            for output, value in zip(outputs, values, strict=True):
                c.cast(output, c.POINTER(native.LONG))[0] = value
            return 0

    source = SimpleNamespace(query=lambda iid: Controls())
    camera = object.__new__(native.NativeCamera)
    monkeypatch.setattr(camera, "_call", lambda action: action(source))
    values = camera.read_all()
    assert values["white-balance-temp"].flags == 1

    def denied(*args: Any) -> int:
        raise native._ComError(-2147024891)

    monkeypatch.setattr(Controls, "call", denied)
    with pytest.raises(native._ComError):
        camera.read_all()
