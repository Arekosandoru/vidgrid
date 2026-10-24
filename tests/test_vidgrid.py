"""End-to-end tests. Only ffmpeg and ffprobe are required - no network, no fixtures."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import vidgrid  # noqa: E402


def make_clip(path: Path, seconds: float, size: str = "320x240", rate: int = 10) -> Path:
    subprocess.run(
        [
            "ffmpeg", "-y", "-loglevel", "error",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}",
            "-t", str(seconds), "-pix_fmt", "yuv420p", str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


@pytest.fixture(scope="session")
def clip(tmp_path_factory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clips") / "clip.mp4", 6)


@pytest.fixture(scope="session")
def long_clip(tmp_path_factory) -> Path:
    return make_clip(tmp_path_factory.mktemp("clips") / "long.mp4", 20, size="160x120", rate=5)


def read_manifest(out: Path) -> dict:
    return json.loads((out / "sheets.json").read_text(encoding="utf-8"))


def all_tiles(manifest: dict) -> list[dict]:
    return [tile for sheet in manifest["sheets"] for tile in sheet["tiles"]]


# ---------------------------------------------------------------- units


@pytest.mark.parametrize(
    "text,expected",
    [("83", 83.0), ("1:23", 83.0), ("0:01:23.5", 83.5), ("1m23s", 83.0), ("0", 0.0)],
)
def test_parse_time(text: str, expected: float) -> None:
    assert vidgrid.parse_time(text) == pytest.approx(expected)


@pytest.mark.parametrize("text,expected", [("3x3", (3, 3)), ("4x2", (4, 2)), ("4 x 2", (4, 2))])
def test_parse_grid(text: str, expected: tuple[int, int]) -> None:
    assert vidgrid.parse_grid(text) == expected


@pytest.mark.parametrize("text", ["3", "0x3", "13x2", ""])
def test_parse_grid_rejects_junk(text: str) -> None:
    with pytest.raises(vidgrid.VidgridError):
        vidgrid.parse_grid(text)


def test_fmt_ts() -> None:
    assert vidgrid.fmt_ts(83.5) == "00:01:23.500"
    assert vidgrid.fmt_ts(3661) == "01:01:01.000"


# ---------------------------------------------------------------- end to end


def test_single_sheet(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main([str(clip), "--out", str(out), "--interval", "1"]) == 0

    manifest = read_manifest(out)
    tiles = manifest["sheets"][0]["tiles"]
    assert len(tiles) == 6
    assert [round(t["time"]) for t in tiles] == [0, 1, 2, 3, 4, 5]
    assert tiles[0]["ts"] == "00:00:00.000"

    sheet = out / "sheet_01.jpg"
    assert sheet.is_file()
    width = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width", "-of", "csv=p=0", str(sheet)],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # The clip is 320px wide and vidgrid never upscales, so tiles stay at 320.
    assert width == str(3 * 320)


def test_multiple_sheets(long_clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main([str(long_clip), "--out", str(out), "--interval", "1", "--grid", "2x2"]) == 0

    manifest = read_manifest(out)
    assert len(manifest["sheets"]) == 5  # 20 frames, 4 per sheet
    assert len(all_tiles(manifest)) == 20
    assert len(manifest["sheets"][-1]["tiles"]) == 4


def test_short_video_becomes_one_tile(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main([str(clip), "--out", str(out), "--interval", "60", "--quiet"]) == 0
    assert len(all_tiles(read_manifest(out))) == 1


def test_count_drives_interval(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main([str(clip), "--out", str(out), "--count", "3", "--quiet"]) == 0
    assert len(all_tiles(read_manifest(out))) == 3


def test_without_timestamps(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main(
        [str(clip), "--out", str(out), "--interval", "2", "--no-timestamps", "--quiet"]
    ) == 0
    assert read_manifest(out)["timestamps"] is False


def test_timestamps_when_the_platform_can_draw(clip: Path, tmp_path: Path) -> None:
    """Homebrew's ffmpeg ships without drawtext, so this only runs where it exists."""
    if vidgrid.find_font(None) is None or not vidgrid.supports_drawtext():
        pytest.skip("no font or no drawtext filter on this platform")
    out = tmp_path / "sheets"
    assert vidgrid.main([str(clip), "--out", str(out), "--interval", "3", "--quiet"]) == 0
    assert read_manifest(out)["timestamps"] is True


def test_grab_single_frame(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "frames"
    assert vidgrid.main([str(clip), "--out", str(out), "--at", "0:00:03"]) == 0
    assert len(list(out.glob("frame_*.jpg"))) == 1


def test_range_start_and_end(clip: Path, tmp_path: Path) -> None:
    out = tmp_path / "sheets"
    assert vidgrid.main(
        [str(clip), "--out", str(out), "--start", "2", "--end", "4", "--interval", "1", "--quiet"]
    ) == 0
    times = [round(t["time"]) for t in all_tiles(read_manifest(out))]
    assert times == [2, 3]


def test_missing_file_is_a_clean_error(tmp_path: Path) -> None:
    assert vidgrid.main([str(tmp_path / "nope.mp4")]) == 1
