"""Validate a packaged HLS output directory.

Checks, per rendition: the playlists parse, every referenced file exists, VOD playlists end with
``EXT-X-ENDLIST``, every ``EXTINF`` respects ``EXT-X-TARGETDURATION`` (RFC 8216 4.3.3.1) and
all but the last segment match the configured segment duration. Across video renditions:
identical segment boundaries, a keyframe at every boundary, and the same keyframe timeline.
The ``CODECS`` attribute is re-derived from the encoded SPS and compared.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

from abr_pack.codecs import video_codec_of
from abr_pack.playlist import MediaPlaylist, parse_master_playlist, parse_media_playlist
from abr_pack.tools import ffprobe_json


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(c.ok for c in self.checks)

    def add(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append(Check(name, ok, detail))

    def to_dict(self) -> dict:
        return {"ok": self.ok, "checks": [asdict(c) for c in self.checks]}

    def render(self) -> str:
        lines = [f"[{'PASS' if c.ok else 'FAIL'}] {c.name}: {c.detail}" for c in self.checks]
        failed = sum(not c.ok for c in self.checks)
        lines.append(f"{len(self.checks) - failed}/{len(self.checks)} checks passed")
        return "\n".join(lines)


def check_segment_durations(
    playlist: MediaPlaylist, expected: float | None, tolerance: float
) -> list[str]:
    """Problems with segment durations (empty list = fine)."""
    problems = []
    for i, seg in enumerate(playlist.segments):
        if round(seg.duration) > playlist.target_duration:
            problems.append(
                f"segment {i} lasts {seg.duration:.3f}s > target {playlist.target_duration}s"
            )
        last = i == len(playlist.segments) - 1
        if expected is not None and not last and abs(seg.duration - expected) > tolerance:
            problems.append(f"segment {i} lasts {seg.duration:.3f}s, expected {expected:g}s")
        if expected is not None and last and seg.duration > expected + tolerance:
            problems.append(f"last segment lasts {seg.duration:.3f}s > {expected:g}s")
    return problems


def check_keyframe_alignment(
    boundaries: dict[str, list[float]],
    keyframes: dict[str, list[float]],
    tolerance: float,
) -> list[str]:
    """Every rendition must cut segments at the same times, each on a keyframe."""
    problems = []
    names = list(boundaries)
    reference = boundaries[names[0]]
    for name in names[1:]:
        other = boundaries[name]
        if len(other) != len(reference):
            problems.append(f"{name} has {len(other)} segments, {names[0]} has {len(reference)}")
            continue
        worst = max((abs(a - b) for a, b in zip(reference, other, strict=True)), default=0.0)
        if worst > tolerance:
            problems.append(f"{name} boundaries differ from {names[0]} by {worst * 1000:.1f} ms")
    for name, starts in boundaries.items():
        frames = keyframes.get(name, [])
        for t in starts:
            nearest = min((abs(t - k) for k in frames), default=float("inf"))
            if nearest > tolerance:
                problems.append(f"{name}: no keyframe at segment start {t:.3f}s")
                break
    return problems


def keyframe_times(playlist_path: Path) -> list[float]:
    """Keyframe timestamps of the first video stream, relative to the first keyframe."""
    data = ffprobe_json(
        ["-select_streams", "v:0", "-skip_frame", "nokey", "-show_entries",
         "frame=pts_time,best_effort_timestamp_time", str(playlist_path)]
    )  # fmt: skip
    times = []
    for frame in data.get("frames", []):
        value = frame.get("pts_time", frame.get("best_effort_timestamp_time"))
        if value not in (None, "N/A"):
            times.append(float(value))
    if not times:
        return []
    first = min(times)
    return sorted(t - first for t in times)


def _load_media(report: Report, path: Path, label: str) -> MediaPlaylist | None:
    if not path.exists():
        report.add(f"{label}: playlist", False, f"{path.name} missing")
        return None
    try:
        playlist = parse_media_playlist(path.read_text())
    except ValueError as exc:
        report.add(f"{label}: playlist", False, str(exc))
        return None
    missing = [s.uri for s in playlist.segments if not (path.parent / s.uri).is_file()]
    present = [path.parent / s.uri for s in playlist.segments if (path.parent / s.uri).is_file()]
    empty = [f.name for f in present if f.stat().st_size == 0]
    if playlist.init_uri and not (path.parent / playlist.init_uri).is_file():
        missing.append(playlist.init_uri)
    report.add(
        f"{label}: files",
        not missing and not empty and bool(playlist.segments),
        f"{len(playlist.segments)} segments"
        + (f"; missing {missing[:3]}" if missing else "")
        + (f"; empty {empty[:3]}" if empty else ""),
    )
    report.add(f"{label}: endlist", playlist.endlist, "VOD playlist ends with EXT-X-ENDLIST")
    return playlist


def validate(out_dir: Path | str) -> Report:
    out = Path(out_dir)
    report = Report()
    master_path = out / "master.m3u8"
    if not master_path.exists():
        report.add("master playlist", False, "master.m3u8 missing")
        return report
    master = parse_master_playlist(master_path.read_text())
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    settings = manifest.get("settings", {})
    expected = settings.get("segment_seconds")
    gop = settings.get("gop_seconds")

    required = ("BANDWIDTH", "AVERAGE-BANDWIDTH", "RESOLUTION", "CODECS", "FRAME-RATE")
    incomplete = [uri for attrs, uri in master.variants if any(k not in attrs for k in required)]
    report.add(
        "master playlist",
        bool(master.variants) and not incomplete,
        f"{len(master.variants)} variants, {len(master.media)} alternative renditions"
        + (f"; missing attributes on {incomplete}" if incomplete else ""),
    )

    boundaries: dict[str, list[float]] = {}
    keyframes: dict[str, list[float]] = {}
    longest_frame = 1 / 60
    for attrs, uri in master.variants:
        label = uri.split("/")[0]
        playlist = _load_media(report, out / uri, label)
        if playlist is None:
            continue
        frame = 1 / float(attrs.get("FRAME-RATE", 25))
        longest_frame = max(longest_frame, frame)
        problems = check_segment_durations(playlist, expected, tolerance=frame + 1e-3)
        report.add(f"{label}: segment durations", not problems,
                   "; ".join(problems[:3]) or f"target {playlist.target_duration}s")  # fmt: skip
        if int(attrs.get("BANDWIDTH", 0)) < int(attrs.get("AVERAGE-BANDWIDTH", 0)):
            report.add(f"{label}: bandwidth", False, "BANDWIDTH < AVERAGE-BANDWIDTH")
        actual = video_codec_of(out / uri)
        declared = attrs.get("CODECS", "").split(",")[0]
        report.add(f"{label}: codecs", actual == declared,
                   f"declared {declared}, stream {actual}")  # fmt: skip
        boundaries[label] = playlist.boundaries()
        keyframes[label] = keyframe_times(out / uri)
        if gop:
            gaps = [b - a for a, b in zip(keyframes[label], keyframes[label][1:], strict=False)]
            off = [g for g in gaps if abs(g - gop) > frame + 1e-3]
            report.add(f"{label}: GOP", not off, f"{len(gaps) + 1} keyframes, "
                       + (f"irregular gaps {off[:3]}" if off else f"every {gop:g}s"))  # fmt: skip

    for media in master.media:
        if "URI" in media:
            label = media["URI"].split("/")[0]
            playlist = _load_media(report, out / media["URI"], label)
            if playlist is not None:
                # audio frames (1024 samples) do not divide segment times exactly
                problems = check_segment_durations(playlist, expected, tolerance=0.05)
                detail = "; ".join(problems[:3]) or f"target {playlist.target_duration}s"
                report.add(f"{label}: segment durations", not problems, detail)

    if len(boundaries) >= 1:
        problems = check_keyframe_alignment(boundaries, keyframes, tolerance=longest_frame / 2)
        segments = len(next(iter(boundaries.values())))
        detail = "; ".join(problems[:3]) or (
            f"{len(boundaries)} renditions share {segments} segment boundaries on keyframes"
        )
        report.add("keyframe alignment", not problems, detail)
    return report
