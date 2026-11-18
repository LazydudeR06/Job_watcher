import logging
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path


def configure_logging(
    verbose: bool = False,
    log_path: str | Path = "logs/jobwatcher.log",
) -> logging.Logger:
    """Configure rotating file logging and optional verbose console output."""
    path = Path(log_path)
    path.parent.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger("jobwatcher")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    file_handler = TimedRotatingFileHandler(
        path,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if verbose:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    return logger
