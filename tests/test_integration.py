"""End-to-end runs with the real ffmpeg/ffprobe on synthetic lavfi clips (skipped without them)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from abr_pack import tools
from abr_pack.cli import main
from abr_pack.encode import EncodeSettings
from abr_pack.loudness import LoudnessTarget, measure
from abr_pack.packager import PackageOptions, ThumbnailOptions, package
from abr_pack.playlist import parse_master_playlist, parse_media_playlist
from abr_pack.probe import probe
from abr_pack.validate import validate
from tests.conftest import requires_ffmpeg

pytestmark = [pytest.mark.ffmpeg, requires_ffmpeg]

FAST = dict(segment_seconds=2.0, gop_seconds=1.0, preset="ultrafast")


@pytest.fixture(scope="module")
def fmp4_package(clip_720p, tmp_path_factory) -> tuple[Path, dict]:
    out = tmp_path_factory.mktemp("fmp4") / "out"
    options = PackageOptions(
        settings=EncodeSettings(segment_format="fmp4", **FAST),
        loudness=LoudnessTarget(integrated=-23.0),
        thumbnails=ThumbnailOptions(interval=2.0, width=160, columns=2),
    )
    return out, package(clip_720p, out, options)


def test_probe_reads_the_synthetic_clip(clip_720p):
    info = probe(clip_720p)
    assert (info.video.width, info.video.height, info.video.fps) == (1280, 720, 30)
    assert info.audio.sample_rate == 48_000
    assert info.duration == pytest.approx(6.0, abs=0.1)


def test_fmp4_package_validates(fmp4_package):
    out, _ = fmp4_package
    report = validate(out)
    assert report.ok, report.render()
    names = {c.name for c in report.checks}
    assert "keyframe alignment" in names and "720p: codecs" in names


def test_master_playlist_attributes(fmp4_package):
    out, manifest = fmp4_package
    master = parse_master_playlist((out / "master.m3u8").read_text())
    resolutions = [attrs["RESOLUTION"] for attrs, _ in master.variants]
    assert resolutions == ["426x240", "640x360", "854x480", "1280x720"]  # ascending bandwidth
    for attrs, uri in master.variants:
        assert int(attrs["BANDWIDTH"]) >= int(attrs["AVERAGE-BANDWIDTH"]) > 0
        # CODECS comes from the encoded SPS, not from the requested profile: x264's ultrafast
        # preset drops CABAC / 8x8 transforms and signals Constrained Baseline (avc1.42c0..)
        # even with -profile:v high.
        assert attrs["CODECS"].startswith("avc1.") and attrs["CODECS"].endswith(",mp4a.40.2")
        assert attrs["FRAME-RATE"] == "30.000" and attrs["AUDIO"] == "aud"
        assert (out / uri).exists()
    (audio,) = master.media
    assert audio["TYPE"] == "AUDIO" and audio["URI"] == "audio/index.m3u8"
    assert manifest["renditions"][0]["level"] == "3.1"


def test_fmp4_segments_and_init(fmp4_package):
    out, _ = fmp4_package
    playlist = parse_media_playlist((out / "720p" / "index.m3u8").read_text())
    assert playlist.init_uri and (out / "720p" / playlist.init_uri).stat().st_size > 0
    assert [s.duration for s in playlist.segments] == pytest.approx([2.0, 2.0, 2.0], abs=0.01)
    assert all(s.uri.endswith(".m4s") for s in playlist.segments)


def test_loudness_is_normalised_to_target(fmp4_package):
    out, manifest = fmp4_package
    loud = manifest["loudness"]
    assert loud["input"]["integrated"] != pytest.approx(-23.0, abs=0.5)  # it really changed
    assert loud["output"]["integrated"] == pytest.approx(-23.0, abs=1.0)
    again = measure(out / "audio" / "index.m3u8")
    assert again.integrated == pytest.approx(-23.0, abs=1.0)


def test_thumbnails_sprite_and_vtt(fmp4_package):
    out, manifest = fmp4_package
    thumbs = manifest["thumbnails"]
    assert (thumbs["count"], thumbs["columns"], thumbs["rows"]) == (3, 2, 2)
    data = tools.ffprobe_json(["-show_entries", "stream=width,height", str(out / thumbs["sprite"])])
    stream = data["streams"][0]
    assert (stream["width"], stream["height"]) == (thumbs["sprite_width"], thumbs["sprite_height"])
    vtt = (out / thumbs["vtt"]).read_text()
    assert vtt.count("-->") == 3 and "sprite.jpg#xywh=0,90,160,90" in vtt


def test_manifest_records_the_job(fmp4_package):
    out, _ = fmp4_package
    on_disk = json.loads((out / "manifest.json").read_text())
    assert on_disk["settings"]["segment_format"] == "fmp4"
    assert on_disk["input"]["video"]["width"] == 1280
    assert on_disk["ffmpeg_command"][0].endswith("ffmpeg")
    assert {r["name"] for r in on_disk["renditions"]} == {"720p", "480p", "360p", "240p"}
    assert all(r["measured"]["segments"] == 3 for r in on_disk["renditions"])


def test_ts_package_validates(clip_720p, tmp_path):
    out = tmp_path / "ts"
    options = PackageOptions(
        settings=EncodeSettings(segment_format="ts", **FAST), max_renditions=2, thumbnails=None
    )
    manifest = package(clip_720p, out, options)
    assert [r["name"] for r in manifest["renditions"]] == ["720p", "480p"]
    playlist = parse_media_playlist((out / "480p" / "index.m3u8").read_text())
    assert playlist.init_uri is None and playlist.segments[0].uri.endswith(".ts")
    report = validate(out)
    assert report.ok, report.render()


def test_video_only_4x3_source(clip_4x3_silent_video, tmp_path):
    out = tmp_path / "noaudio"
    manifest = package(
        clip_4x3_silent_video, out,
        PackageOptions(settings=EncodeSettings(**FAST), thumbnails=None),
    )  # fmt: skip
    assert manifest["audio"] is None
    assert [(r["width"], r["height"]) for r in manifest["renditions"]] == [
        (640, 480), (480, 360), (320, 240),
    ]  # fmt: skip
    master = (out / "master.m3u8").read_text()
    assert "EXT-X-MEDIA" not in master and "mp4a" not in master
    assert validate(out).ok


def test_validate_catches_damage(fmp4_package, tmp_path):
    good, _ = fmp4_package
    broken = tmp_path / "broken"
    shutil.copytree(good, broken)
    (broken / "360p" / "seg_00001.m4s").unlink()
    playlist = broken / "480p" / "index.m3u8"
    playlist.write_text(playlist.read_text().replace("#EXTINF:2.000000,", "#EXTINF:3.000000,", 1))
    report = validate(broken)
    assert not report.ok
    failed = {c.name for c in report.checks if not c.ok}
    assert "360p: files" in failed
    assert "480p: segment durations" in failed
    assert "keyframe alignment" in failed


def test_refuses_to_overwrite_without_flag(clip_4x3_silent_video, tmp_path):
    out = tmp_path / "busy"
    out.mkdir()
    (out / "keep.txt").write_text("x")
    with pytest.raises(FileExistsError):
        package(clip_4x3_silent_video, out, PackageOptions(thumbnails=None))


def test_cli_package_validate_and_ladder(clip_720p, tmp_path, capsys):
    out = tmp_path / "cli"
    args = ["package", str(clip_720p), "-o", str(out), "--format", "ts", "--segment-seconds",
            "2", "--gop-seconds", "1", "--preset", "ultrafast", "--max-renditions", "2",
            "--validate"]  # fmt: skip
    assert main(args) == 0
    printed = capsys.readouterr().out
    assert "packaged 2 renditions" in printed and "checks passed" in printed

    assert main(["validate", str(out), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    assert main(["ladder", str(clip_720p), "--print-command"]) == 0
    printed = capsys.readouterr().out
    assert "720p" in printed and "split=4" in printed

    assert main(["probe", str(clip_720p)]) == 0
    assert json.loads(capsys.readouterr().out)["video"]["height"] == 720

    (out / "720p" / "index.m3u8").unlink()
    assert main(["validate", str(out)]) == 1
    assert main(["package", str(clip_720p), "-o", str(out)]) == 2  # not empty, no --overwrite
