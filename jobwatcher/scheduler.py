import logging
import sched
import time
from collections.abc import Callable


LOGGER = logging.getLogger(__name__)


def run_forever(
    task: Callable[[], None],
    interval_seconds: int,
    scheduler: sched.scheduler | None = None,
) -> None:
    """Run a task immediately and reschedule it at a fixed interval."""
    if interval_seconds < 1:
        raise ValueError("interval_seconds must be positive")

    clock = scheduler or sched.scheduler(time.monotonic, time.sleep)

    def run_task() -> None:
        try:
            task()
        except Exception:
            LOGGER.exception("Scheduled job-watcher cycle failed")
        finally:
            clock.enter(interval_seconds, 1, run_task)

    LOGGER.info("Starting recurring job-watcher schedule every %s seconds", interval_seconds)
    clock.enter(0, 1, run_task)
    clock.run()
