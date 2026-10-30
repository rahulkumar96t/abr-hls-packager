"""probe -> ladder -> (loudness pass 1) -> one-pass encode -> master playlist -> thumbnails ->
job manifest."""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from abr_pack import __version__
from abr_pack.codecs import audio_codec_of, video_codec_of
from abr_pack.encode import AUDIO_GROUP, AUDIO_NAME, EncodeSettings, build_encode_command
from abr_pack.ladder import DEFAULT_LADDER, Rendition, Rung, select_ladder
from abr_pack.loudness import LoudnessTarget, measure, second_pass_filter
from abr_pack.playlist import (
    AudioRendition,
    VariantStream,
    parse_media_playlist,
    render_master,
    segment_bitrates,
)
from abr_pack.probe import MediaInfo, probe
from abr_pack.thumbnails import plan_sprite, render_webvtt, sprite_command
from abr_pack.tools import ffmpeg_version, ffprobe_json, run

MASTER = "master.m3u8"
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class ThumbnailOptions:
    interval: float = 2.0
    width: int = 160
    columns: int = 10


@dataclass(frozen=True)
class PackageOptions:
    settings: EncodeSettings = field(default_factory=EncodeSettings)
    rungs: tuple[Rung, ...] = DEFAULT_LADDER
    max_renditions: int | None = None
    loudness: LoudnessTarget | None = None
    thumbnails: ThumbnailOptions | None = field(default_factory=ThumbnailOptions)
    overwrite: bool = False


@dataclass(frozen=True)
class RenditionStats:
    peak_bps: int
    average_bps: int
    segments: int
    bytes: int


def _prepare(out_dir: Path, overwrite: bool) -> None:
    if out_dir.exists() and any(out_dir.iterdir()):
        if not overwrite:
            raise FileExistsError(f"{out_dir} is not empty (use overwrite)")
        shutil.rmtree(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)


def _stats(playlist_path: Path) -> RenditionStats:
    playlist = parse_media_playlist(playlist_path.read_text())
    sizes = [(playlist_path.parent / s.uri).stat().st_size for s in playlist.segments]
    peak, average = segment_bitrates(sizes, [s.duration for s in playlist.segments])
    return RenditionStats(peak, average, len(sizes), sum(sizes))


def _stream_geometry(playlist_path: Path) -> tuple[int, int, Fraction]:
    data = ffprobe_json(
        ["-select_streams", "v:0", "-show_entries", "stream=width,height,avg_frame_rate",
         str(playlist_path)]
    )  # fmt: skip
    s = data["streams"][0]
    return int(s["width"]), int(s["height"]), Fraction(s["avg_frame_rate"])


def package(source: Path | str, out_dir: Path | str, options: PackageOptions | None = None):
    """Package ``source`` into ``out_dir``; returns the job manifest (also written as JSON)."""
    options = options or PackageOptions()
    settings = options.settings
    source, out_dir = Path(source), Path(out_dir)
    started = time.perf_counter()

    info: MediaInfo = probe(source)
    if info.video is None:
        raise ValueError(f"{source} has no video stream")
    renditions: list[Rendition] = select_ladder(
        info.video, options.rungs, gop_seconds=settings.gop_seconds,
        max_renditions=options.max_renditions,
    )  # fmt: skip
    _prepare(out_dir, options.overwrite)

    with_audio = info.audio is not None
    loudness: dict[str, Any] | None = None
    audio_filter = None
    if with_audio and options.loudness:
        measured = measure(source, options.loudness)
        loudness = {"target": vars(options.loudness), "input": measured.to_dict()}
        if measured.measurable:
            audio_filter = second_pass_filter(
                measured, options.loudness, settings.audio_sample_rate
            )
        else:
            loudness["skipped"] = "input is silent or too quiet to measure"

    argv = build_encode_command(source, out_dir, renditions, settings,
                                with_audio=with_audio, audio_filter=audio_filter)  # fmt: skip
    t0 = time.perf_counter()
    run(argv)
    encode_seconds = time.perf_counter() - t0

    audio_entry = None
    audio_stats = None
    if with_audio:
        audio_playlist = out_dir / AUDIO_NAME / "index.m3u8"
        audio_stats = _stats(audio_playlist)
        audio_codec = audio_codec_of(audio_playlist)
        audio_entry = {
            "uri": f"{AUDIO_NAME}/index.m3u8",
            "codec": audio_codec,
            "bitrate_kbps": settings.audio_kbps,
            "channels": settings.audio_channels,
            "measured": vars(audio_stats),
        }
        if loudness and audio_filter:
            loudness["output"] = measure(audio_playlist, options.loudness).to_dict()

    variants: list[VariantStream] = []
    rendition_entries = []
    for r in renditions:
        playlist_path = out_dir / r.name / "index.m3u8"
        stats = _stats(playlist_path)
        width, height, fps = _stream_geometry(playlist_path)
        video_codec = video_codec_of(playlist_path)
        codecs = f"{video_codec},{audio_entry['codec']}" if audio_entry else video_codec
        peak = stats.peak_bps + (audio_stats.peak_bps if audio_stats else 0)
        average = stats.average_bps + (audio_stats.average_bps if audio_stats else 0)
        variants.append(
            VariantStream(
                uri=f"{r.name}/index.m3u8", bandwidth=peak, average_bandwidth=average,
                width=width, height=height, codecs=codecs, frame_rate=float(fps),
                audio_group=AUDIO_GROUP if with_audio else None,
            )
        )  # fmt: skip
        rendition_entries.append(
            {**r.to_dict(), "uri": f"{r.name}/index.m3u8", "codecs": codecs,
             "gop_frames": r.gop_frames(settings.gop_seconds), "measured": vars(stats),
             "bandwidth": peak, "average_bandwidth": average}
        )  # fmt: skip

    audio_renditions = (
        [AudioRendition(group_id=AUDIO_GROUP, name="Main", uri=audio_entry["uri"],
                        channels=settings.audio_channels)]
        if audio_entry else []
    )  # fmt: skip
    (out_dir / MASTER).write_text(render_master(variants, audio_renditions))

    thumbs = None
    if options.thumbnails:
        t = options.thumbnails
        layout = plan_sprite(info.duration, t.interval, t.width, info.video.display_aspect,
                             t.columns)  # fmt: skip
        thumb_dir = out_dir / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)
        run(sprite_command(source, thumb_dir / "sprite.jpg", layout))
        (thumb_dir / "thumbnails.vtt").write_text(
            render_webvtt(layout, info.duration, "sprite.jpg")
        )
        thumbs = {"sprite": "thumbnails/sprite.jpg", "vtt": "thumbnails/thumbnails.vtt",
                  **layout.to_dict()}  # fmt: skip

    manifest = {
        "tool": {"name": "abr-pack", "version": __version__, "ffmpeg": ffmpeg_version()},
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "input": info.to_dict(),
        "settings": settings.to_dict(),
        "master": MASTER,
        "renditions": rendition_entries,
        "audio": audio_entry,
        "loudness": loudness,
        "thumbnails": thumbs,
        "ffmpeg_command": argv,
        "timings": {
            "encode_seconds": round(encode_seconds, 3),
            "total_seconds": round(time.perf_counter() - started, 3),
        },
    }
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
