# Changelog

## 0.1.0 — 2026-10-24

First release.

- Contact sheets: `trim` → `fps` → `showinfo` → `scale` → `drawtext` → `tile` in
  a single ffmpeg pass, filtergraph passed inline to `-vf`.
- Timecodes burned into every tile; tile times taken from ffmpeg's own PTS.
- `sheets.json` manifest mapping every tile to a second.
- `--at TIME` for one full-size frame at a timestamp.
- Range selection with `--start`/`--end`, exact cutting via `trim`.
- Sampled by `--count` or `--interval`; never upscales past the source.
- Agent Skill in `skills/vidgrid/SKILL.md`.
- 21 tests, CI on Linux, macOS and Windows.
