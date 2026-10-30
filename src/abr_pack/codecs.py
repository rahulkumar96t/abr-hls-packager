"""RFC 6381 codec strings for the master playlist, derived from the encoded streams."""

from __future__ import annotations

from pathlib import Path

from abr_pack.tools import ffprobe_json, run

AAC_OBJECT_TYPES = {"LC": 2, "HE-AAC": 5, "HE-AACv2": 29}


def find_h264_sps(annexb: bytes) -> tuple[int, int, int]:
    """Return (profile_idc, constraint flags, level_idc) from the first SPS in an Annex B stream.

    The three bytes after the SPS NAL header are exactly what ``avc1.PPCCLL`` encodes.
    """
    i = 0
    while True:
        i = annexb.find(b"\x00\x00\x01", i)
        if i < 0 or i + 6 >= len(annexb):
            raise ValueError("no H.264 sequence parameter set found")
        header = annexb[i + 3]
        if header & 0x1F == 7:  # nal_unit_type 7 = SPS
            return annexb[i + 4], annexb[i + 5], annexb[i + 6]
        i += 3


def avc1_codec(profile_idc: int, constraints: int, level_idc: int) -> str:
    return f"avc1.{profile_idc:02x}{constraints:02x}{level_idc:02x}"


def aac_codec(profile: str | None) -> str:
    return f"mp4a.40.{AAC_OBJECT_TYPES.get(profile or 'LC', 2)}"


def video_codec_of(playlist: Path) -> str:
    """Read the SPS of a rendition (through its playlist) and build the avc1 string."""
    proc = run(
        ["ffmpeg", "-v", "error", "-i", str(playlist), "-map", "0:v:0", "-c", "copy",
         "-bsf:v", "h264_mp4toannexb", "-frames:v", "1", "-f", "h264", "pipe:1"],
        binary_output=True,
    )  # fmt: skip
    return avc1_codec(*find_h264_sps(proc.stdout))


def audio_codec_of(playlist: Path) -> str:
    data = ffprobe_json(
        ["-select_streams", "a:0", "-show_entries", "stream=profile", str(playlist)]
    )
    streams = data.get("streams") or [{}]
    return aac_codec(streams[0].get("profile"))
