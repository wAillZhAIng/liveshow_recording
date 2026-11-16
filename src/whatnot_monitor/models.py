from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class SessionStatus(StrEnum):
    WAITING = "WAITING"
    CAPACITY_INSUFFICIENT = "CAPACITY_INSUFFICIENT"
    RECORDING = "RECORDING"
    AUDIENCE_PENDING = "AUDIENCE_PENDING"
    TRANSCRIBING = "TRANSCRIBING"
    UPLOADING = "UPLOADING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


@dataclass(frozen=True, slots=True)
class Keyword:
    id: int
    phrase: str
    enabled: bool
    created_at: str
    scan_started_at: str | None = None
    last_scanned_at: str | None = None
    last_result: str | None = None
    discovered_count: int = 0
    recording_count: int = 0
    scan_status: str = "NEVER"

    def __getitem__(self, key: str):
        """Allow read-only dict-style access for lightweight UI adapters."""
        return getattr(self, key)


@dataclass(frozen=True, slots=True)
class LiveSession:
    session_id: str
    recording_task_id: str
    room_id: str
    name: str
    url: str
    status: SessionStatus
    started_at: str | None
    start_time_source: str
    identity_basis: str
    first_seen_at: str
    keyword_phrases: tuple[str, ...]
    audience_due_at: str | None
    audience_value: int | None
    audience_raw: str | None
    audience_captured_at: str | None
    audience_outcome: str | None
    qualification_status: str
    audience_attempts: int
    audience_retry_at: str | None
    current_viewers: int | None
    current_viewers_raw: str | None
    observed_at: str | None
    recording_started_at: str | None
    recording_path: str | None
    video_path: str | None
    audio_path: str | None
    ended_at: str | None
    duration_seconds: float | None
    transcript_text: str | None
    transcript_path: str | None
    feishu_record_id: str | None
    error: str | None
    cleanup_error: str | None
    capacity_since: str | None
    capacity_released_at: str | None
    retry_at: str | None
    attempt_count: int
    created_at: str
    updated_at: str
    audience_basis: str | None = None
    qualification_minutes: int | None = None
    minimum_viewers: int | None = None

    @property
    def id(self) -> str:
        """Stable local key; the table uses the platform session id as its PK."""
        return self.session_id

    @property
    def transcription_status(self) -> str:
        if self.qualification_status == "rejected_low_viewers":
            return "SKIPPED"
        if self.transcript_text is not None or self.transcript_path is not None:
            return "COMPLETED"
        if self.status == SessionStatus.TRANSCRIBING:
            return "IN_PROGRESS"
        if self.status == SessionStatus.FAILED and (self.audio_path or self.recording_path):
            return "FAILED"
        return "NOT_STARTED"

    @property
    def upload_status(self) -> str:
        if self.feishu_record_id:
            return "COMPLETED"
        if self.status == SessionStatus.UPLOADING:
            return "IN_PROGRESS"
        if self.status == SessionStatus.FAILED and (
            self.transcript_text is not None
            or self.qualification_status == "rejected_low_viewers"
        ):
            return "FAILED"
        return "NOT_STARTED"


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    created_at: str
    level: str
    component: str
    message: str
    session_id: str | None


@dataclass(frozen=True, slots=True)
class SessionKeywordSource:
    keyword_id: int | None
    phrase_snapshot: str
