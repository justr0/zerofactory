"""Windows port: unattended git helpers (``zf_wincompat.git_env`` / ``run_git``).

Regression coverage for the wedged-dispatcher incident: the bundled Git for
Windows defaults ``credential.helper`` to the interactive ``helper-selector``;
inside the daemon that prompt never returns, keeps the captured pipes open, and
``subprocess.run(timeout=)`` blocks forever in ``communicate()``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

import pytest

wincompat = pytest.importorskip(
    "zf_wincompat", reason="Windows port compatibility layer"
)


def test_git_env_disables_every_prompt_path_and_preserves_caller_settings():
    base = {
        "PATH": "x",
        "GIT_SSH_COMMAND": "ssh -F /custom",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "core.x",
        "GIT_CONFIG_VALUE_0": "y",
    }
    env = wincompat.git_env(base)

    assert env["PATH"] == "x"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GCM_INTERACTIVE"] == "never"
    assert env["GIT_SSH_COMMAND"] == "ssh -F /custom"  # caller override wins

    # Pre-existing GIT_CONFIG_* entries are kept; ours are appended after them.
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("core.x", "y")
    assert env["GIT_CONFIG_COUNT"] == "4"
    pairs = [
        (env[f"GIT_CONFIG_KEY_{i}"], env[f"GIT_CONFIG_VALUE_{i}"]) for i in range(1, 4)
    ]
    assert pairs[0] == ("credential.helper", "")  # empty value resets helper-selector
    assert ("credential.interactive", "never") in pairs
    assert ("credential.https://github.com.helper", "!gh auth git-credential") in pairs


def test_git_env_defaults_batch_mode_ssh_and_does_not_mutate_os_environ():
    before = dict(os.environ)
    env = wincompat.git_env()
    assert dict(os.environ) == before
    assert env["GIT_SSH_COMMAND"] == "ssh -o BatchMode=yes"


def test_run_git_success_path_matches_subprocess_run_shape():
    res = wincompat.run_git(
        [sys.executable, "-c", "import sys; print('ok'); sys.stderr.write('e')"],
        timeout=30,
    )
    assert isinstance(res, subprocess.CompletedProcess)
    assert res.returncode == 0
    assert res.stdout.strip() == "ok"
    assert res.stderr.strip() == "e"


@pytest.mark.skipif(
    sys.platform != "win32", reason="taskkill tree-kill is Windows-only"
)
def test_run_git_timeout_is_bounded_when_grandchild_holds_the_pipes():
    # The child spawns a grandchild that inherits stdout/stderr and sleeps. Killing
    # only the child (what subprocess.run(timeout=) does) leaves the pipes open, so
    # communicate() never returns. run_git must tree-kill and raise on time.
    code = (
        "import subprocess, sys, time; "
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)']); "
        "print(g.pid, flush=True); time.sleep(120)"
    )
    started = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired) as excinfo:
        wincompat.run_git([sys.executable, "-c", code], timeout=3)
    elapsed = time.monotonic() - started
    assert elapsed < 30, f"timeout not bounded: {elapsed:.1f}s"
    captured = excinfo.value.output or ""
    if isinstance(captured, bytes):
        captured = captured.decode(errors="replace")
    grandchild_pid = int(captured.strip().splitlines()[0])
    deadline = time.monotonic() + 10
    while wincompat.is_pid_alive(grandchild_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not wincompat.is_pid_alive(grandchild_pid), (
        "grandchild survived the tree kill"
    )
