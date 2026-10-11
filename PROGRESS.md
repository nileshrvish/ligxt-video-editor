# Ligxt Video Editor: Progress

Read this first at the start of every session.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Backend, project format, media import + proxies, single-clip export | **Done** (2026-10-11) |
| 2a | Timeline editing: move/reorder, trim, split, ripple delete, duplicate, snapping, markers, add/remove tracks, overlap prevention, one undo per drag | **Done** (2026-10-11) |
| 2b | Canvas preview player (J/K/L, scrubbing, safe areas), audible music/audio in preview, overlay compositing (preview + export), beat detection | **Done** (2026-10-11) |
| 3 | Transitions (xfade + whip pan) | Next |
| 4 | Filters & color (ffmpeg + WebGL preview), LUTs | |
| 5 | Clip editing (crop, keyframes, speed, reverse, freeze, aspect) + audio (fades, detach, ducking) | |
| 6 | Text, auto-captions (faster-whisper), export presets polish | |
| 7 | Style templates | |

## Phase 1: what exists

- `setup.ps1` / `start.ps1` (tested in PowerShell 5.1). Backend on `127.0.0.1:8765`; it serves `frontend/dist`.
- Backend (`backend/app`):
  - `config.py`: paths. The `LIGXT_DATA_DIR` env var relocates media/cache/projects/exports (the tests use it).
  - `jobs.py`: thread-pool job queue ("media" with 2 workers, "render" with 1), progress, ETA, cancel. Live updates via SSE at `/api/events`.
  - `media.py`: raw-body upload `POST /api/media/import?filename=`, ffprobe metadata (HDR detection, rotation), thumbnail, waveform peaks (100/s, uint8), proxies (≤960 px, NVENC with libx264 fallback, GOP 15; image → preview.jpg; audio proxy only for codecs browsers can't play).
  - `models.py`: project format v1 (pydantic, extra fields preserved, everything defaulted). `project.media` is a list of media ids; metadata lives in `media/<id>/meta.json`.
  - `projects.py`: CRUD, autosave (keeps 10), restore, duplicate.
  - `render.py`: project → filter graph. Main track with gaps, fit/fill/blur, speed (video only), per-clip volume/mute, audio + music tracks (adelay, fades, amix + limiter), range export, HDR tone-map, final/draft encoders, audio sync check. The filter graph is passed via `-/filter_complex <file>`.
- Frontend (`frontend/src`): zustand stores (`store/editor.ts` has commit/undo/redo/gesture grouping; `store/app.ts` has toasts, jobs, SSE), Home (new project with aspect choice, recent projects, duplicate/delete), Editor with resizable panels (media library, program/source preview, properties, timeline), export dialog with progress/ETA/cancel/result, shortcut overlay, autosave every 30 s, unsaved-changes guard.
- Tests: `backend/tests/test_phase1.py` (9 tests, about 14 s). Browser-tested at 1366×768: import via drop, add clips, playback across clips, Delete + Ctrl+Z, export (17 s reel rendered in 4.5 s, sync OK, frames verified).

## Phase 2a: what exists

- `frontend/src/timeline/ops.ts` holds every timeline edit as a pure function on a draft Project (move, trim start/end, split, splitAll, delete/ripple, duplicate, settle, add/remove track, markers, snap). `ops.test.ts` has 22 vitest tests (`cd frontend; npm test`).
- Main video track is **magnetic** by default (`track.magnetic`, toggle in the toolbar): clips stay packed, dragging reorders, trims and deletes ripple. Other tracks: free placement, never overlapping (nearest free slot). Clips move between compatible tracks (video and overlay; audio and music). Locked tracks reject edits.
- Drags use `beginGesture` → `fromBase(fn)` (recompute from the drag's start snapshot) → `endGesture`, so each drag is one undo step. The drag helper listens on `window` because a clip moved to another track is re-mounted (pointer capture would lose `pointerup`).
- Snapping (N, Alt inverts while dragging): clip edges on all tracks, playhead, markers, 0; 8 px threshold, with a yellow snap line.
- Markers: M adds one at the playhead; drag the flag on the ruler; edit label/time/color in Properties; Delete removes it.
- Selection is `{kind: "clip", clipId}` (the track is looked up), plus `{kind: "marker", id}`.
- Timeline fits 5 tracks at 1366×768 (38 px rows, timeline 40% by default).

## Phase 2b: what exists

- `frontend/src/player/engine.ts` (`PreviewEngine`): one master clock (wall time × rate). Each clip near the playhead (1.5 s preroll) gets its own detached `<video>`/`<audio>` on the proxy, slaved to the clock (seeks when drift exceeds 0.25 s). Frames are drawn to a 2D canvas: background → main track → overlay tracks in track order. A frame is only drawn once every layer is ready (no flashes); the old frame stays while seeking.
- Audio: every element routes through WebAudio (`gain` per clip → master → analyser). Volume above 100%, linear fades, and track/clip mute work. Audio is muted in reverse or above 2× shuttle. Stopping silences immediately (from the store subscription, not the next frame).
- `window.__ligxtPreview.debugState()` / `.level()` expose element state and output RMS for browser tests.
- `player/geometry.ts`: layer geometry shared in spirit with `render.overlay_geometry` (unit-tested with the same numbers as the backend pixel test).
- J/K/L shuttle (store `rate`: ±1/2/4/8), scrub bar under the preview, safe-area guides for 9:16 (Reels header, caption area, button column; 90% box for other aspects), fullscreen.
- Overlay tracks in export: `overlay` filter per clip with `setpts+offset` and `enable=between(...)`. Fit/fill, transform scale/x/y/rotation/flip/opacity; later tracks on top; hidden tracks skipped. Overlay video audio is mixed like an audio clip.
- Overlay clip Properties: scale %, X/Y px, opacity % (full transform/keyframe UI in Phase 5).
- Beat detection: `backend/app/beats.py` (numpy: spectral flux, tempo via autocorrelation with a 120 BPM prior, Ellis DP beat tracker, +70 ms window-lag correction). `POST /api/media/{id}/beats` runs as a job and stores `bpm` + `beats` in the media meta. Accuracy on click tracks at 90–150 BPM: tempo within 1 BPM, beats within 30 ms (test). The UI shows beat ticks on music/audio clips, beats are snap targets, and there are "markers every beat / 2nd / bar" buttons.
- Tests: backend 15 (`test_phase2.py` adds overlay pixel tests, overlay audio, beats); frontend 30 (vitest). Browser-verified: preview and export put the overlay in the same place (same sample points), 1×/2×/reverse rates exact, audio levels respond to track mutes, no drift over 3 s, scrub redraws across cuts.

## Decisions

- Project length = end of the main video track (music running longer is cut). `util.projectDuration` and `render.build_plan` agree on this.
- Overlaps are prevented in the UI (ops). The renderer still tolerates them (the earlier clip wins) for hand-edited JSON.
- Output is forced to TV range (`scale=out_range=tv`, `-color_range tv`) so JPEG inputs don't produce yuvj420p.
- Preview is a 2D canvas fed by proxy media elements. Phase 4 adds WebGL shaders for filters (render each layer through a shader before compositing).
- Track stacking: main video is the bottom layer; overlay tracks are drawn in the order listed, so lower rows in the timeline are on top.
- Main-track clip transform is not applied yet (preview and export both ignore it); overlays use it. Phase 5 adds it for the main track.

## Next (Phase 3: transitions)

- Transition blocks between adjacent clips on the main track (and overlays if practical); duration 0.2–2 s; "apply to all cuts".
- Export: `xfade` (fade, dissolve, fadeblack/fadewhite, slide/wipe ×4, zoomin, circleopen/close, pixelize, radial) plus a custom whip pan (directional blur + slide). Transitions overlap the clips, so the timeline gets shorter by each duration (or the clips' handles are used); decide and document.
- Preview: draw both clips during the transition with a canvas/WebGL approximation.
- `ops.pruneTransitions` already drops transitions whose clips stop being neighbours.

## Known issues / notes

- Gotcha in PowerShell 5.1: `nativeExe | Select-Object -First 1` sets a non-zero `$LASTEXITCODE`. Capture the output to a variable first.
- Speed: the export uses pitch-preserving `atempo`. The preview uses the browser's playbackRate. The speed UI arrives in Phase 5.
- Chrome throttles requestAnimationFrame when the window isn't focused. The preview clock uses wall time, so it catches up, but browser-automation timing readings can look jumpy.
- Beat detection was tested on synthetic click tracks only; check it on real songs during Phase 7 (templates use cut timing).
- The media library is per project (`project.media`); files stay in `media\` even when removed from a project (no purge UI yet).
