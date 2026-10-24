---
name: vidgrid
description: Watch a video by turning it into timestamped contact sheets - a grid of stills with the timecode burned into every tile - plus a sheets.json manifest mapping each tile to a second. Then pull one full-size frame at any timestamp. Use when the user hands over an .mp4/.mov/.mkv/.webm and asks what happens in it, when debugging a UI bug from a screen recording, or when reviewing gameplay footage.
---

# vidgrid

You cannot play video. You read images. `vidgrid` turns a clip into a few
contact sheets so one read covers many seconds, and every tile is labelled with
its own timecode.

## 1. Make the sheets

```bash
vidgrid <video> --out <dir>
```

Defaults are sensible: 36 frames, 3×3 tiles, 512 px per tile, timestamps on.
Use `--count` to change the number of frames and `--grid` for the layout.
Long clips: raise the count rather than the per-second rate.

Pick a smaller range when you already know roughly where to look:

```bash
vidgrid <video> --start 0:45 --end 1:15 --count 18 --out <dir>
```

## 2. Read them

Read `sheet_01.jpg`, `sheet_02.jpg`, … in order. They are chronological:
top-left to bottom-right, and each sheet's time range is printed in the run
output and recorded in `sheets.json`.

Quote timestamps using the label printed on the tile, not an estimate. If a
tile is ambiguous, say so and zoom in rather than guessing.

## 3. Zoom

When you need detail, ask for the exact frame:

```bash
vidgrid <video> --at 1:23.5 --out <dir>
```

That writes a full-resolution `frame_00-01-23.500.jpg`. If the moment spans
several seconds, one denser sheet is often enough:

```bash
vidgrid <video> --start 1:20 --end 1:28 --grid 4x4 --count 16 --out <dir>
```

## Rules

- Prefer one 3×3 sheet (9 tiles) over nine separate frames.
- Never ask for 60 frames as 60 images; that is 60 reads.
- Always pass `--out` to a scratch directory, not the user's project root.
- `--json` prints the manifest when you need to parse sheet ranges.
- Do not describe what you did not see. If a tile is too small to read text, say
  the text is unreadable and request `--at` for that second.
