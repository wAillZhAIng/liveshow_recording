"""Delete only registered, confined session artifacts; never sweep a directory."""
from __future__ import annotations

from pathlib import Path

from .config import Settings
from .models import LiveSession


def session_artifacts(session: LiveSession, settings: Settings) -> set[Path]:
    media_roots = [Path(value).resolve() for value in (
        settings.recordings_dir, settings.video_dir, settings.audio_dir
    )]
    transcript_root = Path(settings.transcripts_dir).resolve()
    paths: set[Path] = set()

    def include(raw, roots, suffixes):
        if not raw:
            return
        original = Path(raw)
        path = original.resolve()
        if original.is_symlink() or not any(
            path != root and path.is_relative_to(root) for root in roots
        ):
            raise ValueError(f"拒绝删除配置目录以外或符号链接文件：{original}")
        if path.suffix.lower() not in suffixes or (path.exists() and not path.is_file()):
            raise ValueError(f"拒绝删除非预期文件：{original}")
        paths.add(path)

    media_suffixes = {".webm", ".wav", ".ogg", ".mp4"}
    for raw in (session.video_path, session.audio_path, session.recording_path):
        include(raw, media_roots, media_suffixes)
    if session.video_path and Path(session.video_path).suffix.lower() == ".webm":
        mp4 = Path(session.video_path).with_suffix(".mp4")
        if mp4.exists() or mp4.is_symlink():
            include(mp4, media_roots, {".mp4"})
    for raw in (session.audio_path, session.recording_path):
        if raw:
            audio = Path(raw)
            include(audio.with_suffix(".wav"), media_roots, {".wav"})
            include(audio.with_name("audio.tmp.wav"), media_roots, {".wav"})
    include(session.transcript_path, [transcript_root], {".txt", ".srt", ".vtt", ".json"})
    # The transcriber may have written its output before saving its DB path.
    include(transcript_root / f"{session.session_id}.txt", [transcript_root], {".txt"})
    return paths


def delete_session_artifacts(
    session: LiveSession, retained: list[LiveSession], settings: Settings,
    *, active_paths: set[Path] | None = None,
) -> None:
    # Validate the entire plan, including retained references, before any unlink.
    candidates = session_artifacts(session, settings)
    protected = set(active_paths or ())
    for other in retained:
        protected.update(session_artifacts(other, settings))
    shared = candidates & protected
    if shared:
        raise ValueError("文件仍被录制中的任务引用，已保留记录：" + ", ".join(map(str, sorted(shared))))
    for path in sorted(candidates):
        path.unlink(missing_ok=True)
    roots = {Path(value).resolve() for value in (
        settings.recordings_dir, settings.video_dir, settings.audio_dir, settings.transcripts_dir
    )}
    for parent in {path.parent for path in candidates} - roots:
        try:
            parent.rmdir()  # Only empty task folders; never recurse into other files.
        except OSError:
            pass
