from __future__ import annotations

from fractions import Fraction

import pytest

from abr_pack.ladder import DEFAULT_LADDER, Rung, even, h264_level, select_ladder
from tests.conftest import video


def dims(ladder):
    return [(r.width, r.height) for r in ladder]


def test_1080p_source_gets_the_full_ladder():
    ladder = select_ladder(video(1920, 1080))
    assert [r.name for r in ladder] == ["1080p", "720p", "480p", "360p", "240p"]
    assert dims(ladder) == [(1920, 1080), (1280, 720), (854, 480), (640, 360), (426, 240)]


def test_never_upscales():
    ladder = select_ladder(video(1280, 720))
    assert max(r.height for r in ladder) == 720
    assert all(r.width <= 1280 for r in ladder)


def test_between_rungs_uses_the_next_lower_rung():
    assert select_ladder(video(1600, 900))[0].name == "720p"


def test_all_dimensions_are_even_and_keep_aspect_ratio():
    for w, h in [(1920, 1080), (1440, 1080), (1918, 1080), (720, 576), (1998, 1080), (854, 480)]:
        for r in select_ladder(video(w, h)):
            assert r.width % 2 == 0 and r.height % 2 == 0
            assert abs(r.width / r.height - w / h) < 0.02, (w, h, r)


def test_4_3_source():
    assert dims(select_ladder(video(640, 480))) == [(640, 480), (480, 360), (320, 240)]


def test_anamorphic_source_uses_display_aspect():
    # 720x576 PAL with 16:15 SAR -> 768x576 display (4:3); with 64:45 SAR -> 1024x576 (16:9)
    wide = select_ladder(video(720, 576, sar=Fraction(64, 45)))
    assert dims(wide)[0] == (854, 480)


def test_portrait_source_uses_short_side():
    ladder = select_ladder(video(1080, 1920))
    assert dims(ladder)[:2] == [(1080, 1920), (720, 1280)]


def test_rotated_phone_video():
    ladder = select_ladder(video(1920, 1080, rotation=90))  # coded landscape, displayed portrait
    assert dims(ladder)[0] == (1080, 1920)


def test_tiny_source_gets_one_rendition_at_its_own_size():
    ladder = select_ladder(video(320, 180))
    assert dims(ladder) == [(320, 180)]
    assert dims(select_ladder(video(321, 181))) == [(320, 180)]  # odd sizes rounded down


def test_high_frame_rate_low_rungs_run_at_half_rate():
    ladder = select_ladder(video(1920, 1080, fps=60))
    fps = {r.name: r.fps for r in ladder}
    assert fps["1080p"] == 60 and fps["720p"] == 60 and fps["480p"] == 30 and fps["240p"] == 30
    assert ladder[0].video_kbps > DEFAULT_LADDER[0].video_kbps  # more frames, more bits


def test_ntsc_rates_are_not_halved_when_gop_would_break():
    # 59.94 / 2 = 29.97 fps gives 59.94 frames per 2 s GOP: not an integer, keep full rate
    ladder = select_ladder(video(1280, 720, fps=Fraction(60000, 1001)), gop_seconds=2)
    assert all(r.fps == Fraction(60000, 1001) for r in ladder)


def test_bitrate_is_capped_by_the_source():
    ladder = select_ladder(video(1920, 1080, kbps=2000))
    assert all(r.video_kbps <= 2000 for r in ladder)
    assert ladder[0].maxrate_kbps == round(ladder[0].video_kbps * 1.1)


def test_max_renditions_keeps_the_top():
    assert [r.name for r in select_ladder(video(1920, 1080), max_renditions=2)] == ["1080p", "720p"]


def test_custom_ladder():
    ladder = select_ladder(video(1280, 720), rungs=(Rung(720, 3000), Rung(360, 700)))
    assert [r.name for r in ladder] == ["720p", "360p"]


def test_gop_frames():
    r = select_ladder(video(1280, 720, fps=25))[0]
    assert r.gop_frames(2.0) == 50


@pytest.mark.parametrize(
    ("w", "h", "fps", "maxrate", "level"),
    [
        (640, 360, 30, 880, "3.0"),
        (854, 480, 30, 1540, "3.1"),  # 1620 MBs/frame x 30 > level 3.0 MB rate
        (1280, 720, 30, 3080, "3.1"),
        (1280, 720, 60, 4700, "3.2"),
        (1920, 1080, 30, 5500, "4.0"),
        (1920, 1080, 60, 8000, "4.2"),
        (1920, 1080, 30, 30000, "4.1"),  # bitrate pushes it past 4.0
        (3840, 2160, 30, 20000, "5.1"),
    ],
)
def test_h264_level(w, h, fps, maxrate, level):
    assert h264_level(w, h, fps, maxrate, maxrate * 2) == level


def test_h264_level_rejects_impossible_streams():
    with pytest.raises(ValueError):
        h264_level(7680, 4320, 60, 100_000, 100_000)


def test_even():
    assert [even(x) for x in (853.33, 853, 1, 2.9)] == [854, 852, 2, 2]
