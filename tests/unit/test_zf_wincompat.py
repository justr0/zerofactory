"""Regression tests for Windows compatibility APIs on any host."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

wincompat = pytest.importorskip(
    "zf_wincompat", reason="Windows port compatibility layer"
)


@pytest.mark.parametrize(
    ("exit_code", "query_succeeds", "expected_alive"),
    [
        (259, True, True),
        (0, True, False),
        (259, False, False),
    ],
)
def test_is_pid_alive_preserves_pointer_width_process_handle(
    monkeypatch, exit_code, query_succeeds, expected_alive
):
    if ctypes.sizeof(wintypes.HANDLE) <= ctypes.sizeof(ctypes.c_uint32):
        pytest.skip("requires a pointer wider than 32 bits")

    process_handle = 0x1_2345_6789
    assert process_handle > 0xFFFF_FFFF
    opened = []
    queried = []
    closed = []

    def open_process(access, inherit, pid):
        opened.append((access, inherit, pid))
        return process_handle

    def get_exit_code(handle, exit_code_pointer):
        queried.append(handle)
        if handle != process_handle:
            return False
        ctypes.cast(
            exit_code_pointer, ctypes.POINTER(wintypes.DWORD)
        ).contents.value = exit_code
        return query_succeeds

    def close_handle(handle):
        closed.append(handle)
        return True

    winfunctype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)

    def dll_function(restype, argtypes, callback):
        native_callback = winfunctype(restype, *argtypes)(callback)
        address = ctypes.cast(native_callback, ctypes.c_void_p).value
        assert address is not None
        function = winfunctype(ctypes.c_int, *argtypes)(address)
        return native_callback, function

    open_callback, open_api = dll_function(
        wintypes.HANDLE,
        [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
        open_process,
    )
    exit_code_pointer = ctypes.POINTER(wintypes.DWORD)
    exit_callback, exit_api = dll_function(
        wintypes.BOOL,
        [wintypes.HANDLE, exit_code_pointer],
        get_exit_code,
    )
    close_callback, close_api = dll_function(
        wintypes.BOOL, [wintypes.HANDLE], close_handle
    )
    kernel32 = SimpleNamespace(
        OpenProcess=open_api,
        GetExitCodeProcess=exit_api,
        CloseHandle=close_api,
    )
    assert open_api.restype is ctypes.c_int
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    assert wincompat.is_pid_alive(1234) is expected_alive

    # Keep the native callback objects alive for the duration of the calls.
    assert open_callback and exit_callback and close_callback
    assert kernel32.OpenProcess.restype is wintypes.HANDLE
    assert kernel32.OpenProcess.argtypes == [
        wintypes.DWORD,
        wintypes.BOOL,
        wintypes.DWORD,
    ]
    assert kernel32.GetExitCodeProcess.restype is wintypes.BOOL
    assert kernel32.GetExitCodeProcess.argtypes == [
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    ]
    assert kernel32.CloseHandle.restype is wintypes.BOOL
    assert kernel32.CloseHandle.argtypes == [wintypes.HANDLE]

    assert opened == [(0x1000, False, 1234)]
    assert queried == [process_handle]
    assert closed == [process_handle]


def test_is_pid_alive_returns_false_when_open_process_fails(monkeypatch):
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=None),
        GetExitCodeProcess=Mock(),
        CloseHandle=Mock(),
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    assert wincompat.is_pid_alive(1234) is False
    kernel32.GetExitCodeProcess.assert_not_called()
    kernel32.CloseHandle.assert_not_called()


def test_is_pid_alive_rejects_invalid_pid_without_opening_process(monkeypatch):
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(), GetExitCodeProcess=Mock(), CloseHandle=Mock()
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    for pid in (None, 0, -1, "1234"):
        assert wincompat.is_pid_alive(pid) is False

    kernel32.OpenProcess.assert_not_called()
