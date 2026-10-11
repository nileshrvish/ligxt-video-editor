# Ligxt Video Editor: Progress

Read this first at the start of every session.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Backend, project format, media import + proxies, single-clip export | **Done** (2026-10-11) |
| 2a | Timeline editing: move/reorder, trim, split, ripple delete, duplicate, snapping, markers, add/remove tracks, overlap prevention, one undo per drag | **Done** (2026-10-11) |
| 2b | Canvas preview player (J/K/L, scrubbing, safe areas), audible music/audio in preview, overlay compositing (preview + export), beat detection | **Done** (2026-10-11) |
| 3 | Transitions centred on the cut using handles (xfade types + custom whip pan) | **Done** (2026-10-11) |
| 4 | Filters & colour (adjustments, presets, effects, LUTs, skin protection, adjustment layers, compare, scopes) | **Done** (2026-10-11) |
| 5 | Clip editing (crop, keyframes, speed, reverse, freeze, aspect) + audio (fades, detach, ducking) | Next |
| 6 | Text, auto-captions (faster-whisper), export presets polish | |
| 7 | Style templates | |

Tests: `cd backend; ..\.venv\Scripts\python.exe -m pytest -q tests` (39) and `cd frontend; npm test` (69).
Git: commit to `master` and push after each tested phase or fix.

## Phase 1: what exists

- `setup.ps1` / `start.ps1` (tested in PowerShell 5.1). Backend on `127.0.0.1:8765`; it serves `frontend/dist`.
- Backend (`backend/app`):
  - `config.py`: paths. The `LIGXT_DATA_DIR` env var relocates media/cache/projects/exports (the tests use it).
  - `jobs.py`: thread-pool job queue ("media" with 2 workers, "render" with 1), progress, ETA, cancel. Live updates via SSE at `/api/events`.
  - `media.py`: raw-body upload `POST /api/media/import?filename=`, ffprobe metadata (HDR detection, rotation), thumbnail, waveform peaks (100/s, uint8), proxies (≤960 px, NVENC with libx264 fallback, GOP 15; image → preview.jpg; audio proxy only for codecs browsers can't play).
  - `models.py`: project format v1 (pydantic, extra fields preserved, everything defaulted). `project.media` is a list of media ids; metadata lives in `media/<id>/meta.json`.
  - `projects.py`: CRUD, autosave (keeps 10), restore, duplicate.
  - `render.py`: project → filter graph (see the Phase 3 notes for the main-track piece model). The graph is passed via `-/filter_complex <file>`.
- Frontend (`frontend/src`): zustand stores (`store/editor.ts` has commit/undo/redo/gesture grouping; `store/app.ts` has toasts, jobs, SSE), Home, Editor with resizable panels, export dialog with progress/ETA/cancel/result, shortcut overlay, autosave every 30 s, unsaved-changes guard.

## Phase 2a: what exists

- `frontend/src/timeline/ops.ts` holds every timeline edit as a pure function on a draft Project (move, trim start/end, split, splitAll, delete/ripple, duplicate, settle, add/remove track, markers, snap, transitions). Unit tests in `ops.test.ts`.
- Main video track is **magnetic** by default (`track.magnetic`, toggle in the toolbar): clips stay packed, dragging reorders, trims and deletes ripple. Other tracks: free placement, never overlapping (nearest free slot). Clips move between compatible tracks (video and overlay; audio and music). Locked tracks reject edits.
- Drags use `beginGesture` → `fromBase(fn)` (recompute from the drag's start snapshot) → `endGesture`, so each drag is one undo step. The drag helper listens on `window` because a clip moved to another track is re-mounted (pointer capture would lose `pointerup`).
- Snapping (N, Alt inverts while dragging): clip edges on all tracks, playhead, markers, beats, 0; 8 px threshold, with a yellow snap line.
- Markers: M adds one; drag the flag on the ruler; edit label/time/color in Properties.
- Selection: `{kind: "clip", clipId}` (the track is looked up), `{kind: "marker", id}`, `{kind: "transition", id}`.
- Timeline fits 5 tracks at 1366×768 (38 px rows, timeline 40% by default).

## Phase 2b: what exists

- `frontend/src/player/engine.ts` (`PreviewEngine`): one master clock (wall time × rate). Each clip near the playhead (1.5 s preroll) gets its own detached `<video>`/`<audio>` on the proxy, slaved to the clock (seeks when drift exceeds 0.25 s). Frames are drawn to a 2D canvas: background → main track → overlay tracks in track order. A frame is only drawn once every layer is ready (the old frame stays while seeking).
- Audio: each element goes through WebAudio (gain per clip → master → analyser). Volume above 100%, fades, and track/clip mute work. Audio is muted in reverse or above 2× shuttle. Stopping silences immediately (from the store subscription).
- `window.__ligxtPreview.debugState()` / `.level()` expose element state and output RMS for browser tests.
- J/K/L shuttle (store `rate`: ±1/2/4/8), scrub bar, Reels safe-area guides (9:16: header, caption area, button column; 90% box otherwise), fullscreen.
- Overlay tracks in export: `overlay` filter per clip (`setpts+offset`, `enable=between`), fit/fill + transform scale/x/y/rotation/flip/opacity; later tracks on top; hidden tracks skipped; overlay video audio is mixed in. Overlay Properties: scale %, X/Y, opacity.
- Beat detection: `backend/app/beats.py` (numpy spectral flux, tempo via autocorrelation with a 120 BPM prior, Ellis DP tracker, +70 ms window-lag correction). `POST /api/media/{id}/beats` is a job and stores `bpm` + `beats` in the media meta. UI: beat ticks on music clips, beats as snap targets, "markers on every beat / 2nd / bar".
- Browser-verified: preview overlay position matches export, shuttle rates exact, audio levels follow mutes, no drift over 3 s.

## Phase 3: what exists

- **Rule (your decision, option B):** a transition is centred on the cut and uses handles (footage beyond the in/out points), so the timeline length and every cut position stay unchanged. If footage runs short it is shortened to what's available (min 0.1 s); with none, the edge frame is held. Photos have unlimited handles. h is also capped at half the shorter clip so two transitions on one clip never overlap.
- The rules live in `backend/app/transitions.py` and `frontend/src/timeline/transitions.ts`, both tested against `shared/transition_cases.json` (9 cases, run by pytest and vitest).
- Export (`render.py`): the main track is a sequence of pieces (solid clip parts, gaps, transition pieces) concatenated on the absolute frame grid. `clip_source()` renders any clip from any timeline time (handles, `tpad` clone for freeze, `adelay`/`apad` for audio). Transition pieces are rendered whole and trimmed to the export range. xfade runs in **gbrp** (on yuv420p FFmpeg's zoomin indexes the half-size chroma planes with luma coordinates). Audio crossfades with linear `afade` out/in.
- **Whip pan** (custom, ←→↑↓): cubic in-out eased slide; motion blur = 960 fps overlay + `tmix=frames=32` + `fps` (a 1/30 s shutter; 8 sub-frames at 240 fps showed visible stepping on fast whips). Measured: output frame k averages sub-frames n_last-31..n_last with n_last = ceil((k+0.5)·960/fps)-1 (fps keeps the last sub-frame that rounds to k; frame 0 differs slightly, invisible because the ease starts slow). The preview mirrors this.
- Preview: `player/transitions.ts`, a line-by-line GLSL port of FFmpeg 8 `vf_xfade.c` (progress P runs 1→0, same mix/smoothstep/integer truncation) plus a 2D-canvas whip with the same sub-frame averaging. The engine extends main-track clips across their handles, renders both sides as full frames and composites them. For zoomin, A is rendered at full project resolution and the sample models yuv420 chroma sharing (zoomin magnifies a single pixel). Dissolve uses a sine-free hash (GPU `sin` banding).
- **Parity measured in the browser** against exported frames (21 types × 3 points, 9×16 area-averaged blocks, MAE out of 255): static baseline 1.9–3.5, every transition ≤ 6.8 (dissolve, because of its random dither).
- UI: transition blocks centred on cuts (⚠ and a tooltip when shortened or frozen), "+" on hover at bare cuts, T adds one at the nearest cut (last-used type), Delete removes. Properties: 21 types in 5 groups, duration 0.2–2 s, actual length, spare footage per side, warning text, Preview, Apply to all cuts, Remove.
- Transitions exist on the main track only.

## Phase 4: what exists

- **One colour pipeline, two implementations**: `backend/app/grade.py` and `frontend/src/grade/math.ts` (adjust maths, presets from `shared/presets.json`, .cube sampling, skin protection, VHS colour, effect resolution). `shared/grade_cases.json` (24 cases, generated by the Python side) must be matched by the TS side to 1e-4. All point operations are baked into one 33^3 LUT per clip: export = `lut3d interp=trilinear` (file in the per-export work dir, ffmpeg runs with `cwd` there), preview = WebGL2 3D texture (RGB16F, linear).
- Fixed pipeline order (both sides): blur → sharpen → LUT + chromatic aberration → VHS chroma bleed → glow → light leak → scanlines → vignette → grain. Spatial sizes are reference px at a 1080 short side.
- Grain and light leaks use the **same generated textures** in export and preview (`/api/fx/grain.png`, `/api/fx/leak.png`, cached in `cache/_fx`): grain atlas of 8 frames indexed by absolute frame number; leak slides by `sin(t * speed)` on timeline time. Vignette and scanlines are formulas; masks are generated for ffmpeg (`blend multiply`), the preview box-filters scanlines like the area downscale.
- Skin protection (presets and LUTs, default on): keeps hue/saturation of skin-like colours while following the grade's brightness; **never applied to monochrome presets**.
- Adjustment layers: overlay-track clips with `adjustment: true` and no media. Export: split → grade_chain → `blend normal` with `enable=between(t,...)` (strength = clip opacity). Preview: grade the canvas composed so far.
- UI: Colour & effects panel (presets + intensity + skin toggle, my presets via `/api/presets`, LUT picker + .cube import via `/api/luts`, 13 adjust sliders, 6 effects), copy/paste/apply all/reset, Adjustment button in the timeline toolbar, Before/After split (`\\` key, draggable divider), Scopes (histogram, luma waveform, crushed/clipped %).
- Bundled LUTs: `luts/` (4 CC0 LUTs from `scripts/make_luts.py`); imported LUTs go to `user_luts/`, user presets to `presets/` (both gitignored).
- **Parity measured in the browser** (24 variants: 9 presets, 6 effects, adjust, blur, sharpen, 2 LUTs with/without skin protection, stack, adjustment layer, graded overlay; 9x16 area blocks): all ≤ 5.7/255, baseline 3.0. The harness waits on `__ligxtPreview.lastFrameT` (the time of the last fully composed frame), not on fixed delays.

## Decisions

- Project length = end of the main video track (music running longer is cut). `util.projectDuration` and `render.build_plan` agree.
- Overlaps are prevented in the UI (ops). The renderer still tolerates them (the earlier clip wins) for hand-edited JSON.
- Output is forced to TV range (`scale=out_range=tv`, `-color_range tv`) so JPEG inputs don't produce yuvj420p.
- **FFmpeg 8.1 bug:** `yuv420p → gbrp` corrupts the last 8 columns at widths ≡ 8 (mod 16), e.g. 1080. Always enter planar RGB via `grade.TO_GBRP` (`format=rgb24,format=gbrp`). It affected Phase 3 xfade transitions in 1080-wide exports (edge glitch during fades) until 2026-10-11. Regression test `test_no_edge_corruption_at_1080_wide`.
- Export file names are unique even within the same second (reserved names, `-2`, `-3` suffixes); two exports used to overwrite each other.
- The preview composes each frame off-screen and only shows complete frames (no black frames while grading assets load).
- Main-track segments are snapped to the absolute output frame grid (`trim=end_frame`), so cuts land within half a frame (fixed a drift of up to 3.6 frames after 20 cuts; regression test `test_cuts_do_not_drift`).
- Track stacking: main video is the bottom layer; overlay tracks are drawn in listed order (lower rows on top).
- Main-track clip transform is not applied yet (preview and export ignore it); overlays use it. Phase 5 adds it for the main track (needed for Ken Burns on photos).

## Next (Phase 5: clip editing + audio)

- Crop, rotate, flip, scale, position for main-track clips too (preview and export currently ignore main-track transform); keyframes (position, scale, rotation, opacity) for Ken Burns / punch-ins; speed 0.25–4x with pitch-preserving audio; freeze frame; reverse; canvas switch with fit/fill/blur.
- Audio: per-clip fades, detach audio, music fades, auto-ducking, sync check.
- Keyframed opacity would let adjustment layers fade in (e.g. ease into the golden ending).

## The user's reel ("Beat Sync Reel", project in projects\)

- 12 photos + `Video-78168.mp3` (119.6 BPM), 24 beat-synced cuts, markers Drop / Peak / Outro. The imported video is deliberately not used.
- The user wants it enhanced after every phase to a professional, viral-reel standard.
- Phase 3 pass (2026-10-11): whip pans centred on location changes (fastest point of each whip lands on the beat), a white flash into the B&W breakdown, a whip up onto the drop, hard stutter cuts inside each location and through the 1-beat peak, and a 0.5 s fade into the golden-hour outro. 14 hard cuts within 0.016 s of a beat. Export `exports\Beat Sync Reel_20261011-200643.mp4`.
- Phase 4 pass (2026-10-11): snapshot kept as project **"Beat Sync Reel v3"** + `exports\Beat Sync Reel v3.mp4` (re-rendered with the edge fix). Colour match (per-clip adjust: studio 1 +0.76 EV / studio 2 −0.55 EV to equal mean luma 0.37 and matched WB; orange house exposure/WB; mural tint at half strength; church door already consistent). Tracks "Grade: overall" (Warm Film 55 %, contrast −10, highlights −8, grain 0.12, vignette 0.22), "Grade: sections" (B&W 5.97–7.96 with contrast +28; drop+peak 7.96–19.97 vibrance/contrast/glow; golden ending 19.97–end: Golden Hour 100 % + warmth + light leak 0.38 + glow), "Grade: flashes" (4-frame exposure+bloom flash at the drop, 3-frame at the peak). Export `exports\Beat Sync Reel v4.mp4`; comparisons in `exports\v4 comparison\`.
- Ideas for later phases: P5 Ken Burns on every photo, punch-in on the drop, zoom pulses on beats, fade the golden layer in with keyframed opacity; P6 hook text in the first second inside safe areas. Offer the selective-colour B&W variant (orange glasses kept) if the user prefers it.

## Known issues / notes

- Gotcha in PowerShell 5.1: `nativeExe | Select-Object -First 1` sets a non-zero `$LASTEXITCODE`. Capture the output to a variable first.
- Gotcha: ffmpeg `drawtext` on Windows needs an explicit `fontfile=` (no fontconfig). Relevant for Phase 6.
- Speed: the export uses pitch-preserving `atempo`; the preview uses the browser's playbackRate. The speed UI arrives in Phase 5.
- Chrome throttles requestAnimationFrame when the window isn't focused; the preview clock uses wall time, so it catches up.
- Beat detection tested on synthetic click tracks and one real song (steady 119.6 BPM grid).
- Docs patches: use scripts that assert each replacement (a silent `str.replace` once dropped the 2b PROGRESS update).
- The media library is per project (`project.media`); files stay in `media\` even when removed from a project (no purge UI yet).
