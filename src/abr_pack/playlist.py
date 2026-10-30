"""Minimal HLS playlist parsing and master playlist writing (RFC 8216)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from fractions import Fraction


@dataclass(frozen=True)
class Segment:
    uri: str
    duration: float


@dataclass(frozen=True)
class MediaPlaylist:
    version: int | None
    target_duration: int
    segments: tuple[Segment, ...]
    init_uri: str | None = None
    endlist: bool = False
    playlist_type: str | None = None
    independent_segments: bool = False

    @property
    def duration(self) -> float:
        return sum(s.duration for s in self.segments)

    def boundaries(self) -> list[float]:
        """Start time of every segment, relative to the first one."""
        out, t = [], 0.0
        for s in self.segments:
            out.append(t)
            t += s.duration
        return out


@dataclass(frozen=True)
class VariantStream:
    uri: str
    bandwidth: int
    average_bandwidth: int
    width: int
    height: int
    codecs: str
    frame_rate: float
    audio_group: str | None = None


@dataclass(frozen=True)
class AudioRendition:
    group_id: str
    name: str
    uri: str
    channels: int
    language: str | None = None
    default: bool = True


@dataclass
class MasterPlaylist:
    variants: list[tuple[dict[str, str], str]] = field(default_factory=list)
    media: list[dict[str, str]] = field(default_factory=list)
    independent_segments: bool = False


_ATTR = re.compile(r'([A-Z0-9-]+)=("[^"]*"|[^,]*)')


def parse_attributes(text: str) -> dict[str, str]:
    return {k: v.strip('"') for k, v in _ATTR.findall(text)}


def parse_media_playlist(text: str) -> MediaPlaylist:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not an M3U8 playlist (missing #EXTM3U)")
    version = target = None
    init_uri = playlist_type = None
    endlist = independent = False
    segments: list[Segment] = []
    pending: float | None = None
    for line in lines[1:]:
        if line.startswith("#EXT-X-VERSION:"):
            version = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MAP:"):
            init_uri = parse_attributes(line.split(":", 1)[1]).get("URI")
        elif line.startswith("#EXT-X-PLAYLIST-TYPE:"):
            playlist_type = line.split(":", 1)[1]
        elif line == "#EXT-X-ENDLIST":
            endlist = True
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            independent = True
        elif line.startswith("#EXTINF:"):
            pending = float(line.split(":", 1)[1].split(",", 1)[0])
        elif not line.startswith("#"):
            if pending is None:
                raise ValueError(f"segment {line!r} without #EXTINF")
            segments.append(Segment(uri=line, duration=pending))
            pending = None
    if target is None:
        raise ValueError("missing #EXT-X-TARGETDURATION")
    return MediaPlaylist(version, target, tuple(segments), init_uri, endlist, playlist_type,
                         independent)  # fmt: skip


def parse_master_playlist(text: str) -> MasterPlaylist:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines or lines[0] != "#EXTM3U":
        raise ValueError("not an M3U8 playlist (missing #EXTM3U)")
    master = MasterPlaylist()
    pending: dict[str, str] | None = None
    for line in lines[1:]:
        if line.startswith("#EXT-X-STREAM-INF:"):
            pending = parse_attributes(line.split(":", 1)[1])
        elif line.startswith("#EXT-X-MEDIA:"):
            master.media.append(parse_attributes(line.split(":", 1)[1]))
        elif line == "#EXT-X-INDEPENDENT-SEGMENTS":
            master.independent_segments = True
        elif not line.startswith("#") and pending is not None:
            master.variants.append((pending, line))
            pending = None
    return master


def segment_bitrates(sizes: list[int], durations: list[float]) -> tuple[int, int]:
    """(peak, average) bits per second over media segments, as BANDWIDTH and AVERAGE-BANDWIDTH
    are defined for a single rendition."""
    if not sizes or len(sizes) != len(durations):
        raise ValueError("need one size per segment")
    peak = max(size * 8 / d for size, d in zip(sizes, durations, strict=True) if d > 0)
    average = sum(sizes) * 8 / sum(durations)
    return math.ceil(peak), math.ceil(average)


def format_frame_rate(fps: Fraction | float) -> str:
    return f"{float(fps):.3f}"


def render_master(
    variants: list[VariantStream],
    audio: list[AudioRendition] | None = None,
    *,
    version: int = 6,
) -> str:
    """Master playlist; variants are listed by ascending BANDWIDTH."""
    lines = ["#EXTM3U", f"#EXT-X-VERSION:{version}", "#EXT-X-INDEPENDENT-SEGMENTS", ""]
    for a in audio or []:
        attrs = [
            "TYPE=AUDIO",
            f'GROUP-ID="{a.group_id}"',
            f'NAME="{a.name}"',
            *([f'LANGUAGE="{a.language}"'] if a.language else []),
            f"DEFAULT={'YES' if a.default else 'NO'}",
            "AUTOSELECT=YES",
            f'CHANNELS="{a.channels}"',
            f'URI="{a.uri}"',
        ]
        lines.append("#EXT-X-MEDIA:" + ",".join(attrs))
    if audio:
        lines.append("")
    for v in sorted(variants, key=lambda v: v.bandwidth):
        attrs = [
            f"BANDWIDTH={v.bandwidth}",
            f"AVERAGE-BANDWIDTH={v.average_bandwidth}",
            f"RESOLUTION={v.width}x{v.height}",
            f'CODECS="{v.codecs}"',
            f"FRAME-RATE={format_frame_rate(v.frame_rate)}",
            *([f'AUDIO="{v.audio_group}"'] if v.audio_group else []),
        ]
        lines += ["#EXT-X-STREAM-INF:" + ",".join(attrs), v.uri]
    return "\n".join(lines) + "\n"
