"""One host, one dispatcher loop.

Regression: the plugin registered twice in a gateway process (once per profile
context) started a dispatcher each time when the two registrations held different
module globals. Both polled the same board and split the worker registry between
them.
"""

import threading
from unittest.mock import patch

from dispatcher import scheduler


def test_a_live_dispatcher_thread_blocks_a_second_start_from_other_globals():
    stop = threading.Event()
    foreign = threading.Thread(
        name="ZeroFactoryKanbanDispatcher", target=stop.wait, daemon=True
    )
    foreign.start()
    try:
        with (
            patch.object(scheduler, "_dispatcher_thread", None),
            patch.object(scheduler, "is_worker_or_child_process", return_value=False),
        ):
            assert scheduler._dispatcher_thread_alive() is True
            assert scheduler.start_background_dispatcher() is False
    finally:
        stop.set()
        foreign.join(timeout=5)
    assert scheduler._dispatcher_thread_alive() is False
