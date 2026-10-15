# Ligxt Video Editor: Progress

Read this first at the start of every session. The detailed notes of every phase (what was built, how, measurements,
demos, the reel's history) are in [docs/HISTORY.md](docs/HISTORY.md).

## Current state

- **Version 2.0.0** (2026-10-15, tag `v2.0.0`). Phases 1-12 are done. **Phase 13a** (song lyrics captions: Auto, Paste lyrics,
  Tap to sync) and **Phase 13b** (2026-10-16, advanced lyrics + translation: vocal separation, synced-lyrics lookup, translation,
  Noto script fallback with shaped Hindi / Arabic) are done; no tag requested.
- What exists: timeline editor (trim/split/ripple, snapping, markers), canvas preview matching the export, transitions (xfade + whip),
  colour (presets, LUTs, effects, adjustment layers), motion (keyframes, Ken Burns, punch/shake), speed ramps, audio (ducking, SFX),
  text + auto-captions, export presets, style templates, Cutout (AI matting / SAM objects), AI slow motion (RIFE), motion tracking
  (follow, tracked blur, auto-reframe), Auto-edit from raw footage and song-lyrics captions (+ translation). See README.md for how to use it.
- Tests: `cd backend; ..\.venv\Scripts\python.exe -m pytest -q tests` (134; the 2 real-translation tests skip until M2M100 / MADLAD are installed) and `cd frontend; npm test` (136); headless UI tests `cd frontend; npm run e2e` (7). The AI tools: `ai-venv` (built by setup.ps1).
- Git: commit to `master` and push after each tested phase or fix.
- Stack: FastAPI backend (Python 3.11 `.venv`), React + Vite + TS frontend, ffmpeg 8.1.1 with NVENC, RTX 4050 (6 GB). AI models run
  in `ai-venv` through `ai_worker/` (BiRefNet, SAM 2.1, RIFE, stable-ts lyric alignment, python-audio-separator vocal separation,
  CTranslate2 translation); YuNet, VitTrack and faster-whisper (captions, lyrics Auto mode) run in the main environment.

## Usage rules (set by the user in Phase 10; apply to every phase)

- No Claude in Chrome or any interactive browser control for editing or testing videos (it uses too much of the usage limit).
- Make edits directly: project JSON files, or the backend API / CLI from scripts. Export with the backend's render code or a script, not by clicking in the UI.
- Test with automated tests only: pytest and frontend unit tests; a UI flow that needs checking gets a headless Playwright script (no screenshots unless a failure needs debugging).
- Check video results by extracting frames with ffmpeg and measuring them in scripts, not by viewing the app.
- Leave the visual and click-through checks to the user, with a clear test checklist.
- Use only media from `C:\Users\niles\ligxt-test-media` and the projects the user names; don't search other folders. If that folder is missing or empty, stop and say so. (Phases 10-13b: the folder doesn't exist; the user allowed the Travel Dump project's media and, for 13a/13b, the Beat Sync Reel's song `Video-78168.mp3`, plus `ligxt-test-media` if it ever exists.)
- Playwright (headless, a frontend dev dependency) is the tool for UI flows (`frontend/e2e`, `npm run e2e`).

## AI feature rules (apply to every AI phase)

Set by the user in Phase 9; every later AI feature follows them.

- **Hardware**: RTX 4050 Laptop, 6 GB VRAM. Every model must fit, with batch sizes or tiling tuned to stay under ~4.5 GB (Settings > GPU memory limit; the worker measures the first batch and sizes the rest, halves the batch on out-of-memory). Fall back to the CPU with a clear warning.
- **Environment**: don't break the existing app. Models that need PyTorch run in the AI worker (`ai_worker/`) inside the separate `ai-venv`, started by the backend as a subprocess (`ai_bridge.py`, JSON lines), so the editor keeps working if the AI setup fails. `setup.ps1` builds `ai-venv` (optional step, never fatal; keeps the PowerShell 5.1 fixes: `$LASTEXITCODE` checks, no `$ErrorActionPreference = "Stop"`).
- **Licenses**: check every model's and library's license before using it. Personal non-commercial use must be allowed. GPL or unclear: tell the user before using it and note it in the README. Prefer MIT / Apache / BSD.
- **Jobs**: every AI task is a background job (queue "ai", one GPU task at a time) with progress, ETA and Cancel, never freezing the UI. Results are cached per clip and settings, so repeat previews are instant.
- **Models**: downloaded on first use with progress and size shown, pinned to a revision and verified by SHA-256 (`ai_models.CATALOG`), deletable in Settings, which shows disk usage per model.
- **Preview vs export**: the preview may use a faster, lower-quality version, but the export uses full quality, and the two must line up in position and timing; measure it (pixel comparison of a preview frame and the exported frame, with a shift search).

## Key gotchas

- **FFmpeg 8.1:** `yuv420p -> gbrp` corrupts the last 8 columns at widths = 8 (mod 16), e.g. 1080. Always enter planar RGB via `grade.TO_GBRP`.
- **Frame grid:** every piece of the export is sampled on the absolute output frame grid (main-track segments, transition pieces since
  Phase 11). Off-grid starts drift cuts or repeat a frame (`test_cuts_do_not_drift`, `test_transition_between_frames_repeats_no_frame`).
- **Shared maths:** motion, grade, transitions, text, cutout, slow-mo and tracking each have a Python and a TS implementation checked by
  `shared/*_cases.json`; change both and regenerate the cases (`scripts/make_*_cases.py`).
- **Splitting a filter stream:** never `trim` one branch of a `split` (ffmpeg queues every earlier frame of the other branch); effect the
  whole stream with `enable=` and pad masks instead (see `track_render.py`).
- **Windows:** replacing `meta.json` / `project.json` while another request reads it fails ("Access is denied"): use `fsutil.replace_retry`.
  The AI worker polls stdin with `PeekNamedPipe` (a blocking read deadlocks DLL loading). ffmpeg `drawtext` needs an explicit `fontfile=`.
- **PowerShell 5.1:** `nativeExe | Select-Object -First 1` sets a non-zero `$LASTEXITCODE`; capture the output first.
- **ai-venv packages:** install `torchaudio==2.8.0` from the cu128 index before anything that needs it (stable-ts), or pip pulls a
  newer torchaudio that breaks torch 2.8.0+cu128. stable-ts `align()` with faster-whisper needs an explicit language (detect it first).
  `audio-separator` goes in with `--no-deps` (diffq is CC BY-NC); transformers stays 4.46.3, so the CTranslate2 converter is
  subclassed to pass `torch_dtype` instead of `dtype`; never copy a Hugging Face `config.json` into a converted CTranslate2 folder.
- **Pillow + FriBiDi:** Pillow decides whether raqm (shaping) works when `PIL.ImageFont` is first imported: `app/fribidi.py` loads
  `bin\fribidi\libfribidi-0.dll` first (imported at the top of `main.py` and `text.py`). Latin text keeps the BASIC layout engine
  so existing layouts don't move; shaped words use raqm.
- **Working in this repo:** long or backslash-heavy bash heredocs break; write patch scripts to a file and run them with
  `.venv\Scripts\python.exe`. Docs patches: assert every replacement (a silent `str.replace` once dropped an update).
- **Secrets:** the git remote URL holds a GitHub token: never print it; redact push output with `sed -E 's#https://[^@]*@#https://***@#g'`.
- **Browser tests:** Playwright's bundled Chromium has no H.264 decoder: tests that play video use `channel: "msedge"`. The preview clock
  uses wall time because Chrome throttles requestAnimationFrame in background windows.
- **Speed in the preview:** pitch-preserving `atempo` in the export, the browser's playbackRate in the preview; reversed clips are silent
  in the preview only.
- **Media library:** per project (`project.media`); files stay in `media\` until Home > Media cleanup removes the unused ones.

## Known issues

- Beat detection tested on synthetic click tracks and one real song (steady 119.6 BPM grid).
- Lyrics: vocals are separated with Mel-Band RoFormer only (user decision 2026-10-16: no Fast / Best tiers until Balanced is
  judged). Kim Vocal 2 (no license) and htdemucs_ft (research-only weights, needs CC BY-NC diffq) are not used; UVR DeEcho-DeReverb
  (no stated license, enabled at the user's request) is an off-by-default option. On sung Hindi (Video-78168.mp3) Whisper still
  keeps only ~8 reliable words in the first 7 s even on the separated vocals; Look up / Paste / Tap give full lyrics. Vocal presence
  is the stem's level against the mix (strong backing vocals or lead instruments can count). Lyrics can't be timed on reversed,
  frozen or speed-ramped music clips. Shaped-script lines animate per word (not per letter); right-to-left lines with embedded
  Latin words keep the words in base-direction order (no full bidi reordering inside a line).
- Bug bash (Chrome, 1366x768-class window), not fixed:
  - Built-in template cards start on the plain "1" placeholder card (the preview animates on hover).
  - Exports made before v1.0 can't be re-exported from Recent exports (no recorded settings; the button explains).
  - A read-only tab still shows the editing controls as enabled; edits are refused with a toast instead of being greyed out.
  - The export dialog needs a scroll at 768 px height (the Export button is at the bottom).
  - Cutout: on motion-blurred limbs the matte is soft; the Phase 10 motion-blur fix cuts the show-through by about two thirds, but very fast arms can still show text faintly. Subjects in very dark scenes can lose parts (feet) with the default model.
- Cutout: during a transition the subject matte goes through the same transition, so text behind the subject follows it; a replaced background stays during the transition.
- Cutout: overlay-track clips support background keep/remove, glow and shadow (no colour/photo/video background, no subject/background-only grade); text behind the subject uses the main track only.
- Slow motion: interpolation can warp very fast, thin or overlapping motion (the motion warning suggests a lower factor); the preview shows frame repeat until the low-res render finishes (seconds to a minute); 4K AI renders run at about 2-3 frames per second.
- Auto-edit: scores can't judge what a clip is about (it used the ambiguous tattooed-hands clip) or build a story / chapter change; clips that look unlike the rest are used sparingly (lock one in to force it); text stays minimal (title + closing line) and is only placed around faces, not behind mountains or on objects; it doesn't speed footage up for effect except the slam ramps.
- Motion tracking: hands that change shape (open/close, holding things) track loosely (a fixed frame + Re-track helps); following scale on a big textured region amplifies box-size noise (use position only there); the auto-reframe camera trails very fast moves for a few frames by design; faces that are tiny or very dark (pull-ups clip) aren't found by YuNet; the preview-vs-export UI test needs Microsoft Edge (H.264).
- Click-to-select uses the clicks of one or more frames but cannot be corrected mid-clip yet beyond adding clicks on other frames.
- The template wizard can't take files from the media library (only new files), so the same photo imported twice is two media items.
- Export: once (Phase 12) ffmpeg ended "successfully" without writing the output file; not reproducible. The export now says so
  clearly (export again) instead of failing on the rename.

## The reels

- **Beat Sync Reel** (12 photos + `Video-78168.mp3`): the user wants it enhanced after every phase to a pro, viral standard. Latest:
  "Beat Sync Reel v6.1" (+ Instagram export) and the "Beat Sync Reel v7 test" copy (text behind the subject). Photos only, so the
  slow-motion, tracking and auto-edit phases left it unchanged. The template "Golden Beat Trip" was saved from it.
  On 2026-10-15 the "Beat Sync Reel v6.1" project (`d15f73526286`) was found missing from `projects\` (not removed by Phase 13a
  code or tests); its exports and the template remain. Its song has Hindi vocals throughout (user, 13b): with separation, vocal
  presence 90 % (`exports\lyrics demo\report_13b.md`).
- **Travel Dump** (the user's project; original edit unchanged) and **Travel Dump v2** (rebuilt in Phase 11: slam ramps, tracked titles,
  text behind the peak and the neon portrait, auto-reframe; Phase 12: "fuel money" rides the wrist cuff). Exports `Travel Dump v2.mp4`
  + `Travel Dump v2 Instagram.mp4`; breakdown in `exports\travel dump v2 comparison\`.
- Demos: Cutout Demo, Slow-mo Demo, Tracking Demo, `Auto Edit - Energetic / Cinematic / Chill` (reports in `exports\<demo>\`).

## Next

- **Translation models not installed yet**: the M2M100 download finished (files verified, in `modelsi\m2m100-1.2b`) but Claude Code
  stopped the one-time conversion when the PC ran low on memory (16 GB RAM); MADLAD-400 never started. Re-run only when the user
  asks (Settings > Download, with other apps closed; MADLAD loads ~6 GB in fp16 to convert). The real M2M100 round trip worked
  before two late fixes (tokenizer class, keep CTranslate2's config.json); after them only the test-mode path is verified.
- Phase 13b done: the user will name a folder with a Hindi song and an English song for lyrics + translation tests (visual checks
  are theirs). Still open: was "Beat Sync Reel v6.1" deleted on purpose? Whether to keep de-reverb off (needs the real lyrics to
  judge correctness). Fast / Best separator tiers only after the user has seen Balanced's results.
- If the user sends the Phase 8 bug / friction list (still empty), fix those first.
- At the start of a phase: check whether `C:\Users\niles\ligxt-test-media` exists now (it never has).
- Open question for the user: add RobustVideoMatting (GPL-3.0) for comparison? (Not used without their OK.)
- Candidates from the known issues: greyed-out controls in read-only tabs, mid-clip correction for click-to-select, the template
  wizard taking files from the media library, Auto-edit text beyond a title (e.g. behind a detected landscape subject).
