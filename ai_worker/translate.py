"""Lyrics translation (ops `convert_mt` and `translate`) with CTranslate2 int8.

Models (pinned and verified by the backend's model manager, then converted once on this computer):
- M2M100 1.2B (facebook, MIT): many-to-many; the source language is given (Whisper's or the user's).
- MADLAD-400 3B MT (google, Apache-2.0), "Best": a "<2xx>" prefix names the target; loaded as float16 to convert.

convert_mt args: src (downloaded HF folder), out (CT2 folder), kind ("m2m100" | "madlad")
translate args: model (CT2 folder), kind, lines [str], source (code or None), target (code), device,
                mode ("test-translate" = no model: "[xx] line", for tests)
returns {"lines": [str], "device", "seconds"}
"""
from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

# the tokenizer's files only: the converted folder's config.json is CTranslate2's own and must not be overwritten
TOKENIZER_FILES = ("sentencepiece.bpe.model", "vocab.json", "tokenizer_config.json", "special_tokens_map.json",
                   "spiece.model", "tokenizer.json", "added_tokens.json")
_tr = {"key": None, "t": None, "tok": None, "device": None}


def convert(ctx, args: dict) -> dict:
    t0 = time.time()
    os.environ["TORCH_FORCE_WEIGHTS_ONLY_LOAD"] = "1"  # the M2M100 checkpoint is a pickle: tensors only
    from ctranslate2.converters import TransformersConverter

    class Converter(TransformersConverter):
        def load_model(self, model_class, model_name_or_path, **kwargs):
            # CTranslate2 4.8 passes `dtype=` (newer transformers); the pinned transformers 4.46 wants `torch_dtype=`
            if "dtype" in kwargs:
                kwargs["torch_dtype"] = kwargs.pop("dtype")
            return super().load_model(model_class, model_name_or_path, **kwargs)

    src, out = Path(args["src"]), Path(args["out"])
    tmp = out.with_name(out.name + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    ctx.progress(0.05, "Converting the translation model (once)", force=True)
    Converter(str(src), load_as_float16=args.get("kind") == "madlad", low_cpu_mem_usage=True).convert(
        str(tmp), quantization="int8", force=True)
    for f in TOKENIZER_FILES:
        if (src / f).exists():
            shutil.copyfile(src / f, tmp / f)
    shutil.rmtree(out, ignore_errors=True)
    tmp.replace(out)
    return {"seconds": round(time.time() - t0, 1), "files": sorted(p.name for p in out.iterdir())}


def _load(model: str, kind: str, device: str):
    import ctranslate2
    import transformers
    dev = "cuda" if device != "cpu" and ctranslate2.get_cuda_device_count() > 0 else "cpu"
    key = (model, dev)
    if _tr["key"] != key:
        from matting import unload
        unload()  # one GPU model at a time
        t = ctranslate2.Translator(model, device=dev, compute_type="int8_float16" if dev == "cuda" else "int8")
        cls = transformers.M2M100Tokenizer if kind == "m2m100" else transformers.T5TokenizerFast
        tok = cls.from_pretrained(model)
        _tr.update(key=key, t=t, tok=tok, device=dev)
    return _tr["t"], _tr["tok"], _tr["device"]


def translate(ctx, args: dict) -> dict:
    t0 = time.time()
    lines = [str(x) for x in args["lines"]]
    tgt = args["target"]
    if args.get("mode") == "test-translate":
        return {"lines": [f"[{tgt}] {ln}" for ln in lines], "device": "cpu", "seconds": 0.0}
    ctx.progress(0.05, "Loading the translation model", force=True)
    t, tok, dev = _load(args["model"], args["kind"], args.get("device", "auto"))
    out = []
    step = 16
    for i in range(0, len(lines), step):
        ctx.check()
        ctx.progress(0.1 + 0.85 * i / max(1, len(lines)), f"Translating {i + 1}-{min(len(lines), i + step)} of {len(lines)} lines")
        batch = lines[i:i + step]
        if args["kind"] == "m2m100":
            tok.src_lang = args.get("source") or "en"
            src = [tok.convert_ids_to_tokens(tok.encode(x)) for x in batch]
            lang_tok = tok.get_lang_token(tgt) if hasattr(tok, "get_lang_token") else f"__{tgt}__"
            prefix = [[lang_tok] for _ in batch]
            res = t.translate_batch(src, target_prefix=prefix, beam_size=4, max_decoding_length=256)
            out += [tok.decode(tok.convert_tokens_to_ids(r.hypotheses[0][1:]), skip_special_tokens=True) for r in res]
        else:  # madlad: "<2xx> text"
            src = [tok.convert_ids_to_tokens(tok.encode(f"<2{tgt}> {x}")) for x in batch]
            res = t.translate_batch(src, beam_size=4, max_decoding_length=256)
            out += [tok.decode(tok.convert_tokens_to_ids(r.hypotheses[0]), skip_special_tokens=True) for r in res]
    return {"lines": [x.strip() for x in out], "device": dev, "seconds": round(time.time() - t0, 2)}


def unload() -> None:
    _tr.update(key=None, t=None, tok=None, device=None)
