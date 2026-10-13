"""Talks to the AI worker (ai_worker/worker.py running in ai-venv) over JSON lines.

One worker process at a time; it is started on first use and exits by itself when idle (freeing the
GPU memory). Calls are serialised. Cancel asks the worker to stop between frames and kills it if it
doesn't within a few seconds.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
import uuid

from . import config
from .ffmpeg_utils import Cancelled

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
WORKER = config.ROOT / "ai_worker" / "worker.py"


class AIError(RuntimeError):
    pass


class _Worker:
    def __init__(self) -> None:
        from .ai_models import ai_python
        py = ai_python()
        if not py or not py.exists():
            raise AIError("The AI tools are not installed. Run setup.ps1 (it builds the separate ai-venv) and restart Ligxt.")
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "HF_HOME": str(config.ROOT / "models" / "ai" / ".hf"), "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
               "HF_HUB_DISABLE_TELEMETRY": "1", "PYTORCH_CUDA_ALLOC_CONF": "expandable_segments:True"}
        self.proc = subprocess.Popen([str(py), str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, cwd=str(WORKER.parent), env=env,
                                     creationflags=CREATE_NO_WINDOW, text=True, encoding="utf-8", errors="replace", bufsize=1)
        self.inbox: dict[str, queue.Queue] = {}
        self.ready = threading.Event()
        self.stderr_tail: list[str] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._read_err, daemon=True).start()
        if not self.ready.wait(60):
            self.kill()
            raise AIError("The AI worker did not start: " + " ".join(self.stderr_tail[-5:]))

    def _read(self) -> None:
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("type") == "ready":
                self.ready.set()
                continue
            q = self.inbox.get(msg.get("id") or "")
            if q:
                q.put(msg)
        for q in list(self.inbox.values()):
            q.put({"type": "error", "error": "The AI worker stopped unexpectedly. " + " ".join(self.stderr_tail[-3:])})

    def _read_err(self) -> None:
        for line in self.proc.stderr:
            line = line.rstrip()
            if line:
                self.stderr_tail = (self.stderr_tail + [line])[-40:]

    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def kill(self) -> None:
        try:
            self.proc.kill()
        except OSError:
            pass


_lock = threading.Lock()  # one call at a time
_state_lock = threading.Lock()
_worker: _Worker | None = None
_busy = threading.Event()


def busy() -> bool:
    return _busy.is_set()


def _get() -> _Worker:
    global _worker
    with _state_lock:
        if _worker is None or not _worker.alive():
            _worker = _Worker()
        return _worker


def stop() -> None:
    global _worker
    with _state_lock:
        if _worker and _worker.alive():
            try:
                _worker.send({"op": "shutdown"})
                _worker.proc.wait(5)
            except Exception:  # noqa: BLE001
                _worker.kill()
        _worker = None


def call(op: str, args: dict, *, progress=None, cancelled=None, timeout: float | None = None) -> dict:
    """Run one op in the worker. progress(p, msg) is called with worker progress; cancelled() is polled."""
    with _lock:
        _busy.set()
        try:
            w = _get()
            rid = uuid.uuid4().hex[:10]
            q: queue.Queue = queue.Queue()
            w.inbox[rid] = q
            try:
                w.send({"id": rid, "op": op, "args": args})
                t0 = time.time()
                cancel_sent = None
                while True:
                    if cancelled and cancelled() and cancel_sent is None:
                        w.send({"op": "cancel", "id": rid})
                        cancel_sent = time.time()
                    if cancel_sent and time.time() - cancel_sent > 8:
                        w.kill()  # didn't stop in time
                        raise Cancelled()
                    if timeout and time.time() - t0 > timeout:
                        w.kill()
                        raise AIError(f"The AI worker did not answer within {timeout:.0f} s.")
                    try:
                        msg = q.get(timeout=0.25)
                    except queue.Empty:
                        if not w.alive():
                            raise AIError("The AI worker stopped unexpectedly: " + " ".join(w.stderr_tail[-4:]))
                        continue
                    kind = msg.get("type")
                    if kind == "progress":
                        if progress:
                            progress(float(msg.get("p", 0)), msg.get("msg") or "")
                    elif kind == "result":
                        return msg.get("result") or {}
                    elif kind == "error":
                        if msg.get("cancelled") or cancel_sent:
                            raise Cancelled()
                        err = AIError(msg.get("error") or "AI error")
                        err.trace = msg.get("trace")  # type: ignore[attr-defined]
                        raise err
            finally:
                w.inbox.pop(rid, None)
        finally:
            _busy.clear()
