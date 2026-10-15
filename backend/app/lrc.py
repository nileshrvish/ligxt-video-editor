"""Synced lyrics lookup (Phase 13b): LRCLIB's public API (https://lrclib.net, free, no key), opt-in per request.

Only the artist / title (+ album and duration when known) are sent. Nothing is cached or stored: the fetched lyrics
go straight into the job result, and from there only into the project's text clips.

- parse(): LRC text -> [(seconds, line)] (several time tags per line, [offset:], enhanced <mm:ss> word tags removed).
- locate(): where the clip's range sits in the song. The music clip may be an excerpt (a 23 s reel cut of a 3 min
  song), so the offset between the media's time and the LRC's time is searched: words from a short Whisper pass on
  the vocal stem vote for offsets (word text found in an LRC line -> that line's time - the word's time), and the
  per-second vocal presence is correlated with "a lyric line is being sung" (line start to its estimated end).
- lines_in_range(): the LRC lines inside the clip, with their start/end in media (source) seconds.
"""
from __future__ import annotations

import json
import re
import subprocess
import unicodedata
import urllib.parse
import urllib.request

import numpy as np

from .config import FFPROBE

API = "https://lrclib.net/api"
UA = "Ligxt Video Editor (local video editor; lyrics captions)"
_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2}(?:[.:]\d{1,3})?)\]")
_WORD_TAG = re.compile(r"<\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?>")
_OFFSET = re.compile(r"\[offset:\s*([+-]?\d+)\s*\]", re.I)


# ---------------------------------------------------------------- tags + API
def media_tags(path: str) -> dict:
    """artist / title / album / duration from the file's tags (ffprobe), as far as they exist."""
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration:format_tags", "-of", "json", path],
                       capture_output=True, text=True, encoding="utf-8")
    try:
        fmt = json.loads(r.stdout or "{}").get("format", {})
    except ValueError:
        fmt = {}
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    out = {"artist": tags.get("artist") or tags.get("album_artist") or "", "title": tags.get("title") or "",
           "album": tags.get("album") or ""}
    try:
        out["duration"] = round(float(fmt.get("duration")), 2)
    except (TypeError, ValueError):
        out["duration"] = None
    return out


def _get(url: str, timeout: float = 12.0):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def search(artist: str, title: str, album: str = "") -> list[dict]:
    """Candidates (no lyrics text): id, artist, title, album, duration, synced."""
    q = {"track_name": title.strip()}
    if artist.strip():
        q["artist_name"] = artist.strip()
    if album.strip():
        q["album_name"] = album.strip()
    rows = _get(f"{API}/search?{urllib.parse.urlencode(q)}")
    if not rows and artist.strip():
        rows = _get(f"{API}/search?{urllib.parse.urlencode({'q': f'{artist} {title}'.strip()})}")
    out = []
    for r in rows[:20]:
        out.append({"id": r.get("id"), "artist": r.get("artistName"), "title": r.get("trackName"), "album": r.get("albumName"),
                    "duration": r.get("duration"), "synced": bool(r.get("syncedLyrics")), "instrumental": bool(r.get("instrumental"))})
    return sorted(out, key=lambda x: (not x["synced"], x["instrumental"]))


def fetch(lrclib_id: int) -> dict:
    r = _get(f"{API}/get/{int(lrclib_id)}")
    return {"id": r.get("id"), "artist": r.get("artistName"), "title": r.get("trackName"), "duration": r.get("duration"),
            "synced": r.get("syncedLyrics") or "", "instrumental": bool(r.get("instrumental"))}


# ---------------------------------------------------------------- LRC
def parse(text: str) -> list[tuple[float, str]]:
    """[(seconds, line)] sorted; empty lines (instrumental gaps) are kept as "" so they end the previous line."""
    off = 0.0
    m = _OFFSET.search(text or "")
    if m:
        off = int(m.group(1)) / 1000.0  # positive offset = lyrics appear sooner
    out = []
    for raw in (text or "").replace("\r", "").split("\n"):
        tags = list(_TAG.finditer(raw))
        if not tags:
            continue
        body = _WORD_TAG.sub("", raw[tags[-1].end():]).strip()
        for t in tags:
            mm, ss = int(t.group(1)), float(t.group(2).replace(":", "."))
            out.append((max(0.0, mm * 60 + ss - off), body))
    out.sort(key=lambda x: x[0])
    return out


def line_spans(lrc: list[tuple[float, str]], song_end: float | None = None) -> list[tuple[float, float, str]]:
    """(start, end, text) per non-empty line: until the next tag, at most ~0.45 s per word + 1.5 s."""
    out = []
    for i, (t, s) in enumerate(lrc):
        if not s:
            continue
        nxt = lrc[i + 1][0] if i + 1 < len(lrc) else (song_end or t + 8.0)
        est = 1.5 + 0.45 * len(s.split())
        out.append((t, max(t + 0.3, min(nxt, t + est)), s))
    return out


def _norm(w: str) -> str:
    w = unicodedata.normalize("NFKC", w).lower()
    return re.sub(r"[\W_]+", "", w)


def locate(spans: list[tuple[float, float, str]], presence: list[float], a: float, words: list[dict] | None = None,
           max_offset: float | None = None) -> dict:
    """Offset `off` so that song_time = media_time + off. presence[k] is second k of the clip's source range [a, a+n).
    words: Whisper words in media seconds (optional). Returns {offset, score, method, votes}."""
    n = len(presence)
    hi = max_offset if max_offset is not None else (max((e for _, e, _ in spans), default=0.0) + 5.0)
    cands = np.arange(-5.0, max(0.0, hi) + 1e-6, 0.1)
    pres = np.asarray(presence, float)

    def sung(t: np.ndarray) -> np.ndarray:
        v = np.zeros_like(t)
        for s, e, _ in spans:
            v[(t >= s) & (t < e)] = 1.0
        return v

    tt = a + np.arange(n) + 0.5
    corr = np.full(len(cands), -1.0)
    if pres.std() > 1e-6:
        for i, off in enumerate(cands):
            L = sung(tt + off)
            if L.std() > 1e-6:
                corr[i] = float(np.corrcoef(pres, L)[0, 1])
    votes = np.zeros(len(cands))
    if words:
        line_tokens = [(s, e, {_norm(x) for x in txt.split() if len(_norm(x)) >= 2}) for s, e, txt in spans]
        for w in words:
            k = _norm(w["text"])
            if len(k) < 2:
                continue
            for s, e, toks in line_tokens:
                if k in toks:
                    lo, hi2 = s - w["start"] - 0.5, e - w["start"] + 0.5  # the word is sung somewhere inside the line
                    votes[(cands >= lo) & (cands <= hi2)] += 1.0 / max(1, sum(1 for _, _, t2 in line_tokens if k in t2))
    score = np.maximum(corr, 0) + (votes / votes.max() if votes.max() > 0 else 0) * 1.5
    i = int(np.argmax(score))
    method = "words + vocal presence" if votes.max() > 0 else "vocal presence"
    return {"offset": round(float(cands[i]), 2), "score": round(float(score[i]), 3), "method": method,
            "presenceCorr": round(float(corr[i]), 3), "wordVotes": round(float(votes[i]), 2)}


def lines_in_range(spans: list[tuple[float, float, str]], off: float, a: float, b: float) -> list[dict]:
    """LRC lines that start inside the clip's source range, in media seconds."""
    out = []
    for s, e, txt in spans:
        ms, me = s - off, e - off
        if a - 0.25 <= ms < b - 0.2:
            out.append({"text": txt, "start": round(ms, 3), "end": round(min(me, b), 3)})
    return out
