"""Cross-platform single-instance lock for the local manager."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any


class InstanceAlreadyRunning(RuntimeError):
    """Raised when another manager process owns the lock."""


class InstanceLock:
    """Hold an OS-level advisory lock for the lifetime of a manager process."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: Any = None

    def acquire(self) -> "InstanceLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="ascii")
        if self.path.stat().st_size == 0:
            self._handle.write("0")
            self._handle.flush()
        self._handle.seek(0)

        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as exc:
            self._handle.close()
            self._handle = None
            raise InstanceAlreadyRunning(f"lock is already held: {self.path}") from exc
        return self

    def release(self) -> None:
        if self._handle is None:
            return
        try:
            self._handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            self._handle.close()
            self._handle = None

    def __enter__(self) -> "InstanceLock":
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()
