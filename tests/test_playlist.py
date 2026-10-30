from __future__ import annotations

import pytest

from abr_pack.playlist import (
    AudioRendition,
    VariantStream,
    parse_attributes,
    parse_master_playlist,
    parse_media_playlist,
    render_master,
    segment_bitrates,
)

MEDIA = """#EXTM3U
#EXT-X-VERSION:7
#EXT-X-TARGETDURATION:4
#EXT-X-MEDIA-SEQUENCE:0
#EXT-X-PLAYLIST-TYPE:VOD
#EXT-X-INDEPENDENT-SEGMENTS
#EXT-X-MAP:URI="init_0.mp4"
#EXTINF:4.000000,
seg_00000.m4s
#EXTINF:4.000000,
seg_00001.m4s
#EXTINF:1.500000,
seg_00002.m4s
#EXT-X-ENDLIST
"""


def test_parse_media_playlist():
    p = parse_media_playlist(MEDIA)
    assert (p.version, p.target_duration, p.init_uri, p.endlist) == (7, 4, "init_0.mp4", True)
    assert p.playlist_type == "VOD" and p.independent_segments
    assert [s.duration for s in p.segments] == [4.0, 4.0, 1.5]
    assert p.boundaries() == [0.0, 4.0, 8.0]
    assert p.duration == 9.5


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "#EXT-X-VERSION:3\n",  # no #EXTM3U header
        "#EXTM3U\n#EXTINF:4,\nseg.ts\n",  # no target duration
        "#EXTM3U\n#EXT-X-TARGETDURATION:4\nseg.ts\n",  # segment without EXTINF
    ],
)
def test_parse_rejects_broken_playlists(bad):
    with pytest.raises(ValueError):
        parse_media_playlist(bad)


def test_parse_attributes_handles_quoted_commas():
    attrs = parse_attributes('BANDWIDTH=1000,CODECS="avc1.64001f,mp4a.40.2",RESOLUTION=1280x720')
    assert attrs == {
        "BANDWIDTH": "1000",
        "CODECS": "avc1.64001f,mp4a.40.2",
        "RESOLUTION": "1280x720",
    }


def test_segment_bitrates_peak_and_average():
    peak, average = segment_bitrates([500_000, 250_000, 100_000], [4.0, 4.0, 1.0])
    assert peak == 1_000_000  # 500 kB in 4 s = 1 Mbit/s ; last: 100 kB in 1 s = 0.8 Mbit/s
    assert average == 755_556  # 850 kB * 8 / 9 s, rounded up
    with pytest.raises(ValueError):
        segment_bitrates([1], [1.0, 2.0])


def _variants():
    return [
        VariantStream("720p/index.m3u8", 3_100_000, 2_900_000, 1280, 720,
                      "avc1.64001f,mp4a.40.2", 30.0, "aud"),
        VariantStream("240p/index.m3u8", 560_000, 510_000, 426, 240,
                      "avc1.64001e,mp4a.40.2", 29.97003, "aud"),
    ]  # fmt: skip


def test_render_master_attributes_and_order():
    text = render_master(_variants(), [AudioRendition("aud", "Main", "audio/index.m3u8", 2)])
    lines = text.splitlines()
    assert lines[:3] == ["#EXTM3U", "#EXT-X-VERSION:6", "#EXT-X-INDEPENDENT-SEGMENTS"]
    assert (
        '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="aud",NAME="Main",DEFAULT=YES,AUTOSELECT=YES,'
        'CHANNELS="2",URI="audio/index.m3u8"'
    ) in lines
    streams = [ln for ln in lines if ln.startswith("#EXT-X-STREAM-INF")]
    assert streams[0] == (
        "#EXT-X-STREAM-INF:BANDWIDTH=560000,AVERAGE-BANDWIDTH=510000,RESOLUTION=426x240,"
        'CODECS="avc1.64001e,mp4a.40.2",FRAME-RATE=29.970,AUDIO="aud"'
    )
    assert lines[lines.index(streams[0]) + 1] == "240p/index.m3u8"
    assert "BANDWIDTH=3100000" in streams[1]  # ascending bandwidth
    assert text.endswith("\n")


def test_render_master_without_audio_and_round_trip():
    variant = VariantStream("720p/index.m3u8", 3_000_000, 2_800_000, 1280, 720, "avc1.64001f", 25.0)
    text = render_master([variant])
    assert "EXT-X-MEDIA" not in text and "AUDIO=" not in text
    master = parse_master_playlist(text)
    attrs, uri = master.variants[0]
    assert uri == "720p/index.m3u8"
    assert attrs["FRAME-RATE"] == "25.000" and attrs["CODECS"] == "avc1.64001f"
    assert master.independent_segments
