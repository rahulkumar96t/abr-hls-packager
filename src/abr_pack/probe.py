"""ffprobe (JSON) -> a small, typed description of the input."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from abr_pack.tools import ffprobe_json


@dataclass(frozen=True)
class VideoInfo:
    codec: str
    width: int  # coded size
    height: int
    display_width: int  # after sample aspect ratio and rotation
    display_height: int
    fps: Fraction
    bit_rate: int | None
    rotation: int
    pix_fmt: str | None

    @property
    def display_aspect(self) -> Fraction:
        return Fraction(self.display_width, self.display_height)

    @property
    def portrait(self) -> bool:
        return self.display_height > self.display_width


@dataclass(frozen=True)
class AudioInfo:
    codec: str
    channels: int
    sample_rate: int
    bit_rate: int | None
    profile: str | None


@dataclass(frozen=True)
class MediaInfo:
    path: str
    duration: float
    format_name: str
    video: VideoInfo | None
    audio: AudioInfo | None

    def to_dict(self) -> dict[str, Any]:
        out = asdict(self)
        if self.video:
            out["video"]["fps"] = float(self.video.fps)
        return out


def _fraction(value: str | None) -> Fraction | None:
    if not value or value in ("0/0", "N/A"):
        return None
    try:
        frac = Fraction(value)
    except (ValueError, ZeroDivisionError):
        return None
    return frac if frac > 0 else None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _rotation(stream: dict[str, Any]) -> int:
    for side in stream.get("side_data_list", []) or []:
        if "rotation" in side:
            return int(float(side["rotation"])) % 360
    tag = (stream.get("tags") or {}).get("rotate")
    return int(tag) % 360 if tag else 0


def parse_probe(data: dict[str, Any], path: str = "") -> MediaInfo:
    """Build :class:`MediaInfo` from ``ffprobe -show_format -show_streams`` JSON."""
    streams = data.get("streams", [])
    fmt = data.get("format", {})
    video_stream = next(
        (
            s
            for s in streams
            if s.get("codec_type") == "video"
            and not (s.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    video = None
    if video_stream:
        w, h = int(video_stream["width"]), int(video_stream["height"])
        sar = _fraction(video_stream.get("sample_aspect_ratio", "").replace(":", "/")) or 1
        rotation = _rotation(video_stream)
        dw, dh = round(w * sar), h
        if rotation in (90, 270):
            dw, dh = dh, dw
        fps = (
            _fraction(video_stream.get("avg_frame_rate"))
            or _fraction(video_stream.get("r_frame_rate"))
            or Fraction(25)
        )
        video = VideoInfo(
            codec=video_stream.get("codec_name", "unknown"),
            width=w,
            height=h,
            display_width=dw,
            display_height=dh,
            fps=fps.limit_denominator(1001),
            bit_rate=_int(video_stream.get("bit_rate")),
            rotation=rotation,
            pix_fmt=video_stream.get("pix_fmt"),
        )
    audio = None
    if audio_stream:
        audio = AudioInfo(
            codec=audio_stream.get("codec_name", "unknown"),
            channels=_int(audio_stream.get("channels")) or 2,
            sample_rate=_int(audio_stream.get("sample_rate")) or 48_000,
            bit_rate=_int(audio_stream.get("bit_rate")),
            profile=audio_stream.get("profile"),
        )
    duration = float(fmt.get("duration") or (video_stream or audio_stream or {}).get("duration", 0))
    return MediaInfo(
        path=path,
        duration=duration,
        format_name=fmt.get("format_name", ""),
        video=video,
        audio=audio,
    )


def probe(path: str | Path) -> MediaInfo:
    data = ffprobe_json(["-show_format", "-show_streams", str(path)])
    info = parse_probe(data, str(path))
    if info.duration <= 0:
        raise ValueError(f"{path}: could not determine a duration")
    return info
