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
    process_handle = 0x1_2345_6789
    kernel32 = SimpleNamespace(
        OpenProcess=Mock(return_value=process_handle),
        GetExitCodeProcess=Mock(return_value=query_succeeds),
        CloseHandle=Mock(return_value=True),
    )
    monkeypatch.setattr(
        ctypes, "windll", SimpleNamespace(kernel32=kernel32), raising=False
    )

    def set_exit_code(_handle, exit_code_pointer):
        ctypes.cast(
            exit_code_pointer, ctypes.POINTER(wintypes.DWORD)
        ).contents.value = exit_code
        return query_succeeds

    kernel32.GetExitCodeProcess.side_effect = set_exit_code

    assert wincompat.is_pid_alive(1234) is expected_alive

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

    kernel32.OpenProcess.assert_called_once_with(0x1000, False, 1234)
    kernel32.GetExitCodeProcess.assert_called_once()
    assert kernel32.GetExitCodeProcess.call_args.args[0] == process_handle
    kernel32.CloseHandle.assert_called_once_with(process_handle)


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
