"""Ligxt AI worker: runs in its own Python environment (ai-venv, with PyTorch) as a subprocess of the
Ligxt backend, so the editor keeps working even if the AI setup is missing or broken.

Protocol: JSON lines. The backend writes requests to stdin:
    {"id": "...", "op": "probe" | "matte" | "segment" | "shutdown", "args": {...}}
    {"op": "cancel", "id": "..."}
and the worker answers on stdout:
    {"id": "...", "type": "progress", "p": 0.4, "msg": "..."}
    {"id": "...", "type": "result", "result": {...}}
    {"id": "...", "type": "error", "error": "...", "trace": "..."}
Everything else a library prints goes to stderr. The worker exits by itself after IDLE seconds
without work, which frees the GPU memory.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import traceback

_OUT = sys.stdout
sys.stdout = sys.stderr  # stray prints must never corrupt the protocol
_send_lock = threading.Lock()
IDLE = float(os.environ.get("LIGXT_AI_IDLE", "180"))
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def send(obj: dict) -> None:
    with _send_lock:
        _OUT.write(json.dumps(obj) + "\n")
        _OUT.flush()


class Cancelled(Exception):
    pass


_cancelled: set[str] = set()
_requests: "queue.Queue[dict]" = queue.Queue()


def _lines():
    """stdin lines. On Windows a thread blocked in ReadFile on the pipe deadlocks DLL loading in other
    threads (importing numpy/torch hangs), so poll with PeekNamedPipe and only read what is there."""
    if os.name != "nt":
        yield from sys.stdin
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    h = msvcrt.get_osfhandle(sys.stdin.fileno())
    avail = wintypes.DWORD()
    buf = b""
    while True:
        if not k32.PeekNamedPipe(wintypes.HANDLE(h), None, 0, None, ctypes.byref(avail), None):
            return  # pipe closed
        if avail.value:
            buf += os.read(sys.stdin.fileno(), avail.value)
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace")
        else:
            time.sleep(0.03)


def _reader() -> None:
    for line in _lines():
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            continue
        if req.get("op") == "cancel":
            _cancelled.add(req.get("id"))
        elif req.get("op") == "shutdown":
            os._exit(0)
        else:
            _requests.put(req)
    os._exit(0)  # the backend went away


class Ctx:
    def __init__(self, rid: str):
        self.id = rid
        self._last = 0.0

    def progress(self, p: float, msg: str = "", force: bool = False) -> None:
        now = time.time()
        if force or now - self._last > 0.25:
            self._last = now
            send({"id": self.id, "type": "progress", "p": round(max(0.0, min(1.0, p)), 4), "msg": msg})

    def check(self) -> None:
        if self.id in _cancelled:
            raise Cancelled()


# ---------------------------------------------------------------- ops
def op_probe(ctx: Ctx, args: dict) -> dict:
    out = {"python": sys.version.split()[0], "torch": None, "cuda": False, "device": "cpu"}
    try:
        import torch
        out["torch"] = torch.__version__
        out["cuda"] = bool(torch.cuda.is_available())
        if out["cuda"]:
            free, total = torch.cuda.mem_get_info()
            out.update(device=torch.cuda.get_device_name(0), vramTotal=total, vramFree=free,
                       cudaVersion=torch.version.cuda)
        import importlib.util
        out["sam2"] = importlib.util.find_spec("sam2") is not None
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {e}"
    return out


def op_matte(ctx: Ctx, args: dict) -> dict:
    from matting import matte
    return matte(ctx, args)


def op_segment(ctx: Ctx, args: dict) -> dict:
    from segment import segment
    return segment(ctx, args)


def op_interp(ctx: Ctx, args: dict) -> dict:
    from interp import interp
    return interp(ctx, args)


def op_track(ctx: Ctx, args: dict) -> dict:
    from track_sam import track
    return track(ctx, args)


def op_align_lyrics(ctx: Ctx, args: dict) -> dict:
    from lyrics_align import align
    return align(ctx, args)


def op_separate(ctx: Ctx, args: dict) -> dict:
    from separate import separate
    return separate(ctx, args)


def op_convert_mt(ctx: Ctx, args: dict) -> dict:
    from translate import convert
    return convert(ctx, args)


def op_translate(ctx: Ctx, args: dict) -> dict:
    from translate import translate
    return translate(ctx, args)


OPS = {"probe": op_probe, "matte": op_matte, "segment": op_segment, "interp": op_interp, "track": op_track, "align_lyrics": op_align_lyrics,
       "separate": op_separate, "convert_mt": op_convert_mt, "translate": op_translate}


def main() -> None:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    threading.Thread(target=_reader, daemon=True).start()
    send({"type": "ready", "pid": os.getpid()})
    while True:
        try:
            req = _requests.get(timeout=IDLE)
        except queue.Empty:
            send({"type": "idle-exit"})
            os._exit(0)
        rid = req.get("id") or ""
        ctx = Ctx(rid)
        fn = OPS.get(req.get("op"))
        try:
            if fn is None:
                raise ValueError(f"Unknown op {req.get('op')!r}")
            ctx.check()
            send({"id": rid, "type": "result", "result": fn(ctx, req.get("args") or {})})
        except Cancelled:
            send({"id": rid, "type": "error", "error": "Cancelled", "cancelled": True})
        except Exception as e:  # noqa: BLE001
            msg = f"{type(e).__name__}: {e}"
            try:
                import torch
                if isinstance(e, torch.cuda.OutOfMemoryError):
                    msg = "The graphics card ran out of memory. Close other GPU apps (games, browsers with video) and try again."
            except Exception:  # noqa: BLE001
                pass
            send({"id": rid, "type": "error", "error": msg, "trace": traceback.format_exc()[-4000:]})
        finally:
            _cancelled.discard(rid)


if __name__ == "__main__":
    main()
