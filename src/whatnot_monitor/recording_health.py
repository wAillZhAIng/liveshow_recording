from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass
class _Channel:
    size: int
    changed_at: float


def recording_paths(recorder) -> dict[str, Path]:
    paths = {}
    video = getattr(recorder, 'video_path', None)
    audio = getattr(recorder, 'audio_path', getattr(recorder, 'output_path', None))
    if video:
        paths['视频'] = Path(video)
    if audio:
        paths['音频'] = Path(audio)
    return paths


def empty_recording_channels(recorder) -> list[str]:
    """Call after stop/drain, before declaring capture successful."""
    paths = recording_paths(recorder)
    if not paths:
        return ['未配置输出文件']
    return [name for name, path in paths.items() if not path.is_file() or path.stat().st_size == 0]


class RecordingHealth:
    """Observe flushed per-channel byte growth; do not infer health from recorder.start().

    The grace starts at first observation, rather than recorder creation: a blocked
    owner thread must not manufacture a timeout without getting a chance to see
    writes. All files are preserved when an individual channel stalls.
    """
    def __init__(self, *, timeout_seconds: float = 180, clock: Callable[[], float] = time.monotonic):
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self._sessions: dict[str, dict[str, _Channel]] = {}

    def forget(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def check(self, session_id: str, recorder) -> str | None:
        paths = recording_paths(recorder)
        if not paths:
            return '录制器没有配置输出文件；已保留现有媒体。'
        now = self.clock()
        channels = self._sessions.setdefault(session_id, {})
        stalled = []
        for name, path in paths.items():
            try:
                size = path.stat().st_size if path.is_file() else 0
            except OSError as exc:
                return f'无法检查{name}录制文件 {path}：{exc}；已保留现有媒体。'
            previous = channels.get(name)
            if previous is None or size > previous.size:
                channels[name] = _Channel(size, now)
            elif size < previous.size:
                return f'{name}录制文件大小异常减少：{path}；已保留现有媒体。'
            elif now - previous.changed_at >= self.timeout_seconds:
                stalled.append(f'{name}（{path}，{size} 字节）')
        if stalled:
            return (f'录制文件连续至少 {self.timeout_seconds:g} 秒没有增长：'
                    + '、'.join(stalled) + '；录制可能停滞，继续观察，缺失内容无法补录。')
        return None
