"""Hindi in Latin letters for lyrics (Phase 13b): Devanagari -> IAST with indic-transliteration (MIT), then the
word-final inherent "a" that Hindi doesn't pronounce is dropped (कमल -> kamal, प्यार -> pyār), and optionally the
diacritics too, for the casual spelling people use in captions (pyar, duniya). Non-Devanagari text is unchanged."""
from __future__ import annotations

import re
import unicodedata

_DEV = re.compile(r"[ऀ-ॿ]+")
_CONS = set(range(0x0915, 0x093A)) | set(range(0x0958, 0x0960))  # consonants (incl. nukta forms)


def _word(w: str, diacritics: bool) -> str:
    from indic_transliteration import sanscript as S
    out = S.transliterate(w, S.DEVANAGARI, S.IAST)
    last = w[-1]
    if ord(last) in _CONS and len(w) > 1 and out.endswith("a"):  # schwa deletion at the end of the word
        out = out[:-1]
    out = out.replace("~", "ṃ").replace("m̐", "ṃ")  # candrabindu (हूँ) like anusvara
    if not diacritics:
        out = out.replace("ṃ", "n").replace("ṁ", "n")  # before the marks are stripped (ṃ = m + dot below)
        out = "".join(c for c in unicodedata.normalize("NFD", out) if not unicodedata.combining(c))
    return out


def romanize(text: str, diacritics: bool = False) -> str:
    return _DEV.sub(lambda m: _word(m.group(0), diacritics), text)


def has_devanagari(text: str) -> bool:
    return bool(_DEV.search(text or ""))
