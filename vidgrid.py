#!/usr/bin/env python3
"""vidgrid - turn a video into timestamped contact sheets for AI agents.

A sheet is a grid of stills with a timecode burned into every tile, so an agent
can read a minute of video as one image, then ask for the exact frame it needs.

Requires: Python 3.10+, ffmpeg and ffprobe on PATH.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from pathlib import Path

__version__ = "0.1.0"

FONT_CANDIDATES = [
    # Windows
    r"C:\Windows\Fonts\arial.ttf",
    r"C:\Windows\Fonts\segoeui.ttf",
    r"C:\Windows\Fonts\tahoma.ttf",
    # macOS
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial.ttf",
    # Linux
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
]

DEFAULT_GRID = "3x3"
DEFAULT_TILE_WIDTH = 512
DEFAULT_COUNT = 36
DEFAULT_QUALITY = 3

PTS_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")


class VidgridError(Exception):
    """Anything the user can fix: missing ffmpeg, bad timestamp, unreadable video."""


# ---------------------------------------------------------------- parsing


def parse_time(text: str) -> float:
    """Accept 83, 1:23, 0:01:23.500 or 1m23s."""
    text = text.strip()
    if not text:
        raise VidgridError("empty timestamp")
    if text[-1] in "hms":
        total, num = 0.0, ""
        for ch in text.lower():
            if ch in "hms":
                if not num:
                    raise VidgridError(f"bad timestamp: {text!r}")
                total += float(num) * {"h": 3600, "m": 60, "s": 1}[ch]
                num = ""
            else:
                num += ch
        if num:
            raise VidgridError(f"bad timestamp: {text!r}")
        return total
    parts = text.split(":")
    if len(parts) > 3:
        raise VidgridError(f"bad timestamp: {text!r}")
    seconds = 0.0
    for part in parts:
        seconds = seconds * 60 + float(part)
    return seconds


def parse_grid(text: str) -> tuple[int, int]:
    """COLSxROWS, for example 3x3 or 4x2."""
    match = re.fullmatch(r"\s*(\d+)\s*[xX*,]\s*(\d+)\s*", text)
    if not match:
        raise VidgridError(f"bad grid {text!r}, expected COLSxROWS such as 3x3")
    cols, rows = int(match.group(1)), int(match.group(2))
    if not (1 <= cols <= 12 and 1 <= rows <= 12):
        raise VidgridError("grid must be between 1x1 and 12x12")
    return cols, rows


def fmt_ts(seconds: float) -> str:
    hours, rest = divmod(max(seconds, 0.0), 3600)
    minutes, secs = divmod(rest, 60)
    return f"{int(hours):02d}:{int(minutes):02d}:{secs:06.3f}"


# ---------------------------------------------------------------- ffmpeg plumbing


def _run(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
    except FileNotFoundError as exc:
        raise VidgridError(
            f"{cmd[0]} not found on PATH. Install ffmpeg:\n"
            "  Windows:  winget install --id Gyan.FFmpeg -e\n"
            "  macOS:    brew install ffmpeg\n"
            "  Linux:    sudo apt install ffmpeg"
        ) from exc


def require_tools() -> None:
    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            _run([tool, "-version"])  # raises the friendly error


def probe(video: Path) -> dict:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(video),
        ]
    )
    if result.returncode != 0:
        raise VidgridError(f"ffprobe cannot read {video.name}: {result.stderr.strip()[:400]}")
    data = json.loads(result.stdout or "{}")
    stream = next(
        (s for s in data.get("streams", []) if s.get("codec_type") == "video"), None
    )
    if stream is None:
        raise VidgridError(f"{video.name} has no video stream")
    duration = 0.0
    for candidate in (data.get("format", {}).get("duration"), stream.get("duration")):
        try:
            duration = float(candidate)
            break
        except (TypeError, ValueError):
            continue
    return {
        "duration": duration,
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
    }


def find_font(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise VidgridError(f"font not found: {explicit}")
        return path
    for candidate in FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    return None


_DRAWTEXT: bool | None = None


def supports_drawtext() -> bool:
    """ffmpeg can be built without libfreetype, so drawtext is not guaranteed."""
    global _DRAWTEXT
    if _DRAWTEXT is None:
        result = _run(["ffmpeg", "-hide_banner", "-filters"])
        _DRAWTEXT = result.returncode == 0 and "drawtext" in result.stdout
    return _DRAWTEXT


def build_filter(
    interval: float,
    tile_width: int,
    cols: int,
    rows: int,
    font_size: int | None,
    limit: float | None = None,
) -> str:
    # A fraction, not a rounded float: fps=1/6.000000000 becomes the exact rational
    # 1/6, so the last sample lands on the final frame instead of an epsilon past it.
    chain = []
    if limit is not None:
        # -t truncates the muxer, not the filtergraph, so tile still buffers frames
        # past the range; trim inside the graph is exact.
        chain.append(f"trim=duration={limit:.6f},setpts=PTS-STARTPTS")
    chain += [f"fps=1/{interval:.9f}", "showinfo", f"scale={tile_width}:-2"]
    if font_size:
        # font.ttf is copied next to the filter script, so no path escaping and
        # no colon/space bugs on any platform.
        chain.append(
            "drawtext=fontfile=font.ttf"
            f":text='%{{pts\\:hms}}'"
            f":x=6:y=6:fontsize={font_size}"
            ":fontcolor=white:box=1:boxcolor=black@0.65:boxborderw=4"
        )
    chain.append(f"tile={cols}x{rows}")
    return ",".join(chain)


# ---------------------------------------------------------------- core


def make_sheets(
    video: Path,
    out_dir: Path,
    *,
    cols: int,
    rows: int,
    interval: float,
    tile_width: int,
    quality: int,
    start: float,
    end: float | None,
    font: Path | None,
    timestamps: bool,
    quiet: bool,
) -> dict:
    info = probe(video)
    duration = end - start if end is not None else info["duration"] - start
    if duration <= 0:
        raise VidgridError("nothing to sample: the requested range is empty")
    # An interval longer than the clip makes fps emit zero frames; one frame is
    # the sensible floor.
    interval = min(interval, duration)

    width = min(tile_width, info["width"] or tile_width)
    font_size = None
    if timestamps:
        if font is None:
            if not quiet:
                print(
                    "vidgrid: no system font found, tiles get no timecodes"
                    " (pass --font, or --no-timestamps to silence this)",
                    file=sys.stderr,
                )
        elif not supports_drawtext():
            if not quiet:
                print(
                    "vidgrid: this ffmpeg build has no drawtext filter,"
                    " tiles get no timecodes (times stay in sheets.json)",
                    file=sys.stderr,
                )
        else:
            font_size = max(11, width // 26)

    out_dir.mkdir(parents=True, exist_ok=True)

    # Work dir lives next to the output, not in TMPDIR: same drive as the result,
    # and no surprises from restrictive temp directories.
    work = out_dir / ".vidgrid-work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    try:
        if font is not None and font_size:
            shutil.copyfile(font, work / "font.ttf")
        graph = build_filter(
            interval,
            width,
            cols,
            rows,
            font_size,
            limit=duration if end is not None else None,
        )
        pattern = out_dir / "sheet_%02d.jpg"
        cmd = [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "info",
            "-ss",
            f"{start:.3f}",
            "-i",
            str(video),
            "-vf",
            graph,
            "-q:v",
            str(quality),
            "-an",
            "-sn",
            str(pattern),
        ]
        # cwd=work: the graph names the font by bare filename, so no path and no
        # escaped colon ever reaches the filter parser.
        result = _run(cmd, cwd=work)
    finally:
        shutil.rmtree(work, ignore_errors=True)

    if result.returncode != 0:
        raise VidgridError(f"ffmpeg failed:\n{result.stderr.strip()[-1200:]}")

    times = [float(m) + start for m in PTS_RE.findall(result.stderr)]
    sheets = sorted(out_dir.glob("sheet_*.jpg"))
    if not sheets:
        raise VidgridError("no frames extracted - is the video decodable?")

    per_sheet = cols * rows
    if not times:
        times = [start + i * interval for i in range(len(sheets) * per_sheet)]
    # ffmpeg can emit a final partial sheet; never claim more tiles than exist.
    times = times[: len(sheets) * per_sheet]

    manifest = {
        "tool": f"vidgrid {__version__}",
        "video": str(video),
        "duration": round(info["duration"], 3),
        "range": [round(start, 3), None if end is None else round(end, 3)],
        "grid": [cols, rows],
        "tile_width": width,
        "interval": round(interval, 4),
        "timestamps": bool(font_size),
        "sheets": [],
    }
    for index, sheet in enumerate(sheets):
        chunk = times[index * per_sheet : (index + 1) * per_sheet]
        manifest["sheets"].append(
            {
                "file": sheet.name,
                "tiles": [
                    {"time": round(t, 3), "ts": fmt_ts(t)} for t in chunk
                ],
            }
        )

    (out_dir / "sheets.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    if not quiet:
        first = manifest["sheets"][0]["tiles"][0]["ts"] if manifest["sheets"][0]["tiles"] else "-"
        last_tiles = manifest["sheets"][-1]["tiles"]
        last = last_tiles[-1]["ts"] if last_tiles else "-"
        total = sum(len(s["tiles"]) for s in manifest["sheets"])
        print(
            f"{video.name}: {info['duration']:.1f}s, {cols}x{rows} tiles of {width}px, "
            f"{total} frames in {len(sheets)} sheet(s) ({first} -> {last})"
        )
        for sheet in manifest["sheets"]:
            if sheet["tiles"]:
                tag = "" if len(sheet["tiles"]) == per_sheet else "  (partial)"
                print(f"  {sheet['file']}  {sheet['tiles'][0]['ts']} .. {sheet['tiles'][-1]['ts']}{tag}")
        print(f"manifest: {out_dir / 'sheets.json'}")
    return manifest


def grab_frame(video: Path, at: float, out_path: Path, quality: int) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    result = _run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-nostdin",
            "-loglevel",
            "error",
            "-ss",
            f"{at:.3f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-q:v",
            str(quality),
            str(out_path),
        ]
    )
    if result.returncode != 0 or not out_path.is_file():
        raise VidgridError(f"cannot grab a frame at {fmt_ts(at)}: {result.stderr.strip()[:400]}")
    return out_path


# ---------------------------------------------------------------- cli


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vidgrid",
        description="Turn a video into timestamped contact sheets an AI agent can read.",
        epilog="Example: vidgrid clip.mp4 --grid 3x3 --count 36",
    )
    parser.add_argument("video", help="input video file")
    parser.add_argument("--out", default=None, help="output directory (default: ./<video>.vidgrid)")
    parser.add_argument("--grid", default=DEFAULT_GRID, help=f"COLSxROWS tiles per sheet (default {DEFAULT_GRID})")
    parser.add_argument(
        "--tile-width",
        type=int,
        default=DEFAULT_TILE_WIDTH,
        help=f"pixel width of one tile (default {DEFAULT_TILE_WIDTH})",
    )
    parser.add_argument("--count", type=int, default=None, help=f"total frames to sample (default {DEFAULT_COUNT})")
    parser.add_argument("--interval", default=None, help="seconds between frames, e.g. 3 or 0:00:02")
    parser.add_argument("--start", default="0", help="start of the range (default 0)")
    parser.add_argument("--end", default=None, help="end of the range")
    parser.add_argument("--quality", type=int, default=DEFAULT_QUALITY, help="JPEG quality 1-31, lower is better")
    parser.add_argument("--at", default=None, help="grab one full-size frame at this time instead of sheets")
    parser.add_argument("--font", default=None, help="font file for the timecode overlay")
    parser.add_argument("--no-timestamps", action="store_true", help="do not burn timecodes into the tiles")
    parser.add_argument("--json", action="store_true", help="print the manifest to stdout")
    parser.add_argument("--quiet", action="store_true", help="no human-readable summary")
    parser.add_argument("--version", action="version", version=f"vidgrid {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        require_tools()
        video = Path(args.video).expanduser()
        if not video.is_file():
            raise VidgridError(f"no such file: {video}")
        video = video.resolve()
        out_dir = (
            Path(args.out).expanduser().resolve()
            if args.out
            else Path(f"{video.stem}.vidgrid").resolve()
        )

        if args.at is not None:
            at = parse_time(args.at)
            target = out_dir / f"frame_{fmt_ts(at).replace(':', '-')}.jpg"
            grab_frame(video, at, target, args.quality)
            if args.json:
                print(json.dumps({"frame": str(target), "time": at}, indent=2))
            elif not args.quiet:
                print(f"frame at {fmt_ts(at)} -> {target}")
            return 0

        cols, rows = parse_grid(args.grid)
        if args.tile_width < 64:
            raise VidgridError("--tile-width must be at least 64")

        info = probe(video)
        start = parse_time(args.start)
        end = parse_time(args.end) if args.end else None

        if args.interval:
            interval = parse_time(args.interval)
            if interval <= 0:
                raise VidgridError("--interval must be positive")
        else:
            span = (end - start) if end is not None else (info["duration"] - start)
            count = args.count or DEFAULT_COUNT
            if count < 1:
                raise VidgridError("--count must be at least 1")
            interval = max(span / count, 1.0 / 60.0)

        manifest = make_sheets(
            video,
            out_dir,
            cols=cols,
            rows=rows,
            interval=interval,
            tile_width=args.tile_width,
            quality=args.quality,
            start=start,
            end=end,
            font=find_font(args.font),
            timestamps=not args.no_timestamps,
            quiet=args.quiet or args.json,
        )
        if args.json:
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0
    except VidgridError as exc:
        print(f"vidgrid: {exc}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"vidgrid: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
