# Ligxt Video Editor: Progress

Read this first at the start of every session.

## Status

| Phase | Scope | State |
|-------|-------|-------|
| 1 | Backend, project format, media import + proxies, single-clip export | **Done** (2026-10-11) |
| 2 | Timeline editing (trim, split, reorder, ripple, snapping, markers, beats) + canvas preview player (J/K/L, safe areas) | Next |
| 3 | Transitions (xfade + whip pan) | |
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

## Decisions

- Project length = end of the main video track (music running longer is cut). `util.projectDuration` and `render.build_plan` agree on this.
- Overlapping clips on the main track: the earlier clip wins (Phase 2 should prevent overlaps in the UI).
- Output is forced to TV range (`scale=out_range=tv`, `-color_range tv`) so JPEG inputs don't produce yuvj420p.
- Preview is currently an HTML `<video>` over proxies (CSS object-fit for fit/fill, blurred thumbnail for blur). Phase 2 replaces it with a canvas/WebGL player.

## Next (Phase 2)

- Timeline: drag to move/reorder, trim edges, split (S), ripple delete, duplicate, snapping (clip edges, playhead, markers), markers (M), beat detection on the music track, add/remove tracks, overlap handling.
- Use `beginGesture/endGesture` for drags so each drag is one undo step.
- Canvas preview player: J/K/L shuttle, scrubbing, Reels safe-area guides, music/audio tracks audible in preview (WebAudio), overlay track compositing.
- Renderer: overlay track (`overlay` filter with `enable=`), reverse/freeze.

## Known issues / notes

- Gotcha in PowerShell 5.1: `nativeExe | Select-Object -First 1` sets a non-zero `$LASTEXITCODE`. Capture the output to a variable first.
- Speed: the export uses pitch-preserving `atempo`. The preview uses the browser's playbackRate. The speed UI arrives in Phase 5.
- The media library is per project (`project.media`); files stay in `media\` even when removed from a project (no purge UI yet).
