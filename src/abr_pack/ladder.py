"""Choose an ABR rendition ladder for a given source.

Rules:

* never upscale: a rung is used only if the source's short side is at least the rung's size;
  a source smaller than the lowest rung gets a single rendition at its own size;
* keep the display aspect ratio (sample aspect ratio and rotation already applied by the probe)
  and round both dimensions to even numbers, which 4:2:0 chroma subsampling requires;
* portrait sources use the rung size for the width (the short side);
* above 30 fps, low rungs run at half the frame rate when that keeps the GOP an integer number
  of frames (otherwise keyframes could not stay aligned with the full-rate renditions);
* bitrates scale with pixel count and frame rate, are capped by the source bitrate, and each
  rendition gets the lowest H.264 level that fits its frame size, macroblock rate and VBV.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any

from abr_pack.probe import VideoInfo


@dataclass(frozen=True)
class Rung:
    size: int  # nominal short side, e.g. 720 for "720p"
    video_kbps: int


DEFAULT_LADDER: tuple[Rung, ...] = (
    Rung(1080, 5000),
    Rung(720, 2800),
    Rung(480, 1400),
    Rung(360, 800),
    Rung(240, 400),
)


@dataclass(frozen=True)
class Rendition:
    name: str
    width: int
    height: int
    fps: Fraction
    video_kbps: int
    maxrate_kbps: int
    bufsize_kbps: int
    profile: str
    level: str

    def gop_frames(self, gop_seconds: float) -> int:
        return max(1, round(self.fps * Fraction(gop_seconds).limit_denominator(1000)))

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["fps"] = float(self.fps)
        return out


# level: (max macroblocks/s, max frame size in macroblocks, max bitrate kbps, max CPB kbit)
# Bitrate and CPB limits are the Baseline/Main values; High profile allows 1.25x.
H264_LEVELS: dict[str, tuple[int, int, int, int]] = {
    "3.0": (40_500, 1_620, 10_000, 10_000),
    "3.1": (108_000, 3_600, 14_000, 14_000),
    "3.2": (216_000, 5_120, 20_000, 20_000),
    "4.0": (245_760, 8_192, 20_000, 25_000),
    "4.1": (245_760, 8_192, 50_000, 62_500),
    "4.2": (522_240, 8_704, 50_000, 62_500),
    "5.0": (589_824, 22_080, 135_000, 135_000),
    "5.1": (983_040, 36_864, 240_000, 240_000),
    "5.2": (2_073_600, 36_864, 240_000, 240_000),
}
HIGH_PROFILE_FACTOR = 1.25


def even(value: float) -> int:
    """Nearest even integer, at least 2."""
    return max(2, round(value / 2) * 2)


def h264_level(width: int, height: int, fps: Fraction | float, maxrate_kbps: int,
               bufsize_kbps: int, profile: str = "high") -> str:  # fmt: skip
    frame_mbs = math.ceil(width / 16) * math.ceil(height / 16)
    mb_rate = frame_mbs * float(fps)
    factor = HIGH_PROFILE_FACTOR if profile == "high" else 1.0
    for level, (max_mbps, max_fs, max_br, max_cpb) in H264_LEVELS.items():
        if (
            frame_mbs <= max_fs
            and mb_rate <= max_mbps
            and maxrate_kbps <= max_br * factor
            and bufsize_kbps <= max_cpb * factor
        ):
            return level
    raise ValueError(f"{width}x{height}@{float(fps):.2f} exceeds H.264 level 5.2")


def _dimensions(size: int, aspect: Fraction, portrait: bool, limit: tuple[int, int]):
    """Even (width, height) for a rung, clamped so rounding never exceeds the source."""
    if portrait:
        width, height = even(size), even(size / aspect)
    else:
        width, height = even(size * aspect), even(size)
    max_w, max_h = (max(2, v - v % 2) for v in limit)
    return min(width, max_w), min(height, max_h)


def _rung_fps(source_fps: Fraction, size: int, gop_seconds: float, low_fps_max_size: int):
    if source_fps <= 30 or size > low_fps_max_size:
        return source_fps
    half = source_fps / 2
    frames = half * Fraction(gop_seconds).limit_denominator(1000)
    return half if frames.denominator == 1 else source_fps


def _bitrate(rung: Rung, width: int, height: int, fps: Fraction, source_kbps: int | None) -> int:
    nominal_pixels = even(rung.size * 16 / 9) * rung.size
    pixel_factor = ((width * height) / nominal_pixels) ** 0.75
    fps_factor = (max(float(fps), 24.0) / 30.0) ** 0.6
    kbps = rung.video_kbps * pixel_factor * fps_factor
    if source_kbps:
        kbps = min(kbps, source_kbps)  # never spend more bits than the source has
    return max(100, round(kbps / 50) * 50)


def select_ladder(
    video: VideoInfo,
    rungs: tuple[Rung, ...] = DEFAULT_LADDER,
    *,
    gop_seconds: float = 2.0,
    max_renditions: int | None = None,
    low_fps_max_size: int = 480,
) -> list[Rendition]:
    aspect = video.display_aspect
    portrait = video.portrait
    short_side = min(video.display_width, video.display_height)
    source_kbps = round(video.bit_rate / 1000) if video.bit_rate else None

    ordered = sorted(rungs, key=lambda r: r.size, reverse=True)
    eligible = [r for r in ordered if r.size <= short_side]
    if not eligible:  # tiny source: one rendition at its own size, never upscaled
        lowest = ordered[-1]
        size = short_side - short_side % 2
        eligible = [Rung(size, round(lowest.video_kbps * (size / lowest.size) ** 1.5))]

    renditions: list[Rendition] = []
    seen: set[tuple[int, int]] = set()
    for rung in eligible:
        width, height = _dimensions(
            rung.size, aspect, portrait, (video.display_width, video.display_height)
        )
        if (width, height) in seen:
            continue
        seen.add((width, height))
        fps = _rung_fps(video.fps, rung.size, gop_seconds, low_fps_max_size)
        kbps = _bitrate(rung, width, height, fps, source_kbps)
        maxrate, bufsize = round(kbps * 1.1), kbps * 2
        renditions.append(
            Rendition(
                name=f"{rung.size}p",
                width=width,
                height=height,
                fps=fps,
                video_kbps=kbps,
                maxrate_kbps=maxrate,
                bufsize_kbps=bufsize,
                profile="high",
                level=h264_level(width, height, fps, maxrate, bufsize),
            )
        )
    if max_renditions:
        renditions = renditions[:max_renditions]
    return renditions
