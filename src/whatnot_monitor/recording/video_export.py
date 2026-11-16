"""Create a playback-compatible copy of a finalized browser recording."""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import imageio_ffmpeg


def export_mp4(source: Path) -> Path:
    if source.suffix.lower() != ".webm" or not source.is_file() or source.stat().st_size == 0:
        raise ValueError("MP4 转换需要非空的 WebM 录像。")
    destination = source.with_suffix(".mp4")
    if destination.is_symlink():
        raise ValueError("MP4 输出路径为符号链接。")
    if destination.is_file() and destination.stat().st_size > 0:
        return destination
    fd, name = tempfile.mkstemp(prefix=f".{source.stem}-", suffix=".mp4", dir=source.parent)
    os.close(fd)
    temporary = Path(name)
    try:
        command = [
            imageio_ffmpeg.get_ffmpeg_exe(), "-nostdin", "-y", "-v", "error",
            "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22", "-threads", "2",
            "-vf", "pad=ceil(iw/2)*2:ceil(ih/2)*2", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temporary),
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=4 * 3600)
        if result.returncode or temporary.stat().st_size == 0:
            raise RuntimeError(result.stderr.strip()[-700:] or "FFmpeg 未生成有效的 MP4 文件。")
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)
