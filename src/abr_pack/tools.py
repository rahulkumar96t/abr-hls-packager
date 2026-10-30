"""Locating and running the ffmpeg / ffprobe binaries."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from functools import cache
from typing import Any


class FFmpegNotFoundError(RuntimeError):
    pass


class FFmpegError(RuntimeError):
    """A tool exited non-zero; keeps the command and the tail of stderr for diagnosis."""

    def __init__(self, argv: Sequence[str], returncode: int, stderr: str) -> None:
        tail = "\n".join(stderr.strip().splitlines()[-15:])
        super().__init__(f"{argv[0]} exited with {returncode}:\n{tail}")
        self.argv = list(argv)
        self.returncode = returncode
        self.stderr = stderr


def _locate(name: str, env: str) -> str:
    override = os.environ.get(env)
    candidate = shutil.which(override) if override else shutil.which(name)
    if not candidate:
        where = f"{env}={override!r} is not an executable" if override else f"{name} not on PATH"
        raise FFmpegNotFoundError(f"{where}: install FFmpeg or set {env}")
    return candidate


def ffmpeg_bin() -> str:
    return _locate("ffmpeg", "ABR_PACK_FFMPEG")


def ffprobe_bin() -> str:
    return _locate("ffprobe", "ABR_PACK_FFPROBE")


def available() -> bool:
    try:
        ffmpeg_bin()
        ffprobe_bin()
    except FFmpegNotFoundError:
        return False
    return True


def resolve(argv: Sequence[str]) -> list[str]:
    """Command builders emit a bare ``ffmpeg`` / ``ffprobe``; resolve it only when running, so
    building (and unit-testing) commands never needs the binaries."""
    argv = list(argv)
    if argv and argv[0] == "ffmpeg":
        argv[0] = ffmpeg_bin()
    elif argv and argv[0] == "ffprobe":
        argv[0] = ffprobe_bin()
    return argv


def run(argv: Sequence[str], *, binary_output: bool = False) -> subprocess.CompletedProcess:
    """Run a tool; raise :class:`FFmpegError` with stderr on failure."""
    argv = resolve(argv)
    proc = subprocess.run(argv, capture_output=True, check=False, text=not binary_output)
    if proc.returncode != 0:
        stderr = (
            proc.stderr if isinstance(proc.stderr, str) else proc.stderr.decode(errors="replace")
        )
        raise FFmpegError(argv, proc.returncode, stderr)
    return proc


def ffprobe_json(args: Sequence[str]) -> dict[str, Any]:
    proc = run(["ffprobe", "-v", "error", "-print_format", "json", *args])
    return json.loads(proc.stdout or "{}")


@cache
def ffmpeg_version() -> str:
    first = run(["ffmpeg", "-version"]).stdout.splitlines()[0]
    return first.removeprefix("ffmpeg version ").split(" Copyright")[0]
