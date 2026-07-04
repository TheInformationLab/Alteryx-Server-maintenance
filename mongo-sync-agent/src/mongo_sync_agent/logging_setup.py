import json
import logging
import os
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone
import psutil


def configure_logging(log_dir: str, run_id: str, level: str = "INFO") -> None:
    """
    Configure structured JSON logging with console and file handlers.

    Sets up two handlers:
    1. Console (StreamHandler) writing JSON lines to stdout
    2. File (RotatingFileHandler) in log_dir/msa.log (10MB max, 5 backups), JSON lines

    Each log record includes: timestamp (ISO-8601 UTC), level, logger name, run_id, message,
    plus any extra kwargs passed to the logger.

    Args:
        log_dir: Directory where log files will be stored
        run_id: Run ID to include in all log records
        level: Logging level (default: "INFO")
    """
    # Create log directory if it doesn't exist
    os.makedirs(log_dir, exist_ok=True)

    # Get the root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Create custom JSON formatter
    class JsonFormatter(logging.Formatter):
        def format(self, record):
            log_obj = {
                "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "run_id": run_id,
                "message": record.getMessage(),
            }

            # Add any extra fields from the record (e.g., from logger.info(..., extra={"key": "value"}))
            excluded_keys = {
                "name", "msg", "args", "created", "filename", "funcName",
                "levelname", "levelno", "lineno", "module", "msecs",
                "message", "pathname", "process", "processName", "relativeCreated",
                "thread", "threadName", "exc_info", "exc_text", "stack_info",
                "asctime", "taskName"
            }

            for key, value in record.__dict__.items():
                if key not in excluded_keys:
                    log_obj[key] = value

            return json.dumps(log_obj)

    # Console handler (stdout)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(console_handler)

    # File handler (RotatingFileHandler)
    log_file = os.path.join(log_dir, "msa.log")
    file_handler = RotatingFileHandler(
        log_file,
        maxBytes=10 * 1024 * 1024,  # 10MB
        backupCount=5
    )
    file_handler.setFormatter(JsonFormatter())
    root_logger.addHandler(file_handler)


class PeakRssTracker:
    """Track peak RSS memory usage."""

    def __init__(self):
        self._peak = 0

    def sample(self) -> int:
        """
        Sample current RSS memory usage, update peak, and return current value.

        Returns:
            Current RSS memory usage in bytes
        """
        rss = psutil.Process(os.getpid()).memory_info().rss
        if rss > self._peak:
            self._peak = rss
        return rss

    @property
    def peak(self) -> int:
        """Get the peak RSS memory usage recorded."""
        return self._peak
