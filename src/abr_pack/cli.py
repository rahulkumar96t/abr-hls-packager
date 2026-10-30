"""abr-pack: probe | ladder | package | validate."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from abr_pack.encode import EncodeSettings, build_encode_command
from abr_pack.ladder import select_ladder
from abr_pack.loudness import LoudnessTarget
from abr_pack.packager import PackageOptions, ThumbnailOptions, package
from abr_pack.probe import probe
from abr_pack.tools import FFmpegError, FFmpegNotFoundError
from abr_pack.validate import validate


def _settings(args: argparse.Namespace) -> EncodeSettings:
    return EncodeSettings(
        segment_format=args.format,
        segment_seconds=args.segment_seconds,
        gop_seconds=args.gop_seconds,
        preset=args.preset,
        audio_kbps=args.audio_kbps,
    )


def _cmd_probe(args: argparse.Namespace) -> int:
    print(json.dumps(probe(args.input).to_dict(), indent=2))
    return 0


def _cmd_ladder(args: argparse.Namespace) -> int:
    info = probe(args.input)
    if info.video is None:
        raise SystemExit("input has no video stream")
    settings = _settings(args)
    ladder = select_ladder(info.video, gop_seconds=settings.gop_seconds,
                           max_renditions=args.max_renditions)  # fmt: skip
    v = info.video
    print(f"source: {v.display_width}x{v.display_height} @ {float(v.fps):.3f} fps, "
          f"{(v.bit_rate or 0) / 1000:.0f} kbps, {info.duration:.2f}s")  # fmt: skip
    print(f"{'name':<7}{'size':>11}{'fps':>8}{'kbps':>7}{'maxrate':>9}{'level':>7}{'gop':>6}")
    for r in ladder:
        size = f"{r.width}x{r.height}"
        gop = r.gop_frames(settings.gop_seconds)
        print(f"{r.name:<7}{size:>11}{float(r.fps):>8.3f}{r.video_kbps:>7}"
              f"{r.maxrate_kbps:>9}{r.level:>7}{gop:>6}")  # fmt: skip
    if args.print_command:
        argv = build_encode_command(args.input, args.output, ladder, settings,
                                    with_audio=info.audio is not None)  # fmt: skip
        print()
        print(" ".join(f"'{a}'" if any(c in a for c in " ;[]()*,") else a for a in argv))
    return 0


def _cmd_package(args: argparse.Namespace) -> int:
    options = PackageOptions(
        settings=_settings(args),
        max_renditions=args.max_renditions,
        loudness=LoudnessTarget(args.target_lufs, args.true_peak, args.lra)
        if args.loudnorm
        else None,
        thumbnails=ThumbnailOptions(args.thumb_interval, args.thumb_width, args.thumb_columns)
        if not args.no_thumbnails
        else None,
        overwrite=args.overwrite,
    )
    manifest = package(args.input, args.output, options)
    print(f"packaged {len(manifest['renditions'])} renditions into {args.output}/ "
          f"in {manifest['timings']['total_seconds']:.1f}s")  # fmt: skip
    for r in manifest["renditions"]:
        print(f"  {r['name']:<6} {r['width']}x{r['height']}  BANDWIDTH={r['bandwidth']}  "
              f"AVERAGE-BANDWIDTH={r['average_bandwidth']}  {r['codecs']}")  # fmt: skip
    if manifest.get("loudness", {}) and "output" in manifest["loudness"]:
        lo = manifest["loudness"]
        print(f"  loudness {lo['input']['integrated']:.1f} -> {lo['output']['integrated']:.1f} "
              f"LUFS (target {lo['target']['integrated']:.1f})")  # fmt: skip
    if args.validate:
        return _report(validate(args.output), as_json=False)
    return 0


def _report(report, as_json: bool) -> int:
    print(json.dumps(report.to_dict(), indent=2) if as_json else report.render())
    return 0 if report.ok else 1


def _cmd_validate(args: argparse.Namespace) -> int:
    return _report(validate(args.output), args.json)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="abr-pack", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="print ffprobe facts as JSON")
    p.add_argument("input", type=Path)
    p.set_defaults(func=_cmd_probe)

    def encode_options(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--format", choices=["fmp4", "ts"], default="fmp4")
        sp.add_argument("--segment-seconds", type=float, default=4.0)
        sp.add_argument("--gop-seconds", type=float, default=2.0)
        sp.add_argument("--preset", default="veryfast", help="x264 preset")
        sp.add_argument("--audio-kbps", type=int, default=128)
        sp.add_argument("--max-renditions", type=int)

    lad = sub.add_parser("ladder", help="show the ladder (and command) without encoding")
    lad.add_argument("input", type=Path)
    lad.add_argument("-o", "--output", type=Path, default=Path("out"))
    lad.add_argument("--print-command", action="store_true")
    encode_options(lad)
    lad.set_defaults(func=_cmd_ladder)

    pkg = sub.add_parser("package", help="encode and package to HLS")
    pkg.add_argument("input", type=Path)
    pkg.add_argument("-o", "--output", type=Path, required=True)
    encode_options(pkg)
    pkg.add_argument("--loudnorm", action="store_true", help="two-pass EBU R128 normalisation")
    pkg.add_argument("--target-lufs", type=float, default=-23.0)
    pkg.add_argument("--true-peak", type=float, default=-1.0)
    pkg.add_argument("--lra", type=float, default=7.0)
    pkg.add_argument("--no-thumbnails", action="store_true")
    pkg.add_argument("--thumb-interval", type=float, default=2.0)
    pkg.add_argument("--thumb-width", type=int, default=160)
    pkg.add_argument("--thumb-columns", type=int, default=10)
    pkg.add_argument("--overwrite", action="store_true")
    pkg.add_argument("--validate", action="store_true", help="run validate afterwards")
    pkg.set_defaults(func=_cmd_package)

    val = sub.add_parser("validate", help="check segment durations and keyframe alignment")
    val.add_argument("output", type=Path)
    val.add_argument("--json", action="store_true")
    val.set_defaults(func=_cmd_validate)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (FFmpegError, FFmpegNotFoundError, FileExistsError, ValueError) as exc:
        print(f"abr-pack: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
