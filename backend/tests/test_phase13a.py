"""Phase 13a: song lyrics captions from a music clip. No vocal separation (Demucs' weights are research-only, see
PROGRESS.md), so everything works on the song itself. The worker's `align_lyrics` op runs through the real subprocess
with its numpy "test-align" mode; mapping source -> timeline (in point, speed); the hallucination guard (evenly
repeated words); instrumental detection; interpolation of unaligned words; the API; and, when the pinned
large-v3-turbo model is installed and Windows' speech synthesizer is available, real Whisper + stable-ts on a
synthesized voice over music."""
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

os.environ["LIGXT_AI_PYTHON"] = sys.executable  # the worker runs with this Python (test-align needs numpy only)

from app import ai_bridge, ai_models, config  # noqa: E402
from app import lyrics as LY  # noqa: E402
from app.models import Clip, Project  # noqa: E402
from test_phase1 import upload, wait_job  # noqa: E402

SR = 44100


def tone_song(path: Path, seconds: float, voiced: list[tuple[float, float]] | None = None):
    """Instrumental: a chord + a beat. `voiced` spans get a loud, formant-like 'voice' (for the test aligner)."""
    t = np.arange(int(seconds * SR)) / SR
    x = 0.12 * (np.sin(2 * np.pi * 110 * t) + np.sin(2 * np.pi * 165 * t) + 0.5 * np.sin(2 * np.pi * 220 * t))
    x += 0.25 * np.exp(-((t % 0.5) * 30)) * np.sin(2 * np.pi * 60 * t)
    for a, b in voiced or []:
        m = (t >= a) & (t < b)
        x[m] += 0.5 * np.sin(2 * np.pi * 300 * t[m]) * (0.6 + 0.4 * np.sin(2 * np.pi * 5 * t[m]))
    y = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    import wave
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes(y.tobytes())


@pytest.fixture(scope="module")
def songs(tmp_path_factory, client):
    d = tmp_path_factory.mktemp("p13a")
    inst = d / "instrumental.wav"
    tone_song(inst, 12)
    voc = d / "voiced.wav"
    tone_song(voc, 12, [(2.0, 3.5), (5.0, 6.5), (8.0, 9.5)])
    out = {"inst": upload(client, inst), "voiced": upload(client, voc)}
    yield out
    ai_bridge.stop()


def project_with(client, mid, *, start=1.0, inp=1.5, outp=10.5, speed=1.0, **kw):
    p = client.post("/api/projects", json={"name": "Lyrics test", "aspect": "9:16"}).json()
    mu = next(t for t in p["tracks"] if t["type"] == "music")
    mu["clips"] = [{"id": "song", "mediaId": mid, "start": start, "inPoint": inp, "outPoint": outp, "speed": speed, **kw}]
    return p


def test_source_to_timeline_and_clips_that_cant_be_timed(client, songs):
    p = Project.model_validate(project_with(client, songs["voiced"]["id"], start=2.0, inp=3.0, outp=9.0, speed=1.5))
    c, m, note = LY.music_clip(p, None, 0.0)
    assert note is None and c.id == "song"
    assert LY.to_timeline(c, 3.0) == pytest.approx(2.0)
    assert LY.to_timeline(c, 6.0) == pytest.approx(2.0 + 3.0 / 1.5)
    for extra in ({"reverse": True}, {"speedRamp": {"points": [{"u": 0, "speed": 1}, {"u": 1, "speed": 2}]}}, {"freezeFrame": 4.0}):
        q = Project.model_validate(project_with(client, songs["voiced"]["id"], **extra))
        _, _, note = LY.music_clip(q, None, 0.0)
        assert note and "can't be timed" in note
    empty = client.post("/api/projects", json={"name": "x", "aspect": "9:16"}).json()
    assert LY.music_clip(Project.model_validate(empty), None, 0)[2] == "There is no song on a music track."


def test_hallucination_guard_drops_evenly_repeated_words():
    even = [{"text": "you", "start": 1 + 0.5 * k, "end": 1.3 + 0.5 * k} for k in range(6)]
    real = [{"text": w, "start": 5 + 0.37 * k * (1 + 0.3 * (k % 2)), "end": 5.3 + 0.37 * k * (1 + 0.3 * (k % 2))}
            for k, w in enumerate("we were young and free".split())]
    kept, dropped = LY.reject_repeats(even + real)
    assert dropped == 6 and [w["text"] for w in kept] == "we were young and free".split()
    pair = [{"text": "la" if k % 2 == 0 else "da", "start": 0.4 * k, "end": 0.4 * k + 0.3} for k in range(8)]  # "la da la da ..."
    assert LY.reject_repeats(pair)[1] == 8
    irregular = [{"text": "no", "start": t, "end": t + 0.2} for t in (1.0, 1.3, 2.4, 2.6)]  # sung "no, no... no no": not even
    assert LY.reject_repeats(irregular)[1] == 0


def test_unaligned_words_are_interpolated_and_flagged():
    lines = [[{"text": "a", "start": 1.0, "end": 1.5}, {"text": "b", "start": 0, "end": 0, "flag": True}, {"text": "c", "start": 0, "end": 0, "flag": True}],
             [{"text": "d", "start": 4.0, "end": 4.5}]]
    assert LY.interpolate_flagged(lines, 0.0, 10.0) == 2
    b, c = lines[0][1], lines[0][2]
    assert b["start"] == pytest.approx(1.5) and c["start"] == pytest.approx(2.75) and c["end"] == pytest.approx(4.0)


def test_paste_mode_through_the_worker_keeps_lines_and_maps_to_the_timeline(client, songs, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")
    p = project_with(client, songs["voiced"]["id"], start=1.0, inp=1.5, outp=10.5)
    text = "first line here\nsecond line\n\nthird and last line"
    r = client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "align", "text": text})
    assert r.status_code == 200, r.text
    j = wait_job(client, r.json()["jobId"], timeout=120)
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    res = j["result"]
    lines = res["lines"]
    assert [[w["text"] for w in ln] for ln in lines] == [["first", "line", "here"], ["second", "line"], ["third", "and", "last", "line"]]
    flat = [w for ln in lines for w in ln]
    assert all(1.0 <= w["start"] < w["end"] <= 10.0 for w in flat), flat  # inside the clip on the timeline (start 1, 9 s long)
    assert all(a["start"] <= b["start"] for a, b in zip(flat, flat[1:]))
    # the test aligner put the words on the loud 'voice' spans: source 2.0-9.5 -> timeline 1.5-9.0
    assert flat[0]["start"] == pytest.approx(1.5, abs=0.15) and flat[-1]["end"] == pytest.approx(9.0, abs=0.2)
    assert res["voice"]["perSecond"] and "aligned" in res["message"]


def test_auto_mode_says_instrumental(client, songs, monkeypatch):
    monkeypatch.setenv("LIGXT_LYRICS_TEST", "1")  # voice activity only (no speech model in tests)
    p = project_with(client, songs["inst"]["id"])
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "auto"}).json()["jobId"])
    assert j["status"] == "done", j.get("error")
    assert j["result"]["instrumental"] and j["result"]["words"] == []
    assert "No vocals in this part of the song (instrumental)" in j["result"]["message"]
    assert len(j["result"]["voice"]["perSecond"]) == 9


# ---------------------------------------------------------------- real models (when installed)
def _sapi(text: str, out: Path) -> bool:
    ps = (f"Add-Type -AssemblyName System.Speech; $s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
          f"$s.Rate = -1; $s.SetOutputToWaveFile('{out}'); $s.Speak('{text}'); $s.Dispose()")
    r = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    return r.returncode == 0 and out.exists() and out.stat().st_size > 10000


@pytest.mark.skipif(not ai_models.is_installed(ai_models.LYRICS_MODEL), reason="large-v3-turbo is not installed")
def test_real_whisper_and_stable_ts_on_a_voice_over_music(client, tmp_path, monkeypatch):
    monkeypatch.delenv("LIGXT_LYRICS_TEST", raising=False)
    sentence = "the quick brown fox jumps over the lazy dog"
    voice = tmp_path / "voice.wav"
    if not _sapi(sentence, voice):
        pytest.skip("Windows speech synthesis is not available")
    bed = tmp_path / "bed.wav"
    tone_song(bed, 9)
    song = tmp_path / "song.wav"
    subprocess.run([config.FFMPEG, "-v", "error", "-y", "-i", str(bed), "-i", str(voice), "-filter_complex",
                    "[0]volume=0.35[a];[1]volume=1.6,adelay=2000:all=1[b];[a][b]amix=inputs=2:duration=first:normalize=0", "-ar", "44100", str(song)], check=True)
    m = upload(client, song)
    p = project_with(client, m["id"], start=0.0, inp=0.0, outp=9.0)
    # Auto separates the vocals first (Phase 13b) and paste mode aligns with stable-ts: both run in the real AI environment
    ai_py = Path(config.ROOT) / "ai-venv" / "Scripts" / "python.exe"
    if not ai_py.exists():
        pytest.skip("ai-venv is not installed")
    ai_bridge.stop()
    monkeypatch.setenv("LIGXT_AI_PYTHON", str(ai_py))
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "auto", "model": "large-v3-turbo"}).json()["jobId"],
                 timeout=600)
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    got = [w["text"].lower().strip(".,!?") for w in j["result"]["words"]]
    assert sum(1 for w in sentence.split() if w in got) >= 6, got
    assert not j["result"]["instrumental"]
    assert all(1.5 < w["start"] < 9.0 for w in j["result"]["words"]), j["result"]["words"]
    j = wait_job(client, client.post(f"/api/projects/{p['id']}/lyrics", json={"project": p, "mode": "align", "text": "the quick brown fox\njumps over the lazy dog"}).json()["jobId"],
                 timeout=600)
    ai_bridge.stop()
    assert j["status"] == "done", (j.get("error"), j.get("log"))
    flat = [w for ln in j["result"]["lines"] for w in ln]
    assert [w["text"] for w in flat] == sentence.split()
    assert j["result"]["flagged"] <= 2
    assert all(a["start"] <= b["start"] for a, b in zip(flat, flat[1:]))
    assert 1.5 < flat[0]["start"] < 4.0 and flat[-1]["end"] < 9.0, j["result"]


def test_a_word_stretched_over_the_intro_starts_with_the_voice():
    spans = [[2.1, 4.0], [5.0, 6.0]]
    assert LY.trim_to_voice(0.5, 2.4, spans) == pytest.approx(2.1)  # stretched back over the intro
    assert LY.trim_to_voice(2.3, 2.6, spans) == 2.3  # already inside the voice
    assert LY.trim_to_voice(4.2, 4.8, spans) == 4.2  # no voice heard under it at all: left alone
    assert LY.trim_to_voice(1.0, 2.12, spans) == pytest.approx(2.07)  # keeps at least 50 ms
