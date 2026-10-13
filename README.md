# Ligxt Video Editor

A local video editor for Instagram Reels and other formats. It runs on your own machine: a browser UI plus a Python (FastAPI) backend that renders with ffmpeg. Nothing is uploaded anywhere.

## Requirements

- Windows 10/11 with PowerShell
- Python 3.11, available as `py -3.11` (your default Python can stay 3.13)
- ffmpeg + ffprobe on `PATH` (8.x recommended; NVENC is used automatically if your NVIDIA GPU supports it)
- Node.js 20+ (only used to build the UI)

## Setup (once)

```powershell
cd C:\Users\niles\ligxt-video-editor
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

This creates `.venv` (Python 3.11), installs the backend packages, installs and builds the frontend, and checks for NVENC. You can safely run it again at any time.

## Start

```powershell
.\start.ps1                # opens http://127.0.0.1:8765 in your browser
.\start.ps1 -Port 9000     # different port
.\start.ps1 -NoBrowser     # don't open a browser tab
```

Stop it with **Ctrl+C** in the PowerShell window.

## Folders

| Folder       | Contents |
|--------------|----------|
| `backend\`   | FastAPI app (`app\`) and tests (`tests\`) |
| `frontend\`  | React + Vite + TypeScript UI (built into `frontend\dist`) |
| `media\`     | Imported originals (`media\<id>\original.ext` + `meta.json`). Exports always use these files. |
| `cache\`     | Thumbnails, low-res proxies, waveforms (safe to delete; use **Rebuild proxy** to regenerate) |
| `projects\`  | `projects\<id>\project.json` and `autosave\` (last 10 autosaves, every 30 s) |
| `exports\`   | Rendered videos |
| `templates\` | Style templates (Phase 7) |
| `fonts\`     | Bundled fonts (OFL / Apache 2.0, see `fonts\README.md`) |
| `models\`    | Face detector; the captions speech model is downloaded into `models\whisper` |
| `sfx\`       | Bundled sound effects (whoosh, fast whoosh, hit, riser), synthesised, CC0 |

## Keyboard shortcuts

Press **?** in the editor to see this list.

| Key | Action |
|-----|--------|
| Space | Play / pause |
| ← / → | Step one frame |
| Shift + ← / → | Step one second |
| Home / End | Go to start / end |
| Delete | Delete selected clip or marker |
| Shift + Delete | Ripple delete (close the gap) |
| S | Split at playhead (selected clip, or every clip under the playhead) |
| Ctrl + D | Duplicate clip |
| M | Add marker at playhead |
| T | Add a transition at the nearest cut (uses the last type you picked) |
| \ | Before / after compare (drag the divider) |
| N | Snapping on/off (hold Alt while dragging to invert) |
| Ctrl + Z / Ctrl + Y | Undo / redo |
| Ctrl + S | Save |
| Ctrl + E | Export |
| + / − or Ctrl + scroll | Zoom the timeline |
| Esc | Clear selection |
| J / K / L | Shuttle back / stop / forward (press J or L again for 2×, 4×, 8×) |

Preview: the scrub bar under the picture scrubs, **Safe areas** shows where Instagram's Reels UI covers the video, and the fullscreen button sits next to it. Music: select a music clip and click **Detect beats**. Beats become snap points, and you can drop markers on every beat or bar.

Transitions: hover a cut on the main track and click **+**, or press **T**. Click the block to choose one of 21 types (fades, whip pans, slides, wipes, zoom, circle, pixelize, radial), set 0.2–2 s, or **Apply to all cuts**. Transitions are centred on the cut and use footage beyond each clip's in/out points, so your timing never moves. A ⚠ means a clip ran out of spare footage: the transition was shortened, or the edge frame is held.

Colour & effects: select a clip and open **Colour & effects** in Properties.
- **Presets** (Vintage 70s, Warm Film, Cool Teal, B&W, Faded, Bollywood Retro, Vivid, Golden Hour, Moody), each with an intensity slider, plus **My presets** (save a clip's filters with *Save these filters as my preset*).
- **LUT**: pick a bundled LUT or **Import…** a 3D `.cube`, with intensity.
- **Adjust**: exposure, brightness, contrast, highlights, shadows, whites, blacks, temperature, tint, saturation, vibrance, sharpen, blur. Double-click a slider to reset it.
- **Effects**: film grain (amount, size), vignette, light leaks, VHS look, glow/bloom, chromatic aberration.
- **Protect skin tones** (presets and LUTs, on by default) keeps faces from turning orange, green or grey. It is ignored for B&W.
- **Copy / Paste / All / Reset** move filters between clips. **Adjustment** (timeline toolbar) adds an adjustment layer: its filters grade everything below it for its duration, so one grade can cover many clips.
- Preview: **Before/After** shows a draggable split; **Scopes** shows a histogram, a waveform and how much of the picture is crushed or clipped.

Timeline mouse: drag a clip to move it (or to another track of the same kind), drag its edges to trim, drag a marker flag to move it, double-click a track name to rename it. The main video track is magnetic by default (no gaps; dragging reorders). Turn that off with the **Magnetic** button.

## Motion, speed and audio

- **Transform**: select a clip and drag it in the preview (move; corners scale; the round knob rotates, Shift = 15° steps), or use Properties > Transform (position, scale, rotation, opacity, flips, crop).
- **Keyframes**: the ◆ next to a property adds or removes a keyframe at the playhead; once a property has keyframes, every change at another time adds one. ◀ ▶ jump between keyframes. Pick the easing (linear, ease in, ease out, ease in-out, punch) for the move that starts at a keyframe. Keyframes show as diamonds on the clip in the timeline: click to jump, drag to retime.
- **Motion presets**: zoom in/out (Ken Burns), pan ←→↑↓, punch-in at the playhead, camera shake (amount, duration). Punches and shake layer on top of a zoom or pan.
- **Speed**: 0.25-4x (pitch preserved), speed ramps (presets + a curve you can drag; double-click adds/removes points), reverse, freeze frame.
- **Audio**: volume keyframes, fade in/out on any clip, Detach audio, auto-duck (the `duck` button on a music track lowers it under speech), `+ SFX` adds a sound effect whose peak lands on the playhead or a whoosh on every transition.
- **Selective colour** (Colour & effects): keep one colour (hue, range, softness, or pick it from the preview) and turn the rest black and white.

## Text and captions

- **Add text**: `+ Text` in the timeline toolbar (Bold hook title, Minimal lower-third, Retro film title, Meme top/bottom text). Select it to style it in Properties: font, size, colour, CAPS, alignment, letter spacing, line height, wrap width, outline, shadow/glow, background box (per line or block). **+ Save my text style** keeps a look for later.
- **On the canvas**: click a text to select it; click it again (or double-click) to type on the canvas (Esc or a click outside finishes); drag to move, corners to scale, the knob to rotate.
- **Beat-synced words**: *♪ Sync words to the beat* puts one word on each beat (or half / every second beat) of the music under the text; with the *Reveal + pop* highlight each word appears exactly on its beat.
- **Animations**: In and Out separately: fade, slide (from below/above/left/right), pop/scale, typewriter, word by word, each with a duration and easing. Position, scale, rotation and opacity take ◆ keyframes like clips.
- **Auto-captions** (`CC Captions`): speech on the video and audio tracks is transcribed locally with faster-whisper (GPU when available, free). The first run downloads the model (about 460 MB for "small") into `models\whisper`. Captions land on a "Captions" track as short segments with word timings, placed above the Reels caption area. Highlight styles: karaoke pop, karaoke colour, reveal as spoken. Editing a caption's text keeps the timings; splitting a caption splits its words. If there is no speech, you are told so and nothing is added.
- **Safe areas**: the preview warns when text or a detected face sits under the Reels header, the caption area or the buttons (badge next to *Safe areas*; click a warning to jump there). Faces are found on import (OpenCV YuNet, `modelsace_detection_yunet_2023mar.onnx`, MIT).
- **Extend canvas** (photos, Properties > Frame): fills around the photo with a blurred, colour-matched extension, so a photo can be moved or zoomed out without showing its edges.

## Export settings

| Preset | Size | Video | Audio | Grain |
|--------|------|-------|-------|-------|
| Instagram (optimized) | 1080×1920 | H.264 High 4.2, 12 Mbps (max 16), 2 s keyframes | AAC 256k | reduced |
| Reels (max quality) | 1080×1920 | H.264 High, CRF 18 | AAC 192k | full |
| YouTube Shorts | 1080×1920 | H.264 High, CRF 18, 0.5 s keyframes | AAC 256k | full |
| TikTok | 1080×1920 | H.264 High 4.2, 10 Mbps (max 14) | AAC 192k | reduced |
| Square / Portrait 4:5 | 1080×1080 / 1080×1350 | H.264 High, CRF 18 | AAC 192k | full |
| YouTube 16:9 | 1920×1080 | H.264 High, CRF 18 | AAC 256k | full |
| Custom | any | constant quality (CRF) or bitrate | | either |

- Why the Instagram preset: Instagram re-encodes every upload to a much lower bitrate. A clean, moderately sized file with less film grain survives that better than a huge, noisy master ("reduce grain" exports grain at 35 %).
- **Draft**: h264_nvenc (GPU), fast. Falls back to fast CPU encoding if NVENC is unavailable.
- The video is composed on the project canvas and scaled to the preset; a different aspect ratio is letterboxed (change the project's aspect ratio to reframe instead).
- **File-size estimate** before export: exact from the bitrate for bitrate presets, otherwise from three 1-second test renders.
- **Cover image**: in the export dialog, *Save cover* writes a JPG of the frame at the playhead, optionally with a title (one of your text clips or custom text), to `exports\`.
- Every export runs an audio/video sync check and reports the result.

## Development

```powershell
# backend tests (use a temporary data folder, never your real projects)
cd backend; ..\.venv\Scripts\python.exe -m pytest -q tests

# frontend unit tests (timeline editing logic)
cd frontend; npm test

# UI with hot reload: run the backend, then the Vite dev server on :5173
.\start.ps1 -NoBrowser
cd frontend; npm run dev
```

The project format is documented in `backend\app\models.py`. The ffmpeg graph builder is in `backend\app\render.py`. `POST /api/projects/<id>/export/preview-graph` returns the generated ffmpeg command without rendering it, which helps with debugging.

## Bundled LUTs and licenses

`luts\` holds four example LUTs (Teal & Orange, Bleach Bypass, Matte Fade, Sunset Warm). They were generated for this project by `scripts\make_luts.py` from simple colour formulas, contain no third-party material, and are released under **CC0 1.0** (public domain, see `luts\LICENSE.md`). LUTs you import are stored in `user_luts` and keep their own licenses.

`fonts\` holds the bundled fonts: Anton, Bebas Neue, Archivo Black, Poppins, DM Serif Display, Pacifico (SIL Open Font License 1.1) and Special Elite (Apache 2.0), from the Google Fonts repository. They may be used in any video, also commercially; licenses and sources are in `fonts\README.md` and next to each font.

`modelsace_detection_yunet_2023mar.onnx` is OpenCV's YuNet face detector (MIT, `models\YUNET-LICENSE.txt`). The speech model for captions is downloaded on first use from Hugging Face (Systran/faster-whisper, MIT).

`sfx\` holds sound effects synthesised from noise and sine waves by `scripts\make_sfx.py`; no recordings or samples are used, and they are released under **CC0 1.0** (see `sfx\LICENSE.md`).

## Troubleshooting

- **"Cannot reach the Ligxt backend"**: the PowerShell window running `start.ps1` was closed. Start it again.
- **A clip shows "Proxy failed"**: select the file and click **Rebuild proxy**. Export still uses the original either way.
- **Port already in use**: `.\start.ps1 -Port 9000`.
- **HDR iPhone footage looks washed out**: it is tone-mapped to SDR automatically in proxies and exports. If not, check that ffmpeg has `zscale` (the gyan.dev full build does).
