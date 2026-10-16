"""The background thread and process for one Cursor run."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time


class Runner:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.proc: subprocess.Popen | None = None
        self.cancel = threading.Event()

    def busy(self) -> bool:
        with self.lock:
            return self.thread is not None and self.thread.is_alive()

    def start(self, target) -> bool:
        with self.lock:
            if self.thread is not None and self.thread.is_alive():
                return False
            self.cancel.clear()
            self.proc = None
            self.thread = threading.Thread(target=target, name="cursor-run", daemon=True)
            self.thread.start()
            return True

    def attach(self, proc: subprocess.Popen) -> None:
        with self.lock:
            self.proc = proc
            should_stop = self.cancel.is_set()
        if should_stop:
            stop_process(proc)

    def stop(self) -> None:
        self.cancel.set()
        with self.lock:
            proc = self.proc
        if proc is not None:
            stop_process(proc)

    def reset_cancel(self) -> None:
        with self.lock:
            self.cancel.clear()
            self.proc = None


def stop_process(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.time() + 5
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
