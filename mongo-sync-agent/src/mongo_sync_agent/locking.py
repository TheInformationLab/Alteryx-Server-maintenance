"""Single-instance guard for the agent using a lock file containing the current PID."""

import os
from pathlib import Path

import psutil


class AlreadyRunningError(Exception):
    """Raised when another instance of the agent is already running."""

    def __init__(self, pid: int):
        self.pid = pid
        super().__init__(f"Agent already running with PID {pid}")


class SingleInstanceLock:
    """Manages a single-instance lock using a PID file."""

    def __init__(self, lock_path: Path):
        """Initialize the lock with a path to the lock file.

        Args:
            lock_path: Path to the lock file.
        """
        self.lock_path = Path(lock_path)

    def acquire(self) -> None:
        """Acquire the lock.

        Raises:
            AlreadyRunningError: If another process holds the lock.
        """
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip())
            except (ValueError, OSError):
                # Lock file is corrupted or unreadable; treat as stale
                pid = None

            if pid is not None and psutil.pid_exists(pid):
                raise AlreadyRunningError(pid)

        # Write current PID to lock file
        self.lock_path.write_text(str(os.getpid()))

    def release(self) -> None:
        """Release the lock by deleting the lock file."""
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            # Lock file already deleted; nothing to do
            pass

    def __enter__(self):
        """Context manager entry."""
        self.acquire()
        return self

    def __exit__(self, *_):
        """Context manager exit."""
        self.release()
