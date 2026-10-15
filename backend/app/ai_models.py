"""AI model manager: the catalog (pinned revisions + SHA-256 of every file), downloads on first use with
progress, deletion, disk usage, AI settings (device, VRAM budget, default models), and the status of the
separate AI environment (ai-venv).

Models live in models/ai/<id>/. Every file is checked against its pinned SHA-256 after download; a
mismatch deletes the file and fails the download.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from . import config
from .jobs import JobContext, jobs

router = APIRouter(prefix="/api/ai", tags=["ai"])

AI_MODELS_DIR = config.ROOT / "models" / "ai"
SETTINGS_FILE = config.DATA_ROOT / "ai_settings.json"

HF = "https://huggingface.co/{repo}/resolve/{rev}/{file}"


def _hf(repo: str, rev: str, files: list[tuple[str, int, str]]) -> list[dict]:
    return [{"name": n, "size": s, "sha256": h, "url": HF.format(repo=repo, rev=rev, file=n)} for n, s, h in files]


_BIREFNET_CFG = ("BiRefNet_config.py", 298, "e7b8c2a74f6cea6a59553d517f71d47f2c1d90e670a13416af17c25fe2f3dc52")
_BIREFNET_PY = ("birefnet.py", 92134, "2a45b4e0ece72d7c4212bca1a988e7d7e52bfe9f98ec59c58b8809c8a8b7a831")

# Every model's license was checked before adding it (README lists them too). Only MIT / Apache / BSD.
CATALOG: dict[str, dict] = {
    "birefnet-lite": {
        "name": "BiRefNet lite (matting)", "task": "person", "license": "MIT",
        "source": "https://huggingface.co/ZhengPeng7/BiRefNet_lite-matting", "res": 1024, "dynamic": False,
        "about": "Fast person/subject matting (Swin-T). Good edges at a fraction of the cost.",
        "files": _hf("ZhengPeng7/BiRefNet_lite-matting", "99c33412e3f58e1f33187abdc8c435c645243690", [
            ("model.safetensors", 88965896, "ce8bcfc045e336322c0424a5863dcfb7e9ce8fed0a5fd4d1b2b20adf12d97243"),
            ("birefnet.py", 92134, "af8568b5be406bf4d2a68a7ed6d72e40f73b37a1fb6fc9ebd71b5b3cbcd069c9"),
            _BIREFNET_CFG,
            ("config.json", 418, "2050e7f1d76417bb167d86a22a52737de2a6e114c0f26c8df85c188366819d72"),
        ]),
    },
    "birefnet": {
        "name": "BiRefNet (matting)", "task": "person", "license": "MIT",
        "source": "https://huggingface.co/ZhengPeng7/BiRefNet-matting", "res": 1024, "dynamic": False,
        "about": "High-quality person/subject matting (Swin-L): best hair and fine detail.",
        "files": _hf("ZhengPeng7/BiRefNet-matting", "eccde0a8cbdce7ac5fecfeb06340fe7b949e85d9", [
            ("model.safetensors", 884878856, "a9875de5b1e6c8eb5fdaa8c727a82927ce442cdc87ba3abee6a77e6fa46c25bb"),
            _BIREFNET_PY, _BIREFNET_CFG,
            ("config.json", 413, "b2b235983b80fbff325976ca517bb7eda25e915b84a97b150eca41266cf8a13b"),
        ]),
    },
    "birefnet-dynamic": {
        "name": "BiRefNet dynamic (matting)", "task": "person", "license": "MIT",
        "source": "https://huggingface.co/ZhengPeng7/BiRefNet_dynamic-matting", "res": 1280, "dynamic": True,
        "about": "Swin-L trained on varied sizes: keeps the picture's aspect ratio (no squeeze for 9:16 phone video).",
        "files": _hf("ZhengPeng7/BiRefNet_dynamic-matting", "074df545be87034e74a96bf71566ecbbc4c15f0a", [
            ("model.safetensors", 444473596, "ae177d331c79dcb849ef5b8aea39cea6effd6ca0df36b5a41431658f990e9ee7"),
            _BIREFNET_PY, _BIREFNET_CFG,
            ("config.json", 421, "e4fa685fa1e366deaf966aa5c066fd24147821eafc061d5aa73261237b7df6f2"),
        ]),
    },
    "sam2.1-small": {
        "name": "SAM 2.1 small (click to select)", "task": "object", "license": "Apache-2.0",
        "source": "https://github.com/facebookresearch/sam2", "res": 1024, "dynamic": False,
        "config": "configs/sam2.1/sam2.1_hiera_s.yaml",
        "about": "Meta's Segment Anything 2: click an object (not just people) and it is tracked through the clip.",
        "files": [{"name": "sam2.1_hiera_small.pt", "size": 184416285,
                   "sha256": "6d1aa6f30de5c92224f8172114de081d104bbd23dd9dc5c58996f0cad5dc4d38",
                   "url": "https://dl.fbaipublicfiles.com/segment_anything_2/092824/sam2.1_hiera_small.pt"}],
    },
    "rife-4.26": {
        "name": "RIFE 4.26 (AI slow motion)", "task": "interpolation", "license": "MIT",
        "source": "https://github.com/hzwer/Practical-RIFE", "res": 0, "dynamic": False,
        "about": "Frame interpolation by hzwer: smooth slow motion and frame-rate conversion (weights from the author's Hugging Face).",
        "files": _hf("hzwer/RIFE", "01fdc7e97404120c243c3ea7b427046e5dc7643e", [
            ("RIFEv4.26_0921.zip", 22869906, "1fa9b9cda3d9b8c3e301359e2595960902f97bf926c08598b0e9957a3f3f760e"),
        ]),
        # only the weights are used (the network code is vendored in ai_worker/rife_net.py)
        "installs": [{"name": "flownet.pkl", "size": 24636301, "from": "RIFEv4.26_0921.zip", "member": "RIFEv4.26_0921/flownet.pkl",
                      "sha256": "45c7f74156704769dc9f85cfcaf8552e1e926f9399dcfa3a553dee88fac6f53f"}],
    },
    "whisper-large-v3-turbo": {
        "name": "Whisper large-v3-turbo (song lyrics)", "task": "speech", "license": "MIT",
        "source": "https://huggingface.co/dropbox-dash/faster-whisper-large-v3-turbo", "res": 0, "dynamic": False,
        "about": "OpenAI's Whisper large-v3-turbo (MIT) in CTranslate2 format: the most accurate option for lyrics (Auto) and for "
                 "aligning pasted lyrics. Runs with faster-whisper on the graphics card.",
        # the repo was renamed from mobiuslabsgmbh/faster-whisper-large-v3-turbo; same revision
        "files": _hf("dropbox-dash/faster-whisper-large-v3-turbo", "0a363e9161cbc7ed1431c9597a8ceaf0c4f78fcf", [
            ("model.bin", 1617884929, "e76620f83d5f5b69efd3d87e3dc180c1bd21df9fbebacfd4335e5e1efcc018da"),
            ("config.json", 2263, "b0253ea6c0d3bea6b1e19e91a02acfd3b53f4467362efcb5a3e6b16c9b3a9b7e"),
            ("preprocessor_config.json", 340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
            ("tokenizer.json", 2710337, "297b13372ac43916285644fb9687add3cc62ee2a1adb60da3dc25cc94c1871fd"),
            ("vocabulary.json", 1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
        ]),
    },
}
PERSON_MODELS = [k for k, v in CATALOG.items() if v["task"] == "person"]
LYRICS_MODEL = "whisper-large-v3-turbo"
RIFE_MODEL = "rife-4.26"
OBJECT_MODEL = "sam2.1-small"
DEFAULT_SETTINGS = {"device": "auto", "budgetGB": 4.5, "personModel": "birefnet"}  # best quality on the test clips


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    try:
        s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    out = {**DEFAULT_SETTINGS, **{k: v for k, v in s.items() if k in DEFAULT_SETTINGS}}
    if out["personModel"] not in CATALOG:
        out["personModel"] = DEFAULT_SETTINGS["personModel"]
    out["budgetGB"] = max(1.0, min(5.5, float(out["budgetGB"])))
    return out


class SettingsBody(BaseModel):
    device: str | None = None
    budgetGB: float | None = None
    personModel: str | None = None


@router.put("/settings")
def put_settings(body: SettingsBody):
    s = load_settings()
    if body.device is not None:
        if body.device not in ("auto", "cpu"):
            raise HTTPException(400, "Device must be 'auto' (GPU when available) or 'cpu'.")
        s["device"] = body.device
    if body.budgetGB is not None:
        s["budgetGB"] = max(1.0, min(5.5, float(body.budgetGB)))
    if body.personModel is not None:
        if body.personModel not in PERSON_MODELS:
            raise HTTPException(400, "Unknown model.")
        s["personModel"] = body.personModel
    SETTINGS_FILE.write_text(json.dumps(s, indent=1), encoding="utf-8")
    return s


# ---------------------------------------------------------------- files
def model_dir(mid: str) -> Path:
    return AI_MODELS_DIR / mid


def _dir_size(d: Path) -> int:
    return sum(f.stat().st_size for f in d.rglob("*") if f.is_file()) if d.exists() else 0


def is_installed(mid: str) -> bool:
    spec = CATALOG[mid]
    d = model_dir(mid)
    ok_file = d / ".verified"
    if not ok_file.exists():
        return False
    return all((d / f["name"]).exists() and (d / f["name"]).stat().st_size == f["size"] for f in spec.get("installs") or spec["files"])


def download_size(mid: str) -> int:
    return sum(f["size"] for f in CATALOG[mid]["files"])


_dl_lock = threading.Lock()
_downloading: dict[str, str] = {}  # model id -> job id


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(mid: str, ctx: JobContext | None, p0: float = 0.0, p1: float = 1.0) -> None:
    """Download and verify every file of a model (resumes nothing; skips files already verified)."""
    from .system_api import fmt_bytes, require_space
    spec = CATALOG[mid]
    d = model_dir(mid)
    d.mkdir(parents=True, exist_ok=True)
    total = download_size(mid)
    require_space(d, total - _dir_size(d), f"the {spec['name']} model")
    done = 0
    t0 = time.time()
    (d / ".verified").unlink(missing_ok=True)
    for f in spec["files"]:
        dest = d / f["name"]
        if dest.exists() and dest.stat().st_size == f["size"] and _sha256(dest) == f["sha256"]:
            done += f["size"]
            continue
        part = dest.with_name(dest.name + ".part")
        h = hashlib.sha256()
        part.unlink(missing_ok=True)
        tries = 0
        while True:
            have = part.stat().st_size if part.exists() else 0
            headers = {"User-Agent": "Ligxt-Video-Editor"}
            if have:  # resume after a stall (CDNs can pause long enough to time out on big files)
                headers["Range"] = f"bytes={have}-"
            req = urllib.request.Request(f["url"], headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as r, part.open("ab" if have else "wb") as out:
                    if have and r.status != 206:  # the server ignored the range: start over
                        out.seek(0)
                        out.truncate()
                        h = hashlib.sha256()
                        done -= have
                    while True:
                        if ctx and ctx.cancelled():
                            raise RuntimeError("Cancelled")
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        out.write(chunk)
                        h.update(chunk)
                        done += len(chunk)
                        if ctx:
                            el = max(0.5, time.time() - t0)
                            ctx.progress(p0 + (p1 - p0) * done / total,
                                         f"Downloading {spec['name']}: {fmt_bytes(done)} of {fmt_bytes(total)} ({fmt_bytes(done / el)}/s)")
                if part.stat().st_size >= f["size"]:
                    break
                raise TimeoutError("the connection ended early")
            except (TimeoutError, OSError, urllib.error.URLError) as e:
                tries += 1
                if tries > 6 or (ctx and ctx.cancelled()):
                    part.unlink(missing_ok=True)
                    raise RuntimeError(f"Downloading {f['name']} failed after {tries} tries ({e}). Nothing was installed; try again later.") from e
                time.sleep(min(10, 2 * tries))
                continue
            except BaseException:
                part.unlink(missing_ok=True)
                raise
        if h.hexdigest() != f["sha256"]:
            part.unlink(missing_ok=True)
            raise RuntimeError(f"{f['name']} failed its checksum (the download was corrupted or the file changed upstream). "
                               "Nothing was installed; try again later.")
        part.replace(dest)
    for inst in spec.get("installs") or []:
        # unpack only the listed member of a verified archive, verify it too, then drop the archive
        import zipfile
        with zipfile.ZipFile(d / inst["from"]) as z:
            data = z.read(inst["member"])
        if len(data) != inst["size"] or hashlib.sha256(data).hexdigest() != inst["sha256"]:
            raise RuntimeError(f"{inst['name']} inside {inst['from']} failed its checksum. Nothing was installed.")
        (d / inst["name"]).write_bytes(data)
    for inst in spec.get("installs") or []:
        (d / inst["from"]).unlink(missing_ok=True)
    (d / ".verified").write_text(json.dumps({"verified": time.time(), "files": [f["name"] for f in spec.get("installs") or spec["files"]]}), encoding="utf-8")


def ensure_model(mid: str, ctx: JobContext | None, p0: float = 0.0, p1: float = 0.2) -> Path:
    if mid not in CATALOG:
        raise RuntimeError(f"Unknown AI model '{mid}'.")
    if not is_installed(mid):
        with _dl_lock:
            if not is_installed(mid):
                download(mid, ctx, p0, p1)
    return model_dir(mid)


# ---------------------------------------------------------------- environment
def ai_python() -> Path | None:
    import os
    env = os.environ.get("LIGXT_AI_PYTHON")
    if env:
        return Path(env)
    for p in (config.ROOT / "ai-venv" / "Scripts" / "python.exe", config.ROOT / "ai-venv" / "bin" / "python"):
        if p.exists():
            return p
    return None


def other_models() -> list[dict]:
    """Other model files on disk (not downloaded by this manager)."""
    out = []
    wdir = config.ROOT / "models" / "whisper"
    if wdir.exists():
        for d in sorted(wdir.glob("models--*")):
            name = d.name.split("--")[-1]
            out.append({"id": "whisper:" + name, "name": f"Speech recognition: {name} (captions)", "license": "MIT",
                        "source": "https://huggingface.co/Systran/" + name, "diskBytes": _dir_size(d), "deletable": True,
                        "about": "Downloaded by Auto-captions on first use."})
    yunet = config.ROOT / "models" / "face_detection_yunet_2023mar.onnx"
    if yunet.exists():
        out.append({"id": "yunet", "name": "Face detector (YuNet)", "license": "MIT", "diskBytes": yunet.stat().st_size,
                    "deletable": False, "source": "https://github.com/opencv/opencv_zoo",
                    "about": "Bundled: safe areas and template framing."})
    return out


_probe_cache: dict = {"t": 0.0, "data": None}


def probe(refresh: bool = False) -> dict:
    from . import ai_bridge
    if not refresh and _probe_cache["data"] and time.time() - _probe_cache["t"] < 600:
        return _probe_cache["data"]
    py = ai_python()
    if not py:
        data = {"installed": False, "error": "The AI tools are not installed. Run setup.ps1 (it builds ai-venv) and restart Ligxt."}
    else:
        try:
            data = {"installed": True, **ai_bridge.call("probe", {}, timeout=120)}
        except Exception as e:  # noqa: BLE001
            data = {"installed": True, "error": f"The AI environment did not start: {e}"}
    _probe_cache.update(t=time.time(), data=data)
    return data


@router.get("/status")
def status(refresh: bool = False):
    s = load_settings()
    models = []
    for mid, spec in CATALOG.items():
        models.append({"id": mid, "name": spec["name"], "task": spec["task"], "license": spec["license"],
                       "source": spec["source"], "about": spec["about"], "downloadBytes": download_size(mid),
                       "installed": is_installed(mid), "diskBytes": _dir_size(model_dir(mid)),
                       "jobId": _downloading.get(mid)})
    from . import ai_bridge
    return {"settings": s, "env": probe(refresh) if not ai_bridge.busy() or refresh else (_probe_cache["data"] or {"installed": bool(ai_python())}),
            "models": models, "other": other_models(),
            "totalBytes": sum(m["diskBytes"] for m in models) + sum(m["diskBytes"] for m in other_models()),
            "cacheBytes": cutout_cache_bytes()}


def cutout_cache_bytes() -> int:
    return sum(_dir_size(d) for d in config.CACHE_DIR.glob("*/cutout"))


@router.post("/models/{mid}/download")
def start_download(mid: str):
    if mid not in CATALOG:
        raise HTTPException(404, "Unknown model.")
    with _dl_lock:
        jid = _downloading.get(mid)
        j = jobs.get(jid) if jid else None
        if j and j.status in ("queued", "running"):
            return {"jobId": jid}

    def run(ctx: JobContext) -> dict:
        try:
            download(mid, ctx)
        finally:
            _downloading.pop(mid, None)
        ctx.message("Downloaded and verified")
        return {"model": mid, "diskBytes": _dir_size(model_dir(mid))}

    job = jobs.submit("model-download", f"Download {CATALOG[mid]['name']}", run, queue="media", ref={"model": mid})
    _downloading[mid] = job.id
    return {"jobId": job.id}


@router.delete("/models/{mid}")
def delete_model(mid: str):
    if mid.startswith("whisper:"):
        name = mid.split(":", 1)[1]
        d = config.ROOT / "models" / "whisper" / f"models--Systran--{name}"
        if not d.exists() or not name.replace("-", "").replace(".", "").isalnum():
            raise HTTPException(404, "Not found.")
        shutil.rmtree(d, ignore_errors=True)
        return {"ok": True}
    if mid not in CATALOG:
        raise HTTPException(404, "Unknown model.")
    from . import ai_bridge
    ai_bridge.stop()  # the worker may hold the files open
    shutil.rmtree(model_dir(mid), ignore_errors=True)
    return {"ok": True}


@router.delete("/cutout-cache")
def clear_cutout_cache():
    freed = 0
    for d in config.CACHE_DIR.glob("*/cutout"):
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
    return {"freed": freed}
