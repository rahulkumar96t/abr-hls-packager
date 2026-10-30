"""Pure-function tests: codecs, encode command, loudness parsing, thumbnails, probe parsing,
validation checks. None of these need ffmpeg."""

from __future__ import annotations

from fractions import Fraction

import pytest

from abr_pack.codecs import aac_codec, avc1_codec, find_h264_sps
from abr_pack.encode import EncodeSettings, build_encode_command, filter_graph, var_stream_map
from abr_pack.ladder import select_ladder
from abr_pack.loudness import LoudnessTarget, parse_loudnorm_json, second_pass_filter
from abr_pack.playlist import parse_media_playlist
from abr_pack.probe import parse_probe
from abr_pack.thumbnails import plan_sprite, render_webvtt, vtt_timestamp
from abr_pack.validate import check_keyframe_alignment, check_segment_durations
from tests.conftest import video

# ---- codecs -------------------------------------------------------------------------------


def test_sps_is_found_after_other_nal_units():
    sei = b"\x00\x00\x00\x01\x06\x05\xff\xff"
    sps = b"\x00\x00\x00\x01\x67\x64\x00\x1f\xac\xd9"
    assert find_h264_sps(sei + sps) == (0x64, 0x00, 0x1F)
    assert avc1_codec(0x64, 0x00, 0x1F) == "avc1.64001f"
    assert avc1_codec(0x42, 0xE0, 0x1E) == "avc1.42e01e"  # constrained baseline 3.0


def test_missing_sps_is_an_error():
    with pytest.raises(ValueError):
        find_h264_sps(b"\x00\x00\x01\x65\x88\x84")


def test_aac_codec_strings():
    assert aac_codec("LC") == "mp4a.40.2"
    assert aac_codec("HE-AAC") == "mp4a.40.5"
    assert aac_codec(None) == "mp4a.40.2"


# ---- encode command --------------------------------------------------------------------------


def test_settings_require_whole_gops_per_segment():
    EncodeSettings(segment_seconds=6, gop_seconds=2)
    with pytest.raises(ValueError):
        EncodeSettings(segment_seconds=5, gop_seconds=2)
    with pytest.raises(ValueError):
        EncodeSettings(segment_format="dash")  # type: ignore[arg-type]


def test_filter_graph_splits_once_and_scales_each_rendition():
    ladder = select_ladder(video(1920, 1080, fps=60))[:3]
    graph = filter_graph(ladder)
    assert graph.startswith("[0:v]split=3[s0][s1][s2];")
    assert "[s0]scale=1920:1080:flags=bicubic,setsar=1,fps=60/1,format=yuv420p[v0]" in graph
    assert "[s2]scale=854:480:flags=bicubic,setsar=1,fps=30/1,format=yuv420p[v2]" in graph


def test_encode_command_aligns_gops_and_names_streams(tmp_path):
    ladder = select_ladder(video(1280, 720))[:2]
    settings = EncodeSettings(segment_seconds=4, gop_seconds=2)
    argv = build_encode_command("in.mp4", tmp_path, ladder, settings, with_audio=True,
                                audio_filter="loudnorm=I=-23")  # fmt: skip

    def value(flag):
        return argv[argv.index(flag) + 1]

    assert value("-g:v:0") == value("-keyint_min:v:0") == "60"
    assert value("-force_key_frames:v:1") == "expr:gte(t,n_forced*2)"
    assert value("-sc_threshold") == "0"
    assert value("-b:v:0") == "2800k" and value("-level:v:0") == "3.1"
    assert value("-hls_time") == "4" and value("-hls_segment_type") == "fmp4"
    assert value("-af") == "loudnorm=I=-23"
    assert value("-var_stream_map") == (
        "v:0,agroup:aud,name:720p v:1,agroup:aud,name:480p a:0,agroup:aud,name:audio"
    )
    assert argv[-1] == str(tmp_path / "%v" / "index.m3u8")
    assert value("-hls_segment_filename").endswith("seg_%05d.m4s")


def test_ts_command_has_no_init_segment_and_no_audio_without_audio(tmp_path):
    ladder = select_ladder(video(640, 480))
    argv = build_encode_command("in.mp4", tmp_path, ladder, EncodeSettings(segment_format="ts"),
                                with_audio=False)  # fmt: skip
    assert "-hls_fmp4_init_filename" not in argv
    assert "0:a:0" not in argv and "agroup" not in var_stream_map(ladder, False)
    assert argv[argv.index("-hls_segment_type") + 1] == "mpegts"
    assert argv[argv.index("-hls_segment_filename") + 1].endswith(".ts")


# ---- loudness -----------------------------------------------------------------------------

LOUDNORM_STDERR = """[Parsed_loudnorm_0 @ 0x1]
{
	"input_i" : "-21.80",
	"input_tp" : "-16.08",
	"input_lra" : "0.10",
	"input_thresh" : "-31.80",
	"output_i" : "-23.00",
	"output_tp" : "-17.28",
	"output_lra" : "0.10",
	"output_thresh" : "-33.00",
	"normalization_type" : "dynamic",
	"target_offset" : "-0.00"
}
[out#0/null @ 0x2] video:0KiB audio:2250KiB
"""


def test_parse_loudnorm_json_and_second_pass():
    m = parse_loudnorm_json(LOUDNORM_STDERR)
    assert (m.integrated, m.true_peak, m.lra, m.threshold) == (-21.8, -16.08, 0.1, -31.8)
    assert m.measurable
    f = second_pass_filter(m, LoudnessTarget(), 48_000)
    assert f.startswith("loudnorm=I=-23.0:TP=-1.0:LRA=7.0:measured_I=-21.8:measured_TP=-16.08")
    assert ":linear=true" in f and f.endswith(",aresample=48000")


def test_silence_is_not_measurable():
    silent = LOUDNORM_STDERR.replace('"-21.80"', '"-inf"')
    assert not parse_loudnorm_json(silent).measurable
    with pytest.raises(ValueError):
        parse_loudnorm_json("no json here")


# ---- thumbnails ---------------------------------------------------------------------------


def test_sprite_layout_and_webvtt():
    layout = plan_sprite(duration=25.0, interval=2.0, tile_width=160, aspect=Fraction(16, 9),
                         columns=5)  # fmt: skip
    assert (layout.count, layout.columns, layout.rows) == (13, 5, 3)
    assert (layout.tile_width, layout.tile_height) == (160, 90)
    assert (layout.width, layout.height) == (800, 270)
    vtt = render_webvtt(layout, 25.0, "sprite.jpg")
    assert vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:02.000\nsprite.jpg#xywh=0,0,160,90")
    assert "00:00:10.000 --> 00:00:12.000\nsprite.jpg#xywh=0,90,160,90" in vtt  # 6th tile
    assert vtt.rstrip().endswith("00:00:24.000 --> 00:00:25.000\nsprite.jpg#xywh=320,180,160,90")


def test_portrait_thumbnails_and_timestamps():
    layout = plan_sprite(5.0, 2.0, 90, Fraction(9, 16))
    assert (layout.tile_width, layout.tile_height, layout.count) == (90, 160, 3)
    assert vtt_timestamp(3725.5) == "01:02:05.500"


# ---- probe parsing ------------------------------------------------------------------------


def test_parse_probe_applies_sar_and_rotation():
    data = {
        "format": {"duration": "12.5", "format_name": "mov,mp4"},
        "streams": [
            {"codec_type": "video", "codec_name": "h264", "width": 1920, "height": 1080,
             "sample_aspect_ratio": "1:1", "avg_frame_rate": "30000/1001", "bit_rate": "8000000",
             "side_data_list": [{"rotation": -90}]},
            {"codec_type": "audio", "codec_name": "aac", "channels": 2, "sample_rate": "44100",
             "profile": "LC"},
        ],
    }  # fmt: skip
    info = parse_probe(data, "phone.mp4")
    assert info.video.rotation == 270 and info.video.portrait
    assert (info.video.display_width, info.video.display_height) == (1080, 1920)
    assert info.video.fps == Fraction(30000, 1001)
    assert info.audio.sample_rate == 44100 and info.duration == 12.5


def test_parse_probe_audio_only_and_cover_art():
    data = {
        "format": {"duration": "3"},
        "streams": [
            {"codec_type": "video", "codec_name": "mjpeg", "width": 600, "height": 600,
             "disposition": {"attached_pic": 1}},
            {"codec_type": "audio", "codec_name": "mp3", "channels": 1, "sample_rate": "44100"},
        ],
    }  # fmt: skip
    info = parse_probe(data)
    assert info.video is None and info.audio.channels == 1


# ---- validation checks --------------------------------------------------------------------


def _playlist(durations, target=4):
    body = "".join(f"#EXTINF:{d:.6f},\nseg_{i}.ts\n" for i, d in enumerate(durations))
    return parse_media_playlist(f"#EXTM3U\n#EXT-X-TARGETDURATION:{target}\n{body}#EXT-X-ENDLIST\n")


def test_segment_duration_checks():
    assert check_segment_durations(_playlist([4.0, 4.0, 1.2]), 4.0, 0.04) == []
    problems = check_segment_durations(_playlist([4.0, 5.0, 4.0]), 4.0, 0.04)
    assert any("> target 4s" in p for p in problems)  # 5 s rounds above TARGETDURATION
    assert any("segment 1 lasts 5.000s, expected 4s" in p for p in problems)
    assert check_segment_durations(_playlist([3.0, 4.0]), 4.0, 0.04) != []


def test_keyframe_alignment_checks():
    boundaries = {"720p": [0.0, 4.0, 8.0], "360p": [0.0, 4.0, 8.0]}
    keyframes = {"720p": [0.0, 2.0, 4.0, 6.0, 8.0], "360p": [0.0, 2.0, 4.0, 6.0, 8.0]}
    assert check_keyframe_alignment(boundaries, keyframes, tolerance=0.02) == []

    shifted = {**boundaries, "360p": [0.0, 4.2, 8.0]}
    assert any("differ" in p for p in check_keyframe_alignment(shifted, keyframes, 0.02))

    no_key = {**keyframes, "360p": [0.0, 3.0, 6.0]}
    assert any("no keyframe at segment start 4.000s" in p
               for p in check_keyframe_alignment(boundaries, no_key, 0.02))  # fmt: skip

    fewer = {**boundaries, "360p": [0.0, 8.0]}
    assert any("segments" in p for p in check_keyframe_alignment(fewer, keyframes, 0.02))
