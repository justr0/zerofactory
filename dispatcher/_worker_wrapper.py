"""Supervise worker children and persist their exit and signal outcomes.

The child runs in its own process group on POSIX. Caught wrapper signals are
forwarded to that group and the helper waits for bounded teardown before it
reports the worker exit. Negative child wait statuses are retained in the
signal marker instead of being confused with shell-style exit codes.

Usage: python _worker_wrapper.py <exit_prefix> <cmd...>
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time


def main() -> int:
    prefix = sys.argv[1]
    cmd = sys.argv[2:]
    task_id = os.path.basename(prefix).removeprefix("worker_")
    started_at = time.monotonic()
    caught_signal: int | None = None
    child: subprocess.Popen | None = None

    def catch_signal(signum: int, _frame) -> None:
        nonlocal caught_signal
        caught_signal = caught_signal or signum

    for name in ("SIGHUP", "SIGINT", "SIGTERM"):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), catch_signal)
            except (OSError, ValueError):
                pass

    def terminate_child(signum: int) -> int | None:
        if child is None or child.poll() is not None:
            return child.poll() if child is not None else None
        try:
            if os.name == "nt":
                child.send_signal(signum)
            else:
                os.killpg(child.pid, signum)
        except ProcessLookupError:
            pass
        try:
            return child.wait(timeout=2)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                child.kill()
            else:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            return child.wait()
        return child.poll()

    signal_details: dict[str, object] | None = None
    try:
        child = subprocess.Popen(cmd, start_new_session=os.name != "nt")
        if caught_signal:
            child_rc = terminate_child(caught_signal)
            rc = 128 + caught_signal
        else:
            while True:
                if caught_signal:
                    child_rc = terminate_child(caught_signal)
                    rc = 128 + caught_signal
                    break
                try:
                    child_rc = child.wait(timeout=0.1)
                    rc = child_rc
                    break
                except subprocess.TimeoutExpired:
                    continue
        child_signal = -child_rc if child_rc is not None and child_rc < 0 else None
        signal_number = caught_signal or child_signal
        if signal_number:
            try:
                signal_name = signal.Signals(signal_number).name
            except ValueError:
                signal_name = f"SIG{signal_number}"
            signal_details = {
                "signal_number": signal_number,
                "signal_name": signal_name,
                "task_id": task_id,
                "elapsed_seconds": int(time.monotonic() - started_at),
                "turn_state": "interrupted",
                "child_exit_code": child_rc,
                "child_signal_number": child_signal,
            }
            try:
                with open(f"{prefix}.signal.tmp", "w", encoding="utf-8") as f:
                    json.dump(signal_details, f)
                    f.write("\n")
                os.replace(f"{prefix}.signal.tmp", f"{prefix}.signal")
            except OSError:
                pass
            print(
                "ZeroFactory worker interrupted: "
                f"signal_number={signal_number} signal_name={signal_name} "
                f"task_id={task_id} "
                f"elapsed_seconds={signal_details['elapsed_seconds']} "
                "turn_state=interrupted",
                flush=True,
            )
            if caught_signal is None and child_signal:
                rc = 128 + signal_number
    except Exception:
        rc = 127
    tmp = f"{prefix}.exit.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(f"{int(rc)}\n")
        os.replace(tmp, f"{prefix}.exit")
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
