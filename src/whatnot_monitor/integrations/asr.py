"""Optional local speech recognition adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


class ASRError(RuntimeError):
    """Base error for local transcription failures."""


class ASRUnavailableError(ASRError):
    """Raised when faster-whisper is not installed or configured."""


class TranscriptionError(ASRError):
    """Raised when a model cannot transcribe an audio file."""


@dataclass(frozen=True, slots=True)
class TranscriptSegment:
    start: float
    end: float
    text: str

    def as_line(self) -> str:
        return f"[{_format_time(self.start)} - {_format_time(self.end)}] {self.text.strip()}"


@dataclass(frozen=True, slots=True)
class TranscriptResult:
    segments: tuple[TranscriptSegment, ...]
    language: str | None = None
    language_probability: float | None = None

    @property
    def text(self) -> str:
        """Raw model output, kept in timestamped source order."""
        return "\n".join(segment.as_line() for segment in self.segments)

    def write_txt(self, path: str | Path) -> Path:
        """Persist the original transcript without modifying its audio source."""
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            self.text + ("\n" if self.segments else ""), encoding="utf-8"
        )
        temporary.replace(destination)
        return destination


def _format_time(value: float) -> str:
    milliseconds = max(0, round(value * 1000))
    hours, remainder = divmod(milliseconds, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


class FasterWhisperTranscriber:
    """A lazy faster-whisper adapter; model loading happens on first use."""

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "auto",
        compute_type: str = "int8",
        cpu_threads: int = 0,
        num_workers: int = 1,
    ) -> None:
        self.model_path = str(model_path).strip()
        self.device = device
        self.compute_type = compute_type
        self.cpu_threads = cpu_threads
        self.num_workers = num_workers
        self._model: Any | None = None

    def _get_model(self) -> Any:
        if not self.model_path:
            raise ASRUnavailableError(
                "请先在设置中指定本地 faster-whisper 模型路径。"
            )
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise ASRUnavailableError(
                "可选 faster-whisper 依赖未安装（faster-whisper is not installed）。请先安装 ASR 依赖。"
            ) from exc

        model_path = Path(self.model_path)
        if not model_path.exists():
            raise ASRUnavailableError(
                f"配置的本地 ASR 模型不存在：{model_path}"
            )
        try:
            self._model = WhisperModel(
                str(model_path),
                device=self.device,
                compute_type=self.compute_type,
                cpu_threads=self.cpu_threads,
                num_workers=self.num_workers,
            )
        except Exception as exc:
            raise ASRUnavailableError(
                f"无法加载本地 ASR 模型：{exc}"
            ) from exc
        return self._model

    def transcribe(
        self,
        audio_path: str | Path,
        *,
        language: str | None = None,
        beam_size: int = 5,
    ) -> TranscriptResult:
        source = Path(audio_path)
        if not source.is_file():
            raise TranscriptionError(f"音频文件不存在（does not exist）：{source}")
        if source.stat().st_size == 0:
            raise TranscriptionError(f"音频文件为空：{source}")

        model = self._model or self._get_model()
        try:
            generated, info = model.transcribe(
                str(source),
                language=language,
                beam_size=beam_size,
                vad_filter=False,
            )
            segments = tuple(
                TranscriptSegment(
                    start=float(segment.start),
                    end=float(segment.end),
                    text=str(segment.text),
                )
                for segment in generated
            )
        except Exception as exc:
            raise TranscriptionError(
                f"转写音频文件 {source.name} 失败：{exc}"
            ) from exc

        return TranscriptResult(
            segments=segments,
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
        )
