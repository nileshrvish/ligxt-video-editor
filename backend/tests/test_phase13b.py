"""Phase 13b: vocal separation (python-audio-separator: Mel-Band RoFormer [+ DeEcho-DeReverb]) through the real worker
subprocess (numpy "test-separate" mode), cached stems, vocal presence from the stem (found where 13a's VAD on the full
mix missed it), synced lyrics (LRC parsing, finding the clip's place in the song, the lrc mode end to end), translation
(test mode through the worker; Hindi romanization), complex-script text (Devanagari / Arabic shaped per word, right to
left; Latin layout unchanged), and the real models when they are installed."""
import math
import os
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

os.environ["LIGXT_AI_PYTHON"] = sys.executable  # test modes need numpy only

from app import ai_bridge, ai_models, config  # noqa: E402
from app import lrc as LRC  # noqa: E402
from app import lyrics as LY  # noqa: E402
from app import stems as ST  # noqa: E402
from app import text as T  # noqa: E402
from app import translit  # noqa: E402
from app.media import load_meta  # noqa: E402
from app.models import Project  # noqa: E402
from test_phase1 import upload, wait_job  # noqa: E402

SR = 44100


def write(path: Path, x: np.ndarray, sr: int = SR):
    y = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    if y.ndim == 1:
        y = y[:, None]
    with wave.open(str(path), "wb") as w:
        w.setnchannels(y.shape[1])
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(y.tobytes())


def bed(seconds: float, level: float = 0.25) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    x = level * 0.5 * (np.sin(2 * np.pi * 110 * t) + np.sin(2 * np.pi * 165 * t) + 0.5 * np.sin(2 * np.pi * 220 * t))
    x += level * np.exp(-((t % 0.5) * 30)) * np.sin(2 * np.pi * 60 * t)
    return x


def sung(seconds: float, spans) -> np.ndarray:
    """A 'voice': harmonics of 300 Hz with vibrato, only inside spans."""
    t = np.arange(int(seconds * SR)) / SR
    f = 300 * (1 + 0.01 * np.sin(2 * np.pi * 5 * t))
    ph = 2 * np.pi * np.cumsum(f) / SR
    v = sum(np.sin(k * ph) / k for k in (1, 2, 3, 4))
    m = np.zeros_like(t)
    for a, b in spans:
        m[(t >= a) & (t < b)] = 1.0
    return 0.18 * v * m


@pytest.fixture(scope="module")
def song(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p13b")
    p = d / "voiced.wav"
    write(p, bed(12) + sung(12, [(2.0, 5.0), (7.0, 10.0)]))
    m = upload(client, p)
    yield m
    ai_bridge.stop()


def project_with(client, mid, *, start=0.0, inp=0.0, outp=12.0, speed=1.0):
    p = client.post("/api/projects", json={"name": "Lyrics 13b", "aspect": "9:16"}).json()
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "song", "mediaId": mid, "start": start, "inPoint": inp, "outPoint": outp, "speed": speed}]
    return p


# ---------------------------------------------------------------- separation + cache + presence
def test_separation_through_the_worker_is_cached_per_range_and_settings(client, song, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")
    m = load_meta(song["id"])
    a = ST.vocal_stem(m, 0.0, 12.0)
    assert a and a["path"].exists() and a["path"].suffix == ".flac" and not a["cached"]
    assert a["path"].parent == ST.stems_dir(song["id"])
    b = ST.vocal_stem(m, 0.0, 12.0)
    assert b["cached"] and b["path"] == a["path"]
    c = ST.vocal_stem(m, 1.0, 9.0)  # another section -> another stem
    assert c["path"] != a["path"]
    # settings are part of the key; off = no separation
    st = ai_models.load_settings()
    assert ST.stem_key(m["id"], 0, 12, {"separator": ai_models.SEPARATION_MODEL, "dereverb": False}) != \
        ST.stem_key(m["id"], 0, 12, {"separator": ai_models.SEPARATION_MODEL, "dereverb": True})
    monkeypatch.setattr(ai_models, "load_settings", lambda: {**st, "separator": "off"})
    assert ST.vocal_stem(m, 0.0, 12.0) is None
    monkeypatch.undo()
    s = client.get("/api/ai/status").json()
    assert s["stemsBytes"] > 0 and s["settings"]["separator"] == "mel-roformer" and s["settings"]["dereverb"] is False
    assert client.delete("/api/ai/stems-cache").json()["freed"] > 0
    assert client.get("/api/ai/status").json()["stemsBytes"] == 0


def test_vocal_presence_finds_vocals_under_loud_music_where_vad_on_the_mix_misses_them(tmp_path, monkeypatch):
    """Voice 10-13 dB under a loud, broadband bed: Silero VAD on the mix (13a) hears ~nothing; the stem's level
    against the mix (13b) marks exactly the sung seconds."""
    rng = np.random.default_rng(3)
    secs = 10
    t = np.arange(secs * SR) / SR
    loud = 0.35 * bed(secs, 1.0) + 0.12 * rng.standard_normal(len(t)) * (0.6 + 0.4 * np.sin(2 * np.pi * 2 * t))
    voice = sung(secs, [(3.0, 7.0)])
    mix = np.stack([loud + voice] * 2, 1)
    mix_p = tmp_path / "mix.wav"
    write(mix_p, mix)
    # 13a: VAD on the full mix
    w16 = tmp_path / "mix16.wav"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(mix_p), "-ac", "1", "-ar", "16000", str(w16)], check=True)
    vad = LY.voice_activity(w16, 0.0, 0.0, secs)
    # 13b: stem presence (here the stem is the clean voice, as a separator returns it)
    pres = ST.presence(mix, np.stack([voice] * 2, 1), 0.0, 0.0, secs)
    voiced = [3, 4, 5, 6]
    assert all(pres["perSecond"][k] >= 0.75 for k in voiced), pres
    assert all(pres["perSecond"][k] == 0.0 for k in range(secs) if k not in voiced), pres
    assert sum(vad["perSecond"][k] for k in voiced) / 4 < 0.5, vad  # the speech detector misses the singing
    assert ST.voiced_spans(pres, 0.0) == [[3.0, 7.0]]
    assert LY.chunks([[3.0, 7.0], [8.0, 9.0]]) == [[2.7, 9.3]]  # gaps under 1.5 s are joined, ends padded
    assert len(LY.chunks([[0.0, 60.0]])) == 3


def test_auto_and_paste_use_the_separated_vocals(client, song, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")
    p = project_with(client, song["id"], start=1.0, inp=0.0, outp=12.0)
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "auto"}).json()["jobId"])
    assert j["status"] == "done", j.get("error")
    r = j["result"]
    assert r["voice"]["method"] == "stem" and r["separation"]["separator"] == ai_models.SEPARATION_MODEL
    per = r["voice"]["perSecond"]
    assert len(per) == 12 and max(per[2:5]) > 0.5 and per[0] == 0.0 and per[6] == 0.0, per
    assert r["words"] and all(1.0 <= w["start"] < 13.0 for w in r["words"])  # placeholder words on voiced seconds (timeline)
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "align", "text": "one two\nthree four"}).json()["jobId"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["separation"]["cached"] and j["result"]["voice"]["method"] == "stem"


# ---------------------------------------------------------------- synced lyrics
LRC_TEXT = """[ar:Test]
[ti:Song]
[offset:+0]
[00:30.00]first line of the song
[00:34.50]second <00:35.10>line here
[00:39.00][01:10.00]the chorus line
[00:43.00]
[00:45.00]after a break
"""


def test_lrc_parse_and_spans():
    p = LRC.parse(LRC_TEXT)
    assert [round(t, 2) for t, _ in p] == [30.0, 34.5, 39.0, 43.0, 45.0, 70.0]
    assert p[1][1] == "second line here" and p[3][1] == ""
    sp = LRC.line_spans(p)
    assert len(sp) == 5 and sp[2][0] == 39.0 and sp[2][1] == pytest.approx(min(43.0, 39.0 + 1.5 + 0.45 * 3))
    assert LRC.parse("[offset:500]\n[00:10.00]x")[0][0] == pytest.approx(9.5)


def test_lrc_finds_the_clips_place_in_the_song():
    """The media is an excerpt that starts at 30 s of the song: words + vocal presence agree on the offset."""
    sp = LRC.line_spans(LRC.parse(LRC_TEXT))
    # presence of the excerpt (media seconds 0..20): voice where the lines are sung (song time - 30)
    pres = [0.0] * 20
    for s, e, _ in sp:
        for k in range(20):
            if s - 30 <= k + 0.5 < e - 30:
                pres[k] = 1.0
    loc = LRC.locate(sp, pres, 0.0, None, max_offset=60)
    assert loc["offset"] == pytest.approx(30.0, abs=0.6) and loc["method"] == "vocal presence"
    words = [{"text": "chorus", "start": 9.4}, {"text": "break", "start": 15.3}, {"text": "second", "start": 4.6}]
    loc = LRC.locate(sp, pres, 0.0, words, max_offset=60)
    assert loc["offset"] == pytest.approx(30.0, abs=0.3) and loc["method"] == "words + vocal presence"
    sel = LRC.lines_in_range(sp, loc["offset"], 0.0, 20.0)
    assert [x["text"] for x in sel] == ["first line of the song", "second line here", "the chorus line", "after a break"]
    assert sel[0]["start"] == pytest.approx(0.0, abs=0.3)


def test_lrc_mode_end_to_end_with_pasted_lrc(client, song, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")
    p = project_with(client, song["id"], start=2.0, inp=0.0, outp=12.0)
    lrc = "[00:02.00]sing the first line\n[00:07.00]and the second one\n"
    r = client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "lrc", "lrc": lrc})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"])
    assert j["status"] == "done", j.get("error")
    res = j["result"]
    assert [[w["text"] for w in ln] for ln in res["lines"]] == [["sing", "the", "first", "line"], ["and", "the", "second", "one"]]
    assert res["lrc"]["offset"] == pytest.approx(0.0, abs=0.6) and res["lrc"]["lines"] == 2
    assert all(2.0 <= w["start"] < 14.0 for ln in res["lines"] for w in ln)
    assert client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "lrc"}).status_code == 400


# ---------------------------------------------------------------- translation + romanization
def test_translation_through_the_worker_and_hindi_in_latin_letters(client, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")
    j = wait_job(client, client.post("/api/lyrics/translate", json={"lines": ["hello world", "second"], "target": "fr"}).json()["jobId"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["lines"] == ["[fr] hello world", "[fr] second"] and j["result"]["source"] == "en"
    hi = "नमस्ते दुनिया"
    assert LY.guess_language([hi]) == "hi" and LY.guess_language(["مرحبا"]) == "ar"
    assert translit.romanize(hi) == "namaste duniya"
    assert translit.romanize("कमल प्यार", diacritics=True) == "kamal pyār"
    assert translit.romanize("मैं घर जा रहा हूँ") == "main ghar ja raha hun"
    assert translit.romanize("Goa में") == "Goa men"
    assert client.post("/api/lyrics/romanize", json={"lines": [hi]}).json()["lines"] == ["namaste duniya"]
    assert client.post("/api/lyrics/translate", json={"lines": ["x"], "target": "xx"}).status_code == 400
    langs = client.get("/api/lyrics/languages").json()
    assert "hi" in langs["languages"] and set(langs["models"]) == {"good", "best"}


# ---------------------------------------------------------------- complex scripts in the renderer
def _alpha(lay):
    from PIL import Image
    return np.array(Image.open(T.TEXT_CACHE / lay["key"] / "base.png"))[..., 3]


def _ink(a):
    ys, xs = np.nonzero(a > 40)
    return a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def _reference(word, path, px):
    from PIL import Image, ImageDraw
    f = T._shaping_font(path, px, T.font_weight("Anton-Regular"))
    im = Image.new("L", (int(f.getlength(word)) + 200, int(px * 3)), 0)
    ImageDraw.Draw(im).text((100, int(px * 2)), word, font=f, anchor="ls", fill=255)
    return _ink(np.array(im))


@pytest.mark.parametrize("txt,rtl", [("नमस्ते क्षत्रिय", False),
                                     ("مرحبا بالعالم", True)])
def test_devanagari_and_arabic_are_shaped_per_word_like_raqm(txt, rtl):
    assert T.RAQM, "raqm (FriBiDi) must be available"
    spec = {"content": txt, "font": "Anton-Regular", "size": 80, "outline": {"width": 0}, "shadow": {"enabled": False}, "maxWidth": 2000}
    lay = T.build(spec)
    a = _alpha(lay)
    R, m = lay["res"], lay["m"]
    words = txt.split()
    assert [w["text"] for w in lay["words"]] == words  # reading order kept
    units = [c for c in lay["chars"] if c["c"].strip()]
    assert [c["c"] for c in units] == words  # one unit per shaped word
    xs = [c["tile"][0] for c in units]
    assert (xs[0] > xs[1]) if rtl else (xs[0] < xs[1])  # right to left for Arabic
    path = T.word_font("Anton-Regular", words[0])
    assert path and Path(path).name == ("NotoSansArabic-VF.ttf" if rtl else "NotoSansDevanagari-VF.ttf")
    for c in units:
        x0, x1 = int((c["tile"][0] + m) * R), int((c["tile"][2] + m) * R)
        ours, ref = _ink(a[:, max(0, x0):x1]), _reference(c["c"], path, 80 * R)
        assert ours.shape == ref.shape, (c["c"], ours.shape, ref.shape)
        assert np.abs(ours.astype(int) - ref.astype(int)).mean() < 4.0


def test_latin_layout_is_unchanged_and_mixed_lines_fall_back_per_word():
    spec = {"content": "Goa in golden hour", "font": "Anton-Regular", "size": 100, "letterSpacing": 2}
    lay = T.build(spec)
    f = T._font("Anton-Regular", 100 * T.RES)
    ln = "Goa in golden hour"
    # the original per-character layout: x = getlength(prefix) + spacing * i
    ox = lay["chars"][0]["anchor"][0]  # sanity: one unit per character
    assert len(lay["chars"]) == len(ln)
    xs = [f.getlength(ln[:i]) + 2 * T.RES * i for i in range(len(ln))]
    lefts = [c["tile"][0] for c in lay["chars"]]
    assert all(b >= a for a, b in zip(lefts, lefts[1:])) and ox > 0
    assert lay["w"] == math.ceil((f.getlength(ln) + 2 * T.RES * (len(ln) - 1)) / T.RES)
    assert xs[1] > xs[0]
    mixed = T.build({"content": "Goa में", "font": "Anton-Regular", "size": 100})
    assert [c["c"] for c in mixed["chars"]] == ["G", "o", "a", " ", "में"]
    assert T.line_is_plain("Anton-Regular", "Goa in golden hour") and not T.line_is_plain("Anton-Regular", "Goa में")


# ---------------------------------------------------------------- real models (when installed)
def _sapi(text: str, out: Path) -> bool:
    ps = (f"Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.Rate = -1; $s.SetOutputToWaveFile('{out}'); $s.Speak('{text}'); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 10000


AI_PY = Path(config.ROOT) / "ai-venv" / "Scripts" / "python.exe"


@pytest.mark.skipif(not (AI_PY.exists() and ai_models.is_installed(ai_models.SEPARATION_MODEL) and ai_models.is_installed(ai_models.LYRICS_MODEL)),
                    reason="Mel-Band RoFormer / large-v3-turbo / ai-venv are not installed")
def test_real_separation_finds_a_voice_under_loud_music_and_whisper_hears_it(client, tmp_path, monkeypatch):
    monkeypatch.delenv("LIGXT_LYRICS_TEST", raising=False)
    sentence = "the quick brown fox jumps over the lazy dog"
    voice = tmp_path / "voice.wav"
    if not _sapi(sentence, voice):
        pytest.skip("Windows speech synthesis is not available")
    secs = 10
    rng = np.random.default_rng(5)
    t = np.arange(secs * SR) / SR
    loud = 0.35 * bed(secs, 1.0) + 0.1 * rng.standard_normal(len(t))
    b = tmp_path / "bed.wav"
    write(b, np.stack([loud] * 2, 1))
    song_p = tmp_path / "song.wav"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(b), "-i", str(voice), "-filter_complex",
                    "[1]volume=0.9,adelay=2500:all=1[v];[0][v]amix=inputs=2:duration=first:normalize=0", "-ar", "44100", "-ac", "2", str(song_p)], check=True)
    m = upload(client, song_p)
    p = project_with(client, m["id"], outp=float(secs))
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(AI_PY))
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "auto", "model": "large-v3-turbo"}).json()["jobId"], timeout=900)
    ai_bridge.stop()
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    r = j["result"]
    per = r["voice"]["perSecond"]
    assert r["voice"]["method"] == "stem" and r["separation"]["device"] in ("cuda", "cpu")
    assert max(per[:2]) < 0.3 and np.mean(per[3:6]) > 0.6, per  # silence before the voice, vocals while it speaks
    got = [w["text"].lower().strip(".,!?") for w in r["words"]]
    assert sum(1 for w in sentence.split() if w in got) >= 6, got


@pytest.mark.skipif(not (AI_PY.exists() and ai_models.is_installed("m2m100-1.2b")), reason="M2M100 is not installed")
def test_real_m2m100_translation_round_trip(client, monkeypatch):
    monkeypatch.delenv("LIGXT_LYRICS_TEST", raising=False)
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(AI_PY))
    r = LY.run_translate(["The sun is setting over the sea."], "en", "hi", "good")
    assert translit.has_devanagari(r["lines"][0]) and r["device"] in ("cuda", "cpu")
    back = LY.run_translate(r["lines"], "hi", "en", "good")
    ai_bridge.stop()
    assert "sun" in back["lines"][0].lower() and "sea" in back["lines"][0].lower(), back


@pytest.mark.skipif(not (AI_PY.exists() and ai_models.is_installed("madlad400-3b")), reason="MADLAD-400 is not installed")
def test_real_madlad_translation_round_trip(client, monkeypatch):
    monkeypatch.delenv("LIGXT_LYRICS_TEST", raising=False)
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(AI_PY))
    r = LY.run_translate(["The sun is setting over the sea."], "en", "hi", "best", script="roman")
    assert not translit.has_devanagari(r["lines"][0]) and r["lines"][0].strip()  # romanized Hindi
    hi = LY.run_translate(["The sun is setting over the sea."], "en", "hi", "best")
    back = LY.run_translate(hi["lines"], None, "en", "best")
    ai_bridge.stop()
    assert "sun" in back["lines"][0].lower(), back
