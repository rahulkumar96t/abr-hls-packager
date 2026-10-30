"""EBU R128 loudness normalisation with FFmpeg's loudnorm filter, in two passes.

Pass 1 measures integrated loudness, true peak, loudness range and threshold. Pass 2 feeds
those measurements back so loudnorm can apply a single linear gain (``linear=true``) instead of
dynamic compression, which keeps the programme's dynamics intact whenever the true-peak limit
allows it.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path

from abr_pack.tools import run


@dataclass(frozen=True)
class LoudnessTarget:
    integrated: float = -23.0  # LUFS, EBU R128
    true_peak: float = -1.0  # dBTP
    lra: float = 7.0  # LU

    def filter_args(self) -> str:
        return f"I={self.integrated}:TP={self.true_peak}:LRA={self.lra}"


@dataclass(frozen=True)
class LoudnessMeasurement:
    integrated: float
    true_peak: float
    lra: float
    threshold: float
    target_offset: float

    def to_dict(self) -> dict[str, float]:
        return asdict(self)

    @property
    def measurable(self) -> bool:
        """Silence (or near silence) cannot be normalised meaningfully."""
        return math.isfinite(self.integrated) and self.integrated > -70.0


def parse_loudnorm_json(stderr: str) -> LoudnessMeasurement:
    """loudnorm prints its JSON block last on stderr; take the last {...} in the output."""
    end = stderr.rfind("}")
    start = stderr.rfind("{", 0, end)
    if start < 0 or end < 0:
        raise ValueError("no loudnorm JSON found in ffmpeg output")
    data = json.loads(stderr[start : end + 1])
    return LoudnessMeasurement(
        integrated=float(data["input_i"]),
        true_peak=float(data["input_tp"]),
        lra=float(data["input_lra"]),
        threshold=float(data["input_thresh"]),
        target_offset=float(data["target_offset"]),
    )


def measure_command(path: Path | str, target: LoudnessTarget, stream: str = "0:a:0") -> list[str]:
    return [
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", str(path),
        "-map", stream, "-af", f"loudnorm={target.filter_args()}:print_format=json",
        "-f", "null", "-",
    ]  # fmt: skip


def measure(path: Path | str, target: LoudnessTarget | None = None) -> LoudnessMeasurement:
    target = target or LoudnessTarget()
    return parse_loudnorm_json(run(measure_command(path, target)).stderr)


def second_pass_filter(m: LoudnessMeasurement, target: LoudnessTarget, sample_rate: int) -> str:
    """Filter chain for pass 2; loudnorm works at 192 kHz, so resample back afterwards."""
    return (
        f"loudnorm={target.filter_args()}"
        f":measured_I={m.integrated}:measured_TP={m.true_peak}"
        f":measured_LRA={m.lra}:measured_thresh={m.threshold}"
        f":offset={m.target_offset}:linear=true:print_format=summary"
        f",aresample={sample_rate}"
    )
