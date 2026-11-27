"""Regression checks for importing and exercising dispatcher locks without fcntl."""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest


def test_dispatcher_lock_tests_run_without_repo_fcntl_imports(tmp_path: Path) -> None:
    """Load the Windows lock backend and run lock coverage with fcntl blocked."""
    if os.name == "nt":
        pytest.skip("platform-mocked lock adapter requires host fcntl")

    repo_root = Path(__file__).resolve().parents[3]
    bootstrap = tmp_path / "windows_lock_test_bootstrap.py"
    bootstrap.write_text(
        textwrap.dedent(
            """
            import builtins
            import ctypes
            import importlib
            import os
            import sys
            import types
            from pathlib import Path

            import fcntl as host_fcntl
            import pytest

            repo_root = Path(sys.argv[1]).resolve()
            sys.path.insert(0, str(repo_root))
            sys.modules.pop("fcntl", None)
            def host_locking(fd, mode, _nbytes):
                msvcrt = sys.modules["msvcrt"]
                if mode == msvcrt.LK_NBLCK:
                    host_fcntl.flock(fd, host_fcntl.LOCK_EX | host_fcntl.LOCK_NB)
                elif mode == msvcrt.LK_UNLCK:
                    host_fcntl.flock(fd, host_fcntl.LOCK_UN)
                else:
                    raise AssertionError(f"unexpected msvcrt lock mode: {mode}")

            msvcrt = types.ModuleType("msvcrt")
            msvcrt.LK_NBLCK = 1
            msvcrt.LK_UNLCK = 2
            msvcrt.locking = host_locking
            sys.modules["msvcrt"] = msvcrt

            original_import = builtins.__import__

            def deny_repo_fcntl(name, globals=None, locals=None, fromlist=(), level=0):
                source = (globals or {}).get("__file__")
                if name == "fcntl" and source:
                    try:
                        relative_source = (
                            Path(source).resolve().relative_to(repo_root).as_posix()
                        )
                    except ValueError:
                        pass
                    else:
                        if relative_source.startswith("tests/"):
                            raise ImportError(
                                "native fcntl is unavailable to test modules"
                            )
                return original_import(name, globals, locals, fromlist, level)

            builtins.__import__ = deny_repo_fcntl
            original_name = os.name
            os.name = "nt"
            try:
                import dispatcher
                import zf_wincompat
                scheduler = importlib.import_module("dispatcher.scheduler")
                assert dispatcher.fcntl is zf_wincompat
                assert scheduler.fcntl is zf_wincompat
            finally:
                os.name = original_name

            collect_code = pytest.main(["--collect-only", "-q", "tests/"])
            if collect_code != pytest.ExitCode.OK:
                raise SystemExit(collect_code)

            tests = [
                "tests/unit/dispatcher/test_scheduler.py::test_run_dispatch_cycle_concurrent_flock_skipped",
                "tests/e2e/test_concurrency_resilience.py::TestConcurrencyAndResilienceE2E::test_01_cross_process_flock_concurrency_guard",
                "tests/unit/dispatcher/test_dispatcher_ownership.py::test_start_is_idempotent_and_holds_the_owner_lock",
                "tests/unit/dispatcher/test_dispatcher_ownership.py::test_stop_releases_ownership",
                "tests/unit/dispatcher/test_dispatcher_ownership.py::test_start_declines_while_another_process_owns_the_loop",
            ]
            exit_code = pytest.main(["-q", *tests])
            raise SystemExit(exit_code)
            """
        ),
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(bootstrap), str(repo_root)],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
        timeout=180,
    )
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
