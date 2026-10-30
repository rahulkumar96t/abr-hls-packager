"""Build the single-pass, multi-rendition FFmpeg command.

One decode feeds a ``split`` into one scaler per rendition, so every rendition sees exactly the
same frames. GOPs are fixed in *time* (``-force_key_frames expr:gte(t,n_forced*G)``) with scene-cut
detection disabled and a matching ``-g``/``-keyint_min``, so keyframes - and therefore segment
boundaries - land on the same timestamps in every rendition and players can switch cleanly.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path
from typing import Literal

from abr_pack.ladder import Rendition

SegmentFormat = Literal["fmp4", "ts"]
AUDIO_GROUP = "aud"
AUDIO_NAME = "audio"


@dataclass(frozen=True)
class EncodeSettings:
    segment_format: SegmentFormat = "fmp4"
    segment_seconds: float = 4.0
    gop_seconds: float = 2.0
    preset: str = "veryfast"
    audio_kbps: int = 128
    audio_channels: int = 2
    audio_sample_rate: int = 48_000

    def __post_init__(self) -> None:
        if self.segment_format not in ("fmp4", "ts"):
            raise ValueError("segment_format must be 'fmp4' or 'ts'")
        if self.gop_seconds <= 0 or self.segment_seconds <= 0:
            raise ValueError("gop_seconds and segment_seconds must be positive")
        ratio = Fraction(self.segment_seconds).limit_denominator(1000) / Fraction(
            self.gop_seconds
        ).limit_denominator(1000)
        if ratio.denominator != 1:
            raise ValueError("segment_seconds must be a whole multiple of gop_seconds")

    @property
    def segment_extension(self) -> str:
        return "m4s" if self.segment_format == "fmp4" else "ts"

    def to_dict(self) -> dict:
        return asdict(self)


def filter_graph(renditions: list[Rendition]) -> str:
    splits = "".join(f"[s{i}]" for i in range(len(renditions)))
    parts = [f"[0:v]split={len(renditions)}{splits}"]
    for i, r in enumerate(renditions):
        chain = [f"scale={r.width}:{r.height}:flags=bicubic", "setsar=1"]
        chain.append(f"fps={r.fps.numerator}/{r.fps.denominator}")
        chain.append("format=yuv420p")
        parts.append(f"[s{i}]{','.join(chain)}[v{i}]")
    return ";".join(parts)


def var_stream_map(renditions: list[Rendition], with_audio: bool) -> str:
    group = f",agroup:{AUDIO_GROUP}" if with_audio else ""
    entries = [f"v:{i}{group},name:{r.name}" for i, r in enumerate(renditions)]
    if with_audio:
        entries.append(f"a:0{group},name:{AUDIO_NAME}")
    return " ".join(entries)


def build_encode_command(
    source: Path | str,
    out_dir: Path | str,
    renditions: list[Rendition],
    settings: EncodeSettings,
    *,
    with_audio: bool,
    audio_filter: str | None = None,
) -> list[str]:
    if not renditions:
        raise ValueError("at least one rendition is required")
    out = Path(out_dir)
    gop = f"{settings.gop_seconds:g}"
    argv = ["ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(source),
            "-filter_complex", filter_graph(renditions)]  # fmt: skip
    for i, r in enumerate(renditions):
        gop_frames = str(r.gop_frames(settings.gop_seconds))
        argv += [
            "-map", f"[v{i}]",
            f"-c:v:{i}", "libx264",
            f"-b:v:{i}", f"{r.video_kbps}k",
            f"-maxrate:v:{i}", f"{r.maxrate_kbps}k",
            f"-bufsize:v:{i}", f"{r.bufsize_kbps}k",
            f"-profile:v:{i}", r.profile,
            f"-level:v:{i}", r.level,
            f"-g:v:{i}", gop_frames,
            f"-keyint_min:v:{i}", gop_frames,
            f"-force_key_frames:v:{i}", f"expr:gte(t,n_forced*{gop})",
        ]  # fmt: skip
    argv += ["-preset", settings.preset, "-sc_threshold", "0"]
    if with_audio:
        argv += [
            "-map", "0:a:0", "-c:a", "aac", "-b:a", f"{settings.audio_kbps}k",
            "-ac", str(settings.audio_channels), "-ar", str(settings.audio_sample_rate),
        ]  # fmt: skip
        if audio_filter:
            argv += ["-af", audio_filter]
    argv += [
        "-f", "hls",
        "-hls_time", f"{settings.segment_seconds:g}",
        "-hls_playlist_type", "vod",
        "-hls_flags", "independent_segments",
        "-hls_segment_type", "fmp4" if settings.segment_format == "fmp4" else "mpegts",
    ]  # fmt: skip
    if settings.segment_format == "fmp4":
        argv += ["-hls_fmp4_init_filename", "init.mp4"]
    argv += [
        "-hls_segment_filename", str(out / "%v" / f"seg_%05d.{settings.segment_extension}"),
        "-var_stream_map", var_stream_map(renditions, with_audio),
        str(out / "%v" / "index.m3u8"),
    ]  # fmt: skip
    return argv
