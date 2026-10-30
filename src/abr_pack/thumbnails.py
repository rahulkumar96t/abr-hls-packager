"""Thumbnail sprite sheet + WebVTT track for scrubbing previews.

One JPEG holds a grid of evenly spaced frames; the WebVTT file maps each time range to a tile
with a media fragment (``sprite.jpg#xywh=x,y,w,h``), the format most web players understand.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from pathlib import Path

from abr_pack.ladder import even


@dataclass(frozen=True)
class SpriteLayout:
    interval: float
    count: int
    columns: int
    rows: int
    tile_width: int
    tile_height: int

    @property
    def width(self) -> int:
        return self.columns * self.tile_width

    @property
    def height(self) -> int:
        return self.rows * self.tile_height

    def tile_origin(self, index: int) -> tuple[int, int]:
        return (index % self.columns) * self.tile_width, (index // self.columns) * self.tile_height

    def to_dict(self) -> dict:
        return {**asdict(self), "sprite_width": self.width, "sprite_height": self.height}


def plan_sprite(duration: float, interval: float, tile_width: int, aspect: Fraction,
                columns: int = 10) -> SpriteLayout:  # fmt: skip
    if duration <= 0 or interval <= 0:
        raise ValueError("duration and interval must be positive")
    count = max(1, math.ceil(duration / interval - 1e-9))
    cols = min(columns, count)
    width = even(tile_width)
    return SpriteLayout(
        interval=interval,
        count=count,
        columns=cols,
        rows=math.ceil(count / cols),
        tile_width=width,
        tile_height=even(width / aspect),
    )


def vtt_timestamp(seconds: float) -> str:
    millis = round(seconds * 1000)
    hours, rest = divmod(millis, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, ms = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{ms:03d}"


def render_webvtt(layout: SpriteLayout, duration: float, sprite_uri: str) -> str:
    lines = ["WEBVTT", ""]
    for i in range(layout.count):
        start = i * layout.interval
        end = min((i + 1) * layout.interval, duration)
        if end <= start:
            break
        x, y = layout.tile_origin(i)
        lines += [
            f"{vtt_timestamp(start)} --> {vtt_timestamp(end)}",
            f"{sprite_uri}#xywh={x},{y},{layout.tile_width},{layout.tile_height}",
            "",
        ]
    return "\n".join(lines)


def sprite_command(source: Path | str, out_path: Path | str, layout: SpriteLayout) -> list[str]:
    vf = (
        f"fps={1 / layout.interval:.6f},"
        f"scale={layout.tile_width}:{layout.tile_height},setsar=1,"
        f"tile={layout.columns}x{layout.rows}"
    )
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-y", "-v", "error", "-i", str(source),
        "-an", "-vf", vf, "-frames:v", "1", "-q:v", "4", "-update", "1", str(out_path),
    ]  # fmt: skip
