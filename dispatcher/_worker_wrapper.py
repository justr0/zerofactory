"""Windows-safe replacement for the POSIX ``sh`` exit-code wrapper.

Runs the wrapped command, then atomically writes the integer exit code to
``<prefix>.exit`` (via ``.exit.tmp`` + replace) so the dispatcher's ``.exit``
file protocol stays identical across platforms. If the wrapper itself is
hard-killed, no ``.exit`` file appears and the reaper classifies the task as
CRASHED — same contract as a POSIX SIGKILL.

Usage: python _worker_wrapper.py <exit_prefix> <cmd...>
"""

from __future__ import annotations

import os
import subprocess
import sys


def main() -> int:
    prefix = sys.argv[1]
    cmd = sys.argv[2:]
    try:
        rc = subprocess.call(cmd)
    except Exception:
        rc = 127
    tmp = f"{prefix}.exit.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(str(int(rc)))
        os.replace(tmp, f"{prefix}.exit")
    except OSError:
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
