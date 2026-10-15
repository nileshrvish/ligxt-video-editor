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
    "whisper-large-v3": {
        "name": "Whisper large-v3 (song lyrics, non-English)", "task": "speech", "license": "MIT",
        "source": "https://huggingface.co/Systran/faster-whisper-large-v3", "res": 0, "dynamic": False,
        "about": "OpenAI's Whisper large-v3 (MIT) in CTranslate2 format: slower than turbo but stronger on sung and "
                 "non-English vocals (Hindi and others). Runs with faster-whisper on the graphics card.",
        "files": _hf("Systran/faster-whisper-large-v3", "edaa852ec7e145841d8ffdb056a99866b5f0a478", [
            ("model.bin", 3087284237, "69f74147e3334731bc3a76048724833325d2ec74642fb52620eda87352e3d4f1"),
            ("config.json", 2394, "a9306624f5ec14270a014b647e5c316b6e03a662c369758d1b90697a7b0655b9"),
            ("preprocessor_config.json", 340, "7ccc62c6f2765af1f3b46c00c9b5894426835a05021c8b9c01eecb6dfb542711"),
            ("tokenizer.json", 2480617, "6d8cbd7cd0d8d5815e478dac67b85a26bbe77c1f5e0c6d76d1ce2abc0e5f21ca"),
            ("vocabulary.json", 1068114, "c69260f2ab26d659b7c398f9a2b2b48ed0df16c3b47d7326782fd9cba71690c1"),
        ]),
    },
    "mel-roformer-vocals": {
        "name": "Mel-Band RoFormer vocals (vocal separation)", "task": "separation", "license": "MIT",
        "source": "https://huggingface.co/KimberleyJSN/melbandroformer", "res": 0, "dynamic": False,
        "about": "Kimberley Jensen's Mel-Band RoFormer vocal model (MIT): separates the singing from the music so lyrics "
                 "can be heard and timed. Run with python-audio-separator (MIT); loaded as weights only.",
        "files": _hf("KimberleyJSN/melbandroformer", "ac9b0614ab3cd7f77219e18ba494dfd93956c348", [
            ("MelBandRoformer.ckpt", 913106900, "87201f4d31afb5bc79993230fc49446918425574db48c01c405e44f365c7559e"),
        ]),
    },
    "uvr-deecho-dereverb": {
        "name": "UVR DeEcho-DeReverb (vocal clean-up)", "task": "dereverb", "license": "Not stated",
        "userApproved": "2026-10-16: enabled at the user's request (weights only, SHA-256 of the download pinned)",
        "source": "https://github.com/TRvlvr/model_repo/releases/tag/all_public_uvr_models", "res": 0, "dynamic": False,
        "about": "Ultimate Vocal Remover's echo / reverb remover (attributed to FoxJoy), applied to the separated vocals. "
                 "No license is stated by its author; enabled at the user's request (2026-10-16). Loaded as weights only.",
        # no published checksum: SHA-256 of the download (identical on the UVR and audio-separator release hosts)
        "files": [{"name": "UVR-DeEcho-DeReverb.pth", "size": 223650277,
                   "sha256": "e644028ec82865dc0fe082bc6fea85a43f7c71cfe375caee2da2d154aa661ee7",
                   "url": "https://github.com/TRvlvr/model_repo/releases/download/all_public_uvr_models/UVR-DeEcho-DeReverb.pth"}],
    },
    "m2m100-1.2b": {
        "name": "M2M100 1.2B (lyrics translation)", "task": "translation", "license": "MIT",
        "source": "https://huggingface.co/facebook/m2m100_1.2B", "res": 0, "dynamic": False,
        "about": "Meta's many-to-many translation model (MIT, 100 languages). Downloaded once (5 GB), converted on this "
                 "computer to CTranslate2 int8 (1.2 GB) and the original is removed.",
        "files": _hf("facebook/m2m100_1.2B", "7b36184180524c1a1bbfa37f120a608046250b98", [
            ("pytorch_model.bin", 4958230644, "a58ef8f42362ef12adeddc600b3425f1e2bbd019cfa6aae6b0051e2e3e055cd4"),
            ("sentencepiece.bpe.model", 2423393, "d8f7c76ed2a5e0822be39f0a4f95a55eb19c78f4593ce609e2edbc2aea4d380a"),
            ("config.json", 909, "13b5191c92371b2d587baac6c9a04de018a5264d7179020d379a276769051f95"),
            ("generation_config.json", 233, "aed76366507333ddbb8bd49960f23c82fe6446b3319a46a54befdb45324ccf61"),
            ("special_tokens_map.json", 1140, "c1a4f86c3874d279ae1b2a05162858db5dd6c61665d84223ed886cbcff08fda6"),
            ("tokenizer_config.json", 271, "1566a6cfa4f541a55594c9d5e090f530812d5de7c94882ea3af156962d9933ae"),
            ("vocab.json", 3708092, "b6e77e474aeea8f441363aca7614317c06381f3eacfe10fb9856d5081d1074cc"),
        ]),
        "convert": {"kind": "m2m100", "out": "ct2-int8"},
    },
    "madlad400-3b": {
        "name": "MADLAD-400 3B (lyrics translation, Best)", "task": "translation", "license": "Apache-2.0",
        "source": "https://huggingface.co/google/madlad400-3b-mt", "res": 0, "dynamic": False,
        "about": "Google's MADLAD-400 machine translation model (Apache-2.0, 400+ languages): better on less common "
                 "languages. Downloaded once (11.8 GB), converted on this computer to CTranslate2 int8 (about 3 GB) and the "
                 "original is removed. Google calls it a research model.",
        "files": _hf("google/madlad400-3b-mt", "fa184c675da0b5c9e1c8694fccd4e12e2d422094", [
            ("model.safetensors", 11761587872, "66ff5f8fcaf92291da486fdfbd4d5233cec90e1359348a56e3172c978b3a76d4"),
            ("spiece.model", 4427844, "ef11ac9a22c7503492f56d48dce53be20e339b63605983e9f27d2cd0e0f3922c"),
            ("tokenizer.json", 16629031, "a2799ccc696b752ba00c34f58726bfe253a04921ceb6cfc620400f560474790b"),
            ("added_tokens.json", 4, "d914176fd50bd7f565700006a31aa97b79d3ad17cee20c8e5ff2061d5cb74817"),
            ("config.json", 749, "cad399cab799b99409a6ec2d90d72552257c2bb752861261d2016691e0643e7c"),
            ("generation_config.json", 142, "0849b38987568ccfe4ebefc22bbda1cec4bee01c345e93bfab207d4692b0a1d5"),
            ("special_tokens_map.json", 414, "7f79f1d5063d56c4b980eec0692f3c7429bdef335071d34e566bd00fd4b5e3e0"),
            ("tokenizer_config.json", 830, "641fc660745306dfb935f666a68f8bc10a44c39241cfb357be518fda8c09662d"),
        ]),
        "convert": {"kind": "madlad", "out": "ct2-int8"},
    },
}
PERSON_MODELS = [k for k, v in CATALOG.items() if v["task"] == "person"]
LYRICS_MODEL = "whisper-large-v3-turbo"
LYRICS_MODEL_V3 = "whisper-large-v3"
SEPARATION_MODEL = "mel-roformer-vocals"
DEREVERB_DEFAULT = False  # Phase 13b measurement: +0-1 words, higher confidence, correctness unverified -> opt-in
TRANSLATION_MODELS = {"good": "m2m100-1.2b", "best": "madlad400-3b"}
DEREVERB_MODEL = "uvr-deecho-dereverb"
RIFE_MODEL = "rife-4.26"
OBJECT_MODEL = "sam2.1-small"
DEFAULT_SETTINGS = {"device": "auto", "budgetGB": 4.5, "personModel": "birefnet",  # best quality on the test clips
                    "separator": "mel-roformer", "dereverb": DEREVERB_DEFAULT}
SEPARATORS = {"mel-roformer": SEPARATION_MODEL, "off": None}  # the only separator for now (user decision, Phase 13b)


# ---------------------------------------------------------------- settings
def load_settings() -> dict:
    try:
        s = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        s = {}
    out = {**DEFAULT_SETTINGS, **{k: v for k, v in s.items() if k in DEFAULT_SETTINGS}}
    if out["personModel"] not in CATALOG:
        out["personModel"] = DEFAULT_SETTINGS["personModel"]
    if out["separator"] not in SEPARATORS:
        out["separator"] = DEFAULT_SETTINGS["separator"]
    out["dereverb"] = bool(out["dereverb"])
    out["budgetGB"] = max(1.0, min(5.5, float(out["budgetGB"])))
    return out


class SettingsBody(BaseModel):
    device: str | None = None
    budgetGB: float | None = None
    personModel: str | None = None
    separator: str | None = None
    dereverb: bool | None = None


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
    if body.separator is not None:
        if body.separator not in SEPARATORS:
            raise HTTPException(400, "Unknown vocal separator.")
        s["separator"] = body.separator
    if body.dereverb is not None:
        s["dereverb"] = bool(body.dereverb)
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
    if spec.get("convert"):
        return (d / spec["convert"]["out"] / "model.bin").exists()
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
    if spec.get("convert"):  # convert once in the AI worker (needs PyTorch), then drop the original weights
        from . import ai_bridge
        if ctx:
            ctx.progress(p1, f"Converting {spec['name']} (once; a few minutes)")
        ai_bridge.call("convert_mt", {"src": str(d), "out": str(d / spec["convert"]["out"]), "kind": spec["convert"]["kind"]},
                       cancelled=ctx.cancelled if ctx else None)
        for f in spec["files"]:
            (d / f["name"]).unlink(missing_ok=True)
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
            "cacheBytes": cutout_cache_bytes(), "stemsBytes": stems_cache_bytes()}


def cutout_cache_bytes() -> int:
    return sum(_dir_size(d) for d in config.CACHE_DIR.glob("*/cutout"))


def stems_cache_bytes() -> int:
    return sum(_dir_size(d) for d in config.CACHE_DIR.glob("*/stems"))


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


@router.delete("/stems-cache")
def clear_stems_cache():
    """Separated vocals (song lyrics) are kept per song range; they are rebuilt when needed."""
    freed = 0
    for d in config.CACHE_DIR.glob("*/stems"):
        freed += _dir_size(d)
        shutil.rmtree(d, ignore_errors=True)
    return {"freed": freed}
