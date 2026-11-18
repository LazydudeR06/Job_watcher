import logging
import sched
import tempfile
import unittest
from pathlib import Path

from jobwatcher.logging_config import configure_logging
from jobwatcher.scheduler import run_forever


class OneCycleScheduler:
    """Run one queued event for deterministic scheduler testing."""

    def __init__(self) -> None:
        self.events = []

    def enter(self, delay: float, priority: int, action):
        event = (delay, priority, action)
        self.events.append(event)
        return event

    def run(self) -> None:
        _, _, action = self.events.pop(0)
        action()


class Step6Test(unittest.TestCase):
    def test_scheduler_runs_immediately_and_reschedules(self) -> None:
        calls = []
        scheduler = OneCycleScheduler()

        run_forever(lambda: calls.append("cycle"), 30, scheduler=scheduler)

        self.assertEqual(calls, ["cycle"])
        self.assertEqual(len(scheduler.events), 1)
        self.assertEqual(scheduler.events[0][0], 30)

    def test_scheduler_logs_and_reschedules_after_failure(self) -> None:
        scheduler = OneCycleScheduler()

        def fail() -> None:
            raise RuntimeError("cycle failed")

        with self.assertLogs("jobwatcher.scheduler", level=logging.ERROR):
            run_forever(fail, 15, scheduler=scheduler)

        self.assertEqual(len(scheduler.events), 1)

    def test_logging_creates_rotating_log_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            log_path = Path(temp_dir) / "logs" / "watcher.log"
            logger = configure_logging(verbose=True, log_path=log_path)
            logger.info("test log record")
            for handler in logger.handlers:
                handler.flush()

            self.assertTrue(log_path.is_file())
            self.assertIn("test log record", log_path.read_text(encoding="utf-8"))
            for handler in logger.handlers[:]:
                logger.removeHandler(handler)
                handler.close()

    def test_scheduler_rejects_non_positive_interval(self) -> None:
        with self.assertRaises(ValueError):
            run_forever(lambda: None, 0, scheduler=sched.scheduler())


if __name__ == "__main__":
    unittest.main()
