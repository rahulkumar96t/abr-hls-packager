from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from abr_pack import tools
from abr_pack.probe import VideoInfo

requires_ffmpeg = pytest.mark.skipif(not tools.available(), reason="ffmpeg/ffprobe not installed")


def video(width: int, height: int, fps: float | Fraction = 30, kbps: int | None = None,
          sar: Fraction = Fraction(1), rotation: int = 0) -> VideoInfo:  # fmt: skip
    dw, dh = round(width * sar), height
    if rotation in (90, 270):
        dw, dh = dh, dw
    return VideoInfo(
        codec="h264", width=width, height=height, display_width=dw, display_height=dh,
        fps=Fraction(fps).limit_denominator(1001), bit_rate=kbps * 1000 if kbps else None,
        rotation=rotation, pix_fmt="yuv420p",
    )  # fmt: skip


def make_clip(path: Path, *, size: str, rate: int, seconds: float, audio: bool) -> Path:
    """A synthetic clip from FFmpeg's lavfi sources: testsrc2 video, optional sine audio."""
    argv = [tools.ffmpeg_bin(), "-v", "error", "-y",
            "-f", "lavfi", "-i", f"testsrc2=size={size}:rate={rate}"]  # fmt: skip
    if audio:
        argv += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000"]
    argv += ["-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
    if audio:
        argv += ["-c:a", "aac", "-b:a", "128k"]
    tools.run([*argv, str(path)])
    return path


@pytest.fixture(scope="session")
def clip_720p(tmp_path_factory) -> Path:
    if not tools.available():
        pytest.skip("ffmpeg/ffprobe not installed")
    path = tmp_path_factory.mktemp("media") / "clip_720p.mp4"
    return make_clip(path, size="1280x720", rate=30, seconds=6, audio=True)


@pytest.fixture(scope="session")
def clip_4x3_silent_video(tmp_path_factory) -> Path:
    if not tools.available():
        pytest.skip("ffmpeg/ffprobe not installed")
    path = tmp_path_factory.mktemp("media") / "clip_4x3.mp4"
    return make_clip(path, size="640x480", rate=25, seconds=4, audio=False)
