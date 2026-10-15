"""Vocal separation (op `separate`) with python-audio-separator (MIT): Mel-Band RoFormer vocals (Kimberley Jensen, MIT),
then optionally UVR DeEcho-DeReverb on the vocal stem.

Nothing is fetched at run time: the model files come from the backend's model manager (pinned, SHA-256 verified), the
RoFormer config and the VR model parameters are vendored in ai_worker/configs/. Checkpoints are loaded as weights only
(TORCH_FORCE_WEIGHTS_ONLY_LOAD). GPU memory is capped at the user's budget; audio-separator splits the song into chunks.

args: wav (44.1 kHz stereo WAV), out_vocals (WAV path), roformer (model folder), dereverb (model folder or None),
      ffmpeg (path), device, budgetGB, mode ("test-separate" = no model: a numpy voice-band mask, for tests).
returns {"device", "seconds", "steps": [...], "peakVram"}
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

SR = 44100
HERE = Path(__file__).resolve().parent
ROFORMER_FILE = "MelBandRoformer.ckpt"
ROFORMER_YAML = HERE / "configs" / "mel_band_roformer_vocals.yaml"
DEREVERB_FILE = "UVR-DeEcho-DeReverb.pth"
_sep = {"key": None, "s": None}


# ---------------------------------------------------------------- audio io
def read_wav(path: str) -> np.ndarray:
    """(samples, channels) float32 in -1..1."""
    with wave.open(path, "rb") as w:
        n, ch = w.getnframes(), w.getnchannels()
        if w.getsampwidth() != 2:
            raise ValueError("separate expects 16-bit audio")
        x = np.frombuffer(w.readframes(n), np.int16).astype(np.float32) / 32768
    return x.reshape(-1, ch)


def write_wav(path: str, x: np.ndarray, sr: int = SR) -> None:
    y = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(y.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())


def _test_separate(x: np.ndarray) -> np.ndarray:
    """No model (tests): per 2048-sample frame keep the 250-4000 Hz band where it is peaky (harmonic, voice-like); the
    test songs' beds sit at 220 Hz and below."""
    n, hop = 2048, 512
    mono = x.mean(1)
    out = np.zeros_like(mono)
    norm = np.zeros_like(mono)
    win = np.hanning(n).astype(np.float32)
    f = np.fft.rfftfreq(n, 1 / SR)
    band = (f >= 250) & (f <= 4000)
    for i in range(0, max(1, len(mono) - n), hop):
        S = np.fft.rfft(mono[i:i + n] * win)
        mag = np.abs(S[band])
        keep = mag.max() / (np.median(mag) + 1e-9) > 40 if mag.size else False
        m = np.zeros(S.shape, np.float32)
        if keep:
            m[band] = 1.0
        out[i:i + n] += np.fft.irfft(S * m, n).astype(np.float32) * win
        norm[i:i + n] += win ** 2
    out /= np.maximum(norm, 1e-3)
    return np.repeat(out[:, None], 2, 1)


# ---------------------------------------------------------------- audio-separator, offline
def _separator(out_dir: str, files: dict, device: str):
    """A Separator whose model lookup uses our verified local files instead of fetching lists, configs and model data."""
    import logging
    from audio_separator.separator import Separator

    vr_data = json.loads((HERE / "configs" / "vr_models.json").read_text(encoding="utf-8"))

    class Offline(Separator):
        def list_supported_model_files(self):  # never fetch the online model lists
            return {}

        def download_model_files(self, model_filename):
            if model_filename == ROFORMER_FILE:
                return model_filename, "MDXC", "Mel-Band RoFormer vocals", files[ROFORMER_FILE], str(ROFORMER_YAML)
            if model_filename == DEREVERB_FILE:
                return model_filename, "VR", "UVR DeEcho-DeReverb", files[DEREVERB_FILE], None
            raise ValueError(f"{model_filename} is not an installed separation model")

        def load_model_data_using_hash(self, model_path):
            info = vr_data[Path(model_path).name]
            if self.get_model_hash(model_path) != info["hash"]:
                raise RuntimeError(f"{Path(model_path).name} does not match its recorded parameters")
            return {k: v for k, v in info.items() if k != "hash"}

    return Offline(log_level=logging.WARNING, model_file_dir=out_dir, output_dir=out_dir, output_format="WAV",
                   sample_rate=SR, use_soundfile=True, use_autocast=device == "cuda")


def _run(sep, model_file: str, wav: str, stem: str, name: str) -> str:
    sep.output_single_stem = stem
    if sep.model_filename != model_file or sep.model_instance is None:
        sep.load_model(model_file)
    sep.model_instance.output_single_stem = stem  # the instance copies the setting when it is created
    sep.model_instance.output_dir = sep.output_dir
    sep.separate(wav, custom_output_names={stem: name})
    path = Path(sep.output_dir) / f"{name}.wav"
    if not path.exists():
        raise RuntimeError(f"the separator wrote no {stem} stem")
    return str(path)


def separate(ctx, args: dict) -> dict:
    t0 = time.time()
    if args.get("mode") == "test-separate":
        write_wav(args["out_vocals"], _test_separate(read_wav(args["wav"])))
        return {"device": "cpu", "seconds": round(time.time() - t0, 3), "steps": ["test"], "peakVram": 0}
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"  # checkpoints are plain tensors: never unpickle code
    if args.get("ffmpeg"):
        os.environ["PATH"] = str(Path(args["ffmpeg"]).parent) + os.pathsep + os.environ.get("PATH", "")
    import torch
    dev = "cuda" if args.get("device", "auto") != "cpu" and torch.cuda.is_available() else "cpu"
    files = {ROFORMER_FILE: str(Path(args["roformer"]) / ROFORMER_FILE)}
    if args.get("dereverb"):
        files[DEREVERB_FILE] = str(Path(args["dereverb"]) / DEREVERB_FILE)
    key = (dev, tuple(sorted(files.items())))
    if _sep["key"] != key and dev == "cuda":
        from matting import unload
        unload()  # one GPU model at a time (this also drops a separator loaded with other settings)
    if dev == "cuda":
        total = torch.cuda.get_device_properties(0).total_memory
        torch.cuda.set_per_process_memory_fraction(min(0.95, float(args.get("budgetGB", 4.5)) * 2**30 / total))
        torch.cuda.reset_peak_memory_stats()
    steps = []
    with tempfile.TemporaryDirectory(prefix="ligxt_sep_") as tmp:
        if _sep["key"] != key:
            _sep.update(key=key, s=_separator(tmp, files, dev))
        sep = _sep["s"]
        sep.output_dir = sep.model_file_dir = tmp
        ctx.progress(0.05, "Separating the vocals (Mel-Band RoFormer)", force=True)
        voc = _run(sep, ROFORMER_FILE, args["wav"], "Vocals", "vocals")
        steps.append("mel-roformer")
        ctx.check()
        if DEREVERB_FILE in files:
            ctx.progress(0.6, "Removing echo and reverb from the vocals", force=True)
            voc = _run(sep, DEREVERB_FILE, voc, "No Reverb", "vocals_dry")
            steps.append("dereverb")
        ctx.check()
        n = len(read_wav(args["wav"]))
        y = read_wav(voc)  # the VR pass can end a few ms short (no shift): match the input length exactly
        write_wav(args["out_vocals"], np.pad(y, ((0, max(0, n - len(y))), (0, 0)))[:n])
    peak = int(torch.cuda.max_memory_allocated()) if dev == "cuda" else 0
    return {"device": dev, "seconds": round(time.time() - t0, 2), "steps": steps, "peakVram": peak}


def unload() -> None:
    _sep.update(key=None, s=None)
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
    except Exception:  # noqa: BLE001
        pass


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()
