"""Background job queue with progress, ETA, and cancellation.

Two queues: "media" (proxies, thumbnails, waveforms; 2 workers) and "render" (exports; 1 worker,
so exports never fight each other for the encoder).
"""
from __future__ import annotations

import subprocess
import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable

from .ffmpeg_utils import Cancelled, FFmpegError


@dataclass
class Job:
    id: str
    kind: str  # e.g. "proxy", "thumbnail", "waveform", "export"
    label: str
    queue: str
    ref: dict = field(default_factory=dict)  # e.g. {"mediaId": ...} or {"projectId": ...}
    status: str = "queued"  # queued | running | done | error | cancelled
    progress: float = 0.0
    eta: float | None = None  # seconds remaining
    message: str = ""
    error: str | None = None
    log: str | None = None
    result: Any = None
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    cancel_requested: bool = False
    proc: subprocess.Popen | None = None
    version: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "ref": self.ref,
            "status": self.status, "progress": round(self.progress, 4), "eta": self.eta,
            "message": self.message, "error": self.error, "log": self.log, "result": self.result,
            "created": self.created, "started": self.started, "finished": self.finished,
        }


class JobContext:
    """Handed to job functions so they can report progress and honour cancellation."""

    def __init__(self, manager: "JobManager", job: Job):
        self._m = manager
        self.job = job

    def progress(self, p: float, message: str | None = None) -> None:
        j = self.job
        p = max(0.0, min(1.0, p))
        if j.started and p > 0.01:
            elapsed = time.time() - j.started
            eta = elapsed * (1 - p) / p
        else:
            eta = None
        self._m._update(j, progress=p, eta=eta, **({"message": message} if message is not None else {}))

    def message(self, msg: str) -> None:
        self._m._update(self.job, message=msg)

    def cancelled(self) -> bool:
        return self.job.cancel_requested

    def set_process(self, proc: subprocess.Popen) -> None:
        self.job.proc = proc


class JobManager:
    def __init__(self) -> None:
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._version = 0
        self._pools = {
            "media": ThreadPoolExecutor(max_workers=2, thread_name_prefix="media"),
            "render": ThreadPoolExecutor(max_workers=1, thread_name_prefix="render"),
        }

    # -- state ------------------------------------------------------------
    def _update(self, job: Job, **changes: Any) -> None:
        with self._cond:
            for k, v in changes.items():
                setattr(job, k, v)
            self._version += 1
            job.version = self._version
            self._cond.notify_all()

    @property
    def version(self) -> int:
        return self._version

    def wait_for_change(self, since: int, timeout: float) -> int:
        with self._cond:
            if self._version == since:
                self._cond.wait(timeout)
            return self._version

    def changed_since(self, since: int) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in self._jobs.values() if j.version > since]

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[dict]:
        with self._lock:
            return [j.to_dict() for j in sorted(self._jobs.values(), key=lambda j: j.created)]

    # -- control ----------------------------------------------------------
    def submit(self, kind: str, label: str, fn: Callable[[JobContext], Any], *, queue: str = "media",
               ref: dict | None = None) -> Job:
        job = Job(id=uuid.uuid4().hex[:12], kind=kind, label=label, queue=queue, ref=ref or {})
        with self._cond:
            self._jobs[job.id] = job
            self._version += 1
            job.version = self._version
            self._cond.notify_all()
        self._prune()
        self._pools[queue].submit(self._run, job, fn)
        return job

    def cancel(self, job_id: str) -> bool:
        job = self._jobs.get(job_id)
        if not job or job.status not in ("queued", "running"):
            return False
        self._update(job, cancel_requested=True, message="Cancelling…")
        if job.proc and job.proc.poll() is None:
            try:
                job.proc.kill()
            except OSError:
                pass
        return True

    def _run(self, job: Job, fn: Callable[[JobContext], Any]) -> None:
        if job.cancel_requested:
            self._update(job, status="cancelled", finished=time.time(), message="Cancelled")
            return
        self._update(job, status="running", started=time.time())
        ctx = JobContext(self, job)
        try:
            result = fn(ctx)
            if job.cancel_requested:
                raise Cancelled()
            self._update(job, status="done", progress=1.0, eta=0, result=result, finished=time.time(),
                         message=job.message if job.message and job.kind != "export" else "Done")
        except Cancelled:
            self._update(job, status="cancelled", finished=time.time(), message="Cancelled", eta=None)
        except FFmpegError as e:
            self._update(job, status="error", error=str(e), log=e.log, finished=time.time(), eta=None)
        except Exception as e:  # noqa: BLE001 - surface every failure to the UI
            self._update(job, status="error", error=f"{type(e).__name__}: {e}",
                         log=traceback.format_exc()[-3000:], finished=time.time(), eta=None)
        finally:
            job.proc = None

    def _prune(self, keep: int = 300) -> None:
        with self._lock:
            done = [j for j in self._jobs.values() if j.status in ("done", "error", "cancelled")]
            if len(self._jobs) > keep:
                for j in sorted(done, key=lambda j: j.created)[: len(self._jobs) - keep]:
                    self._jobs.pop(j.id, None)

    def shutdown(self) -> None:
        for j in list(self._jobs.values()):
            if j.status in ("queued", "running"):
                self.cancel(j.id)
        for p in self._pools.values():
            p.shutdown(wait=False, cancel_futures=True)


jobs = JobManager()
