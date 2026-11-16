from __future__ import annotations

import json
import re
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from .models import Event, Keyword, LiveSession, SessionKeywordSource, SessionStatus

_ALLOWED_TRANSITIONS: dict[SessionStatus, set[SessionStatus]] = {
    SessionStatus.WAITING: {
        SessionStatus.RECORDING,
        SessionStatus.CAPACITY_INSUFFICIENT,
        SessionStatus.UPLOADING,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.CAPACITY_INSUFFICIENT: {
        SessionStatus.WAITING,
        SessionStatus.RECORDING,
        SessionStatus.UPLOADING,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.RECORDING: {
        SessionStatus.AUDIENCE_PENDING,
        SessionStatus.UPLOADING,
        SessionStatus.TRANSCRIBING,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.AUDIENCE_PENDING: {
        SessionStatus.UPLOADING,
        SessionStatus.TRANSCRIBING,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.TRANSCRIBING: {
        SessionStatus.UPLOADING,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.UPLOADING: {
        SessionStatus.COMPLETED,
        SessionStatus.FAILED,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.FAILED: {
        SessionStatus.WAITING,
        SessionStatus.CAPACITY_INSUFFICIENT,
        SessionStatus.RECORDING,
        SessionStatus.AUDIENCE_PENDING,
        SessionStatus.TRANSCRIBING,
        SessionStatus.UPLOADING,
        SessionStatus.INTERRUPTED,
    },
    SessionStatus.INTERRUPTED: {
        SessionStatus.WAITING,
        SessionStatus.CAPACITY_INSUFFICIENT,
        SessionStatus.RECORDING,
        SessionStatus.AUDIENCE_PENDING,
        SessionStatus.TRANSCRIBING,
        SessionStatus.UPLOADING,
        SessionStatus.FAILED,
    },
    SessionStatus.COMPLETED: set(),
}


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds")


class Store:
    """Durable SQLite store for monitoring configuration and session work."""

    def __init__(self, db_path: str | Path) -> None:
        path = str(db_path)
        self._db_path = Path(path) if path != ":memory:" else None
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = FULL")
            self._backup_before_migration_if_needed()
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _backup_before_migration_if_needed(self) -> None:
        if self._db_path is None:
            return
        existing_tables = {
            row["name"]
            for row in self._conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
            if not row["name"].startswith("sqlite_")
        }
        required_columns = {
            "keywords": {
                "scan_started_at",
                "discovered_count",
                "recording_count",
                "scan_status",
            },
            "sessions": {
                "identity_basis",
                "recording_task_id",
                "capacity_since",
                "capacity_released_at",
                "recording_started_at",
                "qualification_status",
                "audience_attempts",
                "video_path",
                "audio_path",
            },
        }
        expected_tables = {
            "recording_segments",
            "keywords",
            "sessions",
            "session_keywords",
            "session_keyword_sources",
            "events",
            "upload_tokens",
        }
        migration_needed = bool(existing_tables) and not expected_tables.issubset(
            existing_tables
        )
        for table, expected in required_columns.items():
            if table in existing_tables:
                actual = {
                    row["name"]
                    for row in self._conn.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                }
                migration_needed |= not expected.issubset(actual)
        if "sessions" in existing_tables:
            migration_needed |= self._conn.execute(
                """SELECT 1 FROM sessions
                   WHERE room_id LIKE 'whatnot-seller:%'
                     AND lower(trim(name)) LIKE 'live %'
                   LIMIT 1"""
            ).fetchone() is not None
        if not migration_needed:
            return

        backup_root = self._db_path.parent
        if backup_root.name.casefold() == "database":
            backup_root = backup_root.parent
        backup_root = backup_root / "backups"
        backup_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
        backup_path = backup_root / f"{self._db_path.stem}-before-migration-{stamp}.sqlite3"
        backup_connection = sqlite3.connect(backup_path)
        try:
            self._conn.backup(backup_connection)
        finally:
            backup_connection.close()

    def _initialize(self) -> None:
        with self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS keywords (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    phrase TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
                    last_scanned_at TEXT,
                    last_result TEXT,
                    scan_started_at TEXT,
                    discovered_count INTEGER NOT NULL DEFAULT 0,
                    recording_count INTEGER NOT NULL DEFAULT 0,
                    scan_status TEXT NOT NULL DEFAULT 'NEVER',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS rejected_show_keys (show_key TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS room_broadcasts (
                    room_key TEXT PRIMARY KEY,
                    episode_id TEXT NOT NULL,
                    started_at TEXT,
                    ended INTEGER NOT NULL DEFAULT 0
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    session_id TEXT PRIMARY KEY,
                    recording_task_id TEXT UNIQUE,
                    room_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    started_at TEXT,
                    start_time_source TEXT NOT NULL,
                    identity_basis TEXT NOT NULL DEFAULT 'legacy_fallback',
                    first_seen_at TEXT NOT NULL,
                    audience_due_at TEXT,
                    audience_value INTEGER,
                    audience_raw TEXT,
                    audience_captured_at TEXT,
                    audience_outcome TEXT,
                    qualification_status TEXT NOT NULL DEFAULT 'pending',
                    audience_attempts INTEGER NOT NULL DEFAULT 0,
                    audience_retry_at TEXT,
                    current_viewers INTEGER,
                    current_viewers_raw TEXT,
                    observed_at TEXT,
                    recording_started_at TEXT,
                    recording_path TEXT,
                    video_path TEXT,
                    audio_path TEXT,
                    ended_at TEXT,
                    duration_seconds REAL,
                    transcript_text TEXT,
                    transcript_path TEXT,
                    feishu_record_id TEXT,
                    error TEXT,
                    cleanup_error TEXT,
                    capacity_since TEXT,
                    capacity_released_at TEXT,
                    retry_at TEXT,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_sessions_room_status
                    ON sessions(room_id, status, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_sessions_status_retry
                    ON sessions(status, retry_at);
                CREATE TABLE IF NOT EXISTS session_keywords (
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    phrase TEXT NOT NULL,
                    PRIMARY KEY (session_id, phrase)
                );
                CREATE TABLE IF NOT EXISTS session_keyword_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    keyword_id INTEGER REFERENCES keywords(id) ON DELETE SET NULL,
                    phrase_snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_session_keyword_sources_keyed
                    ON session_keyword_sources(session_id, keyword_id)
                    WHERE keyword_id IS NOT NULL;
                CREATE INDEX IF NOT EXISTS idx_session_keyword_sources_session
                    ON session_keyword_sources(session_id, id);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT NOT NULL,
                    level TEXT NOT NULL,
                    component TEXT NOT NULL,
                    message TEXT NOT NULL,
                    session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at DESC);

                CREATE TABLE IF NOT EXISTS upload_tokens (
                    session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
                    client_token TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS recording_segments (
                    segment_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                    video_path TEXT, audio_path TEXT,
                    started_at TEXT, ended_at TEXT,
                    status TEXT NOT NULL, reason TEXT, audience_snapshot TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_recording_segments_session
                    ON recording_segments(session_id, started_at);
                """
            )
            self._migrate_keyword_scan_columns()
            self._migrate_session_identity_column()
            self._migrate_recording_task_id_column()
            self._migrate_session_capacity_column()
            self._migrate_session_capacity_release_column()
            self._migrate_session_recording_started_column()
            self._migrate_session_qualification_columns()
            self._migrate_session_keyword_sources()
            self._migrate_viewer_badge_room_names()

    def _migrate_viewer_badge_room_names(self) -> None:
        """Replace historical Live · N labels with the stable seller handle."""
        pattern = re.compile(
            r"^(?:live(?:\s*[·•.:\-]\s*[\d,.]+\s*[kKmM]?)?|"
            r"[\d,.]+\s*(?:live|viewers?|watching))$",
            re.IGNORECASE,
        )
        rows = self._conn.execute(
            "SELECT session_id, room_id, name FROM sessions"
        ).fetchall()
        updates = []
        for row in rows:
            seller_prefix = "whatnot-seller:"
            name = str(row["name"] or "").strip()
            if (
                pattern.fullmatch(name)
                and str(row["room_id"]).startswith(seller_prefix)
                and str(row["room_id"])[len(seller_prefix) :].strip()
            ):
                updates.append(
                    (
                        str(row["room_id"])[len(seller_prefix) :].strip(),
                        _utc_now(),
                        row["session_id"],
                    )
                )
        if updates:
            self._conn.executemany(
                "UPDATE sessions SET name=?, updated_at=? WHERE session_id=?",
                updates,
            )

    def _migrate_keyword_scan_columns(self) -> None:
        """Add scan metadata to existing databases without replacing user data."""
        existing = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(keywords)").fetchall()
        }
        additions = {
            "scan_started_at": "TEXT",
            "discovered_count": "INTEGER NOT NULL DEFAULT 0",
            "recording_count": "INTEGER NOT NULL DEFAULT 0",
            "scan_status": "TEXT NOT NULL DEFAULT 'NEVER'",
        }
        for name, definition in additions.items():
            if name not in existing:
                self._conn.execute(
                    f"ALTER TABLE keywords ADD COLUMN {name} {definition}"
                )

    def _migrate_session_identity_column(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "identity_basis" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN identity_basis TEXT NOT NULL DEFAULT 'legacy_fallback'"
            )

    def _migrate_recording_task_id_column(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "recording_task_id" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN recording_task_id TEXT"
            )
        rows = self._conn.execute(
            "SELECT session_id FROM sessions WHERE recording_task_id IS NULL"
        ).fetchall()
        self._conn.executemany(
            "UPDATE sessions SET recording_task_id=? WHERE session_id=?",
            ((f"recording:{uuid.uuid4()}", row["session_id"]) for row in rows),
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_sessions_recording_task_id "
            "ON sessions(recording_task_id)"
        )

    def _migrate_session_capacity_column(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "capacity_since" not in columns:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN capacity_since TEXT")

    def _migrate_session_capacity_release_column(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "capacity_released_at" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN capacity_released_at TEXT"
            )

    def _migrate_session_recording_started_column(self) -> None:
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "recording_started_at" not in columns:
            self._conn.execute(
                "ALTER TABLE sessions ADD COLUMN recording_started_at TEXT"
            )

    def _migrate_session_qualification_columns(self) -> None:
        """Add durable audience qualification and separate media paths."""
        columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(sessions)").fetchall()
        }
        qualification_column_missing = "qualification_status" not in columns
        additions = {
            "qualification_status": "TEXT NOT NULL DEFAULT 'pending'",
            "audience_attempts": "INTEGER NOT NULL DEFAULT 0",
            "audience_retry_at": "TEXT",
            "video_path": "TEXT",
            "audio_path": "TEXT",
            "cleanup_error": "TEXT",
            "audience_basis": "TEXT",
            "qualification_minutes": "INTEGER",
            "minimum_viewers": "INTEGER",
        }
        for name, definition in additions.items():
            if name not in columns:
                self._conn.execute(
                    f"ALTER TABLE sessions ADD COLUMN {name} {definition}"
                )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_sessions_audience_retry "
            "ON sessions(qualification_status, audience_retry_at, audience_due_at)"
        )
        if qualification_column_missing:
            self._conn.execute(
                """UPDATE sessions SET qualification_status='legacy'
                   WHERE audience_outcome IS NOT NULL"""
            )
        self._conn.execute(
            "UPDATE sessions SET audio_path=recording_path "
            "WHERE audio_path IS NULL AND recording_path IS NOT NULL"
        )

    def _migrate_session_keyword_sources(self) -> None:
        """Backfill historical phrase-only attribution into the ID-aware table."""
        self._conn.execute(
            """INSERT INTO session_keyword_sources(
                   session_id, keyword_id, phrase_snapshot, created_at)
               SELECT legacy.session_id, keyword.id, legacy.phrase, ?
               FROM session_keywords AS legacy
               LEFT JOIN keywords AS keyword
                   ON keyword.phrase = legacy.phrase COLLATE NOCASE
               WHERE NOT EXISTS (
                   SELECT 1 FROM session_keyword_sources AS source
                   WHERE source.session_id=legacy.session_id
                     AND source.keyword_id IS keyword.id
                     AND source.phrase_snapshot=legacy.phrase
               )""",
            (_utc_now(),),
        )

    def upsert_keyword(
        self, phrase: str, *, keyword_id: int | None = None, enabled: bool = True
    ) -> int:
        phrase = phrase.strip()
        if not phrase:
            raise ValueError("Keyword phrase cannot be empty")
        now = _utc_now()
        with self._lock, self._conn:
            if keyword_id is None:
                self._conn.execute(
                    """INSERT INTO keywords(phrase, enabled, created_at, updated_at)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(phrase) DO UPDATE SET enabled=excluded.enabled,
                           updated_at=excluded.updated_at""",
                    (phrase, int(enabled), now, now),
                )
                row = self._conn.execute(
                    "SELECT id FROM keywords WHERE phrase = ? COLLATE NOCASE", (phrase,)
                ).fetchone()
                assert row is not None
                return int(row["id"])
            self._conn.execute(
                """UPDATE keywords SET phrase=?, enabled=?, updated_at=? WHERE id=?""",
                (phrase, int(enabled), now, keyword_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown keyword id: {keyword_id}")
            return keyword_id

    def set_keyword_enabled(self, keyword_id: int, enabled: bool) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE keywords SET enabled=?, updated_at=? WHERE id=?",
                (int(enabled), _utc_now(), keyword_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown keyword id: {keyword_id}")

    def update_keyword_scan(
        self, keyword_id: int, *, scanned_at: str, result: str | None
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE keywords SET last_scanned_at=?, last_result=?, updated_at=? WHERE id=?",
                (scanned_at, result, _utc_now(), keyword_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown keyword id: {keyword_id}")

    def begin_keyword_scan(
        self, keyword_id: int, started_at: str | None = None
    ) -> None:
        timestamp = started_at or _utc_now()
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE keywords SET scan_started_at=?, scan_status='SCANNING',
                   updated_at=? WHERE id=?""",
                (timestamp, _utc_now(), keyword_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown keyword id: {keyword_id}")

    def finish_keyword_scan(
        self,
        keyword_id: int,
        *,
        started_at: str | None,
        ended_at: str,
        result: str | None,
        discovered_count: int,
        recording_count: int,
        status: str = "OK",
    ) -> None:
        if discovered_count < 0 or recording_count < 0:
            raise ValueError("Scan counts cannot be negative")
        if recording_count > discovered_count:
            raise ValueError("recording_count cannot exceed discovered_count")
        scan_status = status.strip().upper()
        if scan_status not in {"OK", "ERROR", "EMPTY", "CANCELLED"}:
            raise ValueError(f"Invalid scan status: {status}")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT scan_started_at FROM keywords WHERE id=?", (keyword_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown keyword id: {keyword_id}")
            # Ignore stale completion from an earlier scan if a newer scan began.
            current_started = row["scan_started_at"]
            if started_at is not None and current_started not in (None, started_at):
                return
            self._conn.execute(
                """UPDATE keywords SET scan_started_at=NULL, last_scanned_at=?,
                   last_result=?, discovered_count=?, recording_count=?, scan_status=?,
                   updated_at=? WHERE id=?""",
                (
                    ended_at,
                    result,
                    discovered_count,
                    recording_count,
                    scan_status,
                    _utc_now(),
                    keyword_id,
                ),
            )

    def keyword_scan_counts(self) -> dict[str, int]:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS keyword_count,
                          COALESCE(SUM(CASE WHEN enabled=1 THEN 1 ELSE 0 END), 0) AS enabled_count,
                          COALESCE(SUM(discovered_count), 0) AS discovered_count,
                          COALESCE(SUM(recording_count), 0) AS recording_count,
                          COALESCE(SUM(CASE WHEN scan_status='ERROR' THEN 1 ELSE 0 END), 0) AS failed_count
                   FROM keywords"""
            ).fetchone()
        keys = (
            "keyword_count",
            "enabled_count",
            "discovered_count",
            "recording_count",
            "failed_count",
        )
        return {key: int(row[key]) for key in keys}

    def delete_keyword(self, keyword_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM keywords WHERE id=?", (keyword_id,))
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown keyword id: {keyword_id}")

    def list_keywords(self, *, enabled_only: bool = False) -> list[Keyword]:
        sql = "SELECT * FROM keywords"
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY phrase COLLATE NOCASE"
        with self._lock:
            rows = self._conn.execute(sql).fetchall()
        return [
            Keyword(
                id=row["id"],
                phrase=row["phrase"],
                enabled=bool(row["enabled"]),
                created_at=row["created_at"],
                scan_started_at=row["scan_started_at"],
                last_scanned_at=row["last_scanned_at"],
                last_result=row["last_result"],
                discovered_count=row["discovered_count"],
                recording_count=row["recording_count"],
                scan_status=row["scan_status"],
            )
            for row in rows
        ]

    def create_session(
        self,
        session_id: str,
        room_id: str,
        name: str,
        url: str,
        *,
        started_at: str | None,
        start_time_source: str,
        identity_basis: str = "legacy_fallback",
        first_seen_at: str,
        keyword_phrases: Iterable[str] = (),
        audience_due_at: str | None = None,
    ) -> tuple[LiveSession, bool]:
        """Create once by platform session ID; return existing row on duplicates."""
        if not all(value.strip() for value in (session_id, room_id, name, url)):
            raise ValueError("session_id, room_id, name and url are required")
        now = _utc_now()
        phrases = tuple(dict.fromkeys(p.strip() for p in keyword_phrases if p.strip()))
        with self._lock, self._conn:
            cur = self._conn.execute(
                """INSERT OR IGNORE INTO sessions(
                       session_id, recording_task_id, room_id, name, url, status, started_at,
                       start_time_source, identity_basis, first_seen_at,
                       audience_due_at, created_at, updated_at)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    session_id,
                    f"recording:{uuid.uuid4()}",
                    room_id,
                    name,
                    url,
                    SessionStatus.WAITING.value,
                    started_at,
                    start_time_source,
                    identity_basis,
                    first_seen_at,
                    audience_due_at,
                    now,
                    now,
                ),
            )
            created = cur.rowcount == 1
            existing = self._conn.execute(
                "SELECT room_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if existing["room_id"] != room_id:
                raise ValueError(
                    f"session_id {session_id!r} is already assigned to another room"
                )
            self._conn.executemany(
                "INSERT OR IGNORE INTO session_keywords(session_id, phrase) VALUES(?, ?)",
                ((session_id, phrase) for phrase in phrases),
            )
        session = self.get_session(session_id)
        assert session is not None
        if created:
            self.add_event(
                "INFO", "store", "Live session created", session_id=session_id
            )
        return session, created

    def add_session_keyword_source(
        self, session_id: str, keyword_id: int | None, phrase: str
    ) -> SessionKeywordSource:
        phrase_snapshot = phrase.strip()
        if not phrase_snapshot:
            raise ValueError("phrase cannot be empty")
        with self._lock, self._conn:
            if keyword_id is not None:
                keyword = self._conn.execute(
                    "SELECT id FROM keywords WHERE id=?", (keyword_id,)
                ).fetchone()
                if keyword is None:
                    raise KeyError(f"Unknown keyword id: {keyword_id}")
                self._conn.execute(
                    """INSERT INTO session_keyword_sources(
                           session_id, keyword_id, phrase_snapshot, created_at)
                       VALUES(?, ?, ?, ?)
                       ON CONFLICT(session_id, keyword_id) WHERE keyword_id IS NOT NULL
                       DO NOTHING""",
                    (session_id, keyword_id, phrase_snapshot, _utc_now()),
                )
            else:
                exists = self._conn.execute(
                    """SELECT 1 FROM session_keyword_sources
                       WHERE session_id=? AND keyword_id IS NULL AND phrase_snapshot=?""",
                    (session_id, phrase_snapshot),
                ).fetchone()
                if exists is None:
                    self._conn.execute(
                        """INSERT INTO session_keyword_sources(
                               session_id, keyword_id, phrase_snapshot, created_at)
                           VALUES(?, NULL, ?, ?)""",
                        (session_id, phrase_snapshot, _utc_now()),
                    )
            row = self._conn.execute(
                """SELECT keyword_id, phrase_snapshot FROM session_keyword_sources
                   WHERE session_id=? AND keyword_id IS ?
                     AND phrase_snapshot=? ORDER BY id DESC LIMIT 1""",
                (session_id, keyword_id, phrase_snapshot),
            ).fetchone()
            if row is None:
                # A keyword rename may update a prior source row's snapshot in place.
                row = self._conn.execute(
                    """SELECT keyword_id, phrase_snapshot FROM session_keyword_sources
                       WHERE session_id=? AND keyword_id IS ? ORDER BY id DESC LIMIT 1""",
                    (session_id, keyword_id),
                ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session id: {session_id}")
            return SessionKeywordSource(
                keyword_id=row["keyword_id"], phrase_snapshot=row["phrase_snapshot"]
            )

    def session_keyword_sources(self, session_id: str) -> list[SessionKeywordSource]:
        with self._lock:
            rows = self._conn.execute(
                """SELECT keyword_id, phrase_snapshot FROM session_keyword_sources
                   WHERE session_id=? ORDER BY id""",
                (session_id,),
            ).fetchall()
            exists = self._conn.execute(
                "SELECT 1 FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        if exists is None:
            raise KeyError(f"Unknown session id: {session_id}")
        return [
            SessionKeywordSource(
                keyword_id=row["keyword_id"], phrase_snapshot=row["phrase_snapshot"]
            )
            for row in rows
        ]

    def session_source_phrases(self, session_id: str) -> list[str]:
        return [
            source.phrase_snapshot
            for source in self.session_keyword_sources(session_id)
        ]

    def find_active_session_for_room(self, room_id: str) -> LiveSession | None:
        with self._lock:
            row = self._conn.execute(
                """SELECT * FROM sessions WHERE room_id=? AND status != ?
                   ORDER BY updated_at DESC, rowid DESC LIMIT 1""",
                (room_id, SessionStatus.COMPLETED.value),
            ).fetchone()
        return self._session_from_row(row) if row else None

    def get_session(self, session_id: str) -> LiveSession | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return self._session_from_row(row) if row else None

    def clear_non_recording_sessions(
        self,
        remove_files: Callable[[LiveSession, list[LiveSession]], None],
        *,
        protected_ids: Iterable[str] = (),
    ) -> dict:
        """Clear all inactive history, independent of UI limits/filters.

        The runtime must serialize this with capture startup and postprocessing.
        Keep each DB row until its file cleanup succeeds, so failures are retryable.
        """
        protected = set(protected_ids)
        deleted = 0
        failures = {}
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            sessions = [self._session_from_row(row) for row in self._conn.execute(
                "SELECT * FROM sessions"
            ).fetchall()]
            retained = [session for session in sessions if (
                session.status == SessionStatus.RECORDING or session.session_id in protected
            )]
            retained_ids = {session.session_id for session in retained}
            for session in sessions:
                if session.session_id in retained_ids:
                    continue
                try:
                    remove_files(session, retained)
                except (OSError, ValueError) as exc:
                    failures[session.session_id] = str(exc)
                    self._conn.execute(
                        "UPDATE sessions SET cleanup_error=?, updated_at=? WHERE session_id=?",
                        (str(exc), _utc_now(), session.session_id),
                    )
                    continue
                # Relation/token rows cascade. Session events need explicit removal.
                self._conn.execute("DELETE FROM events WHERE session_id=?", (session.session_id,))
                self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session.session_id,))
                deleted += 1
        return {"deleted": deleted, "retained": len(retained), "failures": failures}

    def show_was_rejected(self, show_key: str) -> bool:
        with self._lock:
            return self._conn.execute(
                "SELECT 1 FROM rejected_show_keys WHERE show_key=?", (show_key,)
            ).fetchone() is not None

    def live_episode(self, room_key: str, started_at: str | None) -> tuple[str, str | None]:
        """Return one durable ID per observed broadcast of a reusable room."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            row = self._conn.execute(
                "SELECT * FROM room_broadcasts WHERE room_key=?", (room_key,)
            ).fetchone()
            episode = row["episode_id"] if row else f"platform-live:{room_key}"
            previous_start = row["started_at"] if row else None
            newer = started_at and previous_start and started_at > previous_start
            stale = started_at and previous_start and started_at <= previous_start
            if newer or (row and row["ended"] and not stale):
                episode = f"platform-live:{room_key}:{uuid.uuid4().hex}"
                previous_start = None
            elif row and row["ended"]:
                return episode, previous_start
            start = previous_start or started_at
            self._conn.execute(
                """INSERT INTO room_broadcasts(room_key,episode_id,started_at,ended)
                   VALUES (?,?,?,0) ON CONFLICT(room_key) DO UPDATE SET
                   episode_id=excluded.episode_id, started_at=excluded.started_at, ended=0""",
                (room_key, episode, start),
            )
            return episode, start

    def mark_room_ended(self, room_key: str, episode_id: str | None = None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE room_broadcasts SET ended=1 WHERE room_key=? AND (? IS NULL OR episode_id=?)",
                (room_key, episode_id, episode_id),
            )

    def remove_room_candidate(self, session_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """DELETE FROM sessions WHERE session_id=? AND status IN ('WAITING','CAPACITY_INSUFFICIENT')
                   AND video_path IS NULL AND audio_path IS NULL AND recording_path IS NULL""",
                (session_id,),
            )

    def remove_rejected_session(self, session_id: str, show_key: str) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT OR IGNORE INTO rejected_show_keys VALUES (?)", (show_key,)
            )
            self._conn.execute("DELETE FROM events WHERE session_id=?", (session_id,))
            self._conn.execute("DELETE FROM sessions WHERE session_id=?", (session_id,))

    def sessions_due_for_audience(self, now: str | None = None) -> list[LiveSession]:
        """Return sessions whose scheduled audience check is due and unresolved."""
        timestamp = now or _utc_now()
        with self._lock:
            rows = self._conn.execute(
                """SELECT * FROM sessions
                   WHERE audience_due_at IS NOT NULL AND audience_due_at <= ?
                     AND ((audience_outcome IS NULL AND qualification_status = 'pending')
                          OR (audience_outcome = 'missed' AND qualification_status = 'viewer_check_failed'))
                     AND (audience_retry_at IS NULL OR audience_retry_at <= ?)
                     AND status = ?
                   ORDER BY audience_due_at""",
                (timestamp, timestamp, SessionStatus.RECORDING.value),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def list_sessions(
        self,
        *,
        statuses: Iterable[SessionStatus | str] | None = None,
        limit: int = 500,
    ) -> list[LiveSession]:
        if limit < 1:
            raise ValueError("limit must be positive")
        values = (
            tuple(SessionStatus(s).value for s in statuses)
            if statuses is not None
            else ()
        )
        sql = "SELECT * FROM sessions"
        params: tuple[object, ...] = ()
        if values:
            sql += f" WHERE status IN ({','.join('?' for _ in values)})"
            params = values
        sql += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(sql, (*params, limit)).fetchall()
        return [self._session_from_row(row) for row in rows]

    def waiting_sessions(self, *, limit: int = 1000, platform: str = "whatnot") -> list[LiveSession]:
        """Capture queue order is independent of the UI's recent-update order."""
        platform_filter = (
            "session_id NOT LIKE ?" if platform == "whatnot" else "session_id LIKE ?"
        )
        prefix = "platform-live:%" if platform == "whatnot" else f"platform-live:{platform}:%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM sessions WHERE status IN ('WAITING','CAPACITY_INSUFFICIENT') "
                f"AND {platform_filter} ORDER BY first_seen_at ASC, created_at ASC, session_id ASC LIMIT ?",
                (prefix, limit),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def transition(
        self,
        session_id: str,
        status: SessionStatus | str,
        *,
        error: str | None = None,
        retry_at: str | None = None,
    ) -> LiveSession:
        next_status = SessionStatus(status)
        now = _utc_now()
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session id: {session_id}")
            current = SessionStatus(row["status"])
            if (
                next_status != current
                and next_status not in _ALLOWED_TRANSITIONS[current]
            ):
                raise ValueError(
                    f"Invalid session transition: {current.value} -> {next_status.value}"
                )
            attempts = 1 if next_status == SessionStatus.FAILED else 0
            if next_status == SessionStatus.FAILED and current == SessionStatus.FAILED:
                attempts = 1
            self._conn.execute(
                """UPDATE sessions SET status=?, error=?, retry_at=?,
                       capacity_since=CASE WHEN ? THEN ? ELSE capacity_since END,
                       capacity_released_at=CASE
                           WHEN ? THEN NULL
                           WHEN ? THEN ?
                           ELSE capacity_released_at
                       END,
                       attempt_count=attempt_count+?, updated_at=?
                   WHERE session_id=?""",
                (
                    next_status.value,
                    error,
                    retry_at,
                    int(next_status == SessionStatus.CAPACITY_INSUFFICIENT),
                    now,
                    int(next_status == SessionStatus.CAPACITY_INSUFFICIENT),
                    int(
                        current == SessionStatus.CAPACITY_INSUFFICIENT
                        and next_status != SessionStatus.CAPACITY_INSUFFICIENT
                    ),
                    now,
                    attempts,
                    now,
                    session_id,
                ),
            )
            self._insert_event(
                "ERROR" if next_status == SessionStatus.FAILED else "INFO",
                "state",
                f"{current.value} -> {next_status.value}"
                + (f": {error}" if error else ""),
                session_id,
                now,
            )
        result = self.get_session(session_id)
        assert result is not None
        return result

    def save_recording(
        self,
        session_id: str,
        path: str,
        *,
        ended_at: str | None = None,
        duration_seconds: float | None = None,
    ) -> LiveSession:
        if duration_seconds is not None and duration_seconds < 0:
            raise ValueError("duration_seconds cannot be negative")
        return self._update_session_artifacts(
            session_id,
            "recording_path=?, ended_at=COALESCE(?, ended_at), "
            "duration_seconds=COALESCE(?, duration_seconds)",
            (path, ended_at, duration_seconds),
        )

    def save_recording_assets(
        self,
        session_id: str,
        *,
        video_path: str | None,
        audio_path: str | None,
    ) -> LiveSession:
        """Persist separate page-local media outputs while retaining legacy path."""
        return self._update_session_artifacts(
            session_id,
            "video_path=?, audio_path=?, recording_path=?",
            (video_path, audio_path, audio_path),
        )

    def save_recording_started(
        self, session_id: str, started_at: str | None = None, *,
        qualification_minutes: int | None = None, minimum_viewers: int | None = None,
    ) -> LiveSession:
        """Freeze a new capture's qualification policy and its actual start."""
        timestamp = started_at or _utc_now()
        return self._update_session_artifacts(
            session_id, "recording_started_at=CASE WHEN audience_basis IN "
            "('recording_start_estimate','recording_start') OR "
            "(recording_started_at IS NOT NULL AND qualification_minutes IS NOT NULL) "
            "THEN recording_started_at ELSE ? END, "
            "qualification_minutes=COALESCE(qualification_minutes,?), "
            "minimum_viewers=COALESCE(minimum_viewers,?)",
            (timestamp, qualification_minutes, minimum_viewers),
        )

    def set_audience_schedule(self, session_id: str, due_at: str, basis: str) -> LiveSession:
        if basis not in {"platform_start", "recording_start_estimate", "recording_start"}:
            raise ValueError("Invalid audience schedule basis")
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE sessions SET audience_due_at=?, audience_basis=?, updated_at=?
                   WHERE session_id=? AND audience_basis IS NULL
                     AND audience_outcome IS NULL AND qualification_status='pending'""",
                (due_at, basis, _utc_now(), session_id),
            )
        result = self.get_session(session_id)
        if result is None:
            raise KeyError(session_id)
        return result

    def save_audience(
        self,
        session_id: str,
        *,
        value: int | None,
        captured_at: str | None,
        raw_value: str | None,
        outcome: str = "captured",
    ) -> LiveSession:
        if outcome not in {
            "captured",
            "captured_late",
            "captured_at_end",
            "missed",
            "not_reached",
            "ended_before_due",
            "failed",
            "unknown",
        }:
            raise ValueError(f"Unknown audience outcome: {outcome}")
        if value is not None and value < 0:
            raise ValueError("audience value cannot be negative")
        if outcome not in {"captured", "captured_late", "captured_at_end"} and value is not None:
            raise ValueError("Only captured samples may contain a numeric value")
        return self._update_session_artifacts(
            session_id,
            "audience_value=?, audience_raw=?, audience_captured_at=?, audience_outcome=?",
            (value, raw_value, captured_at, outcome),
        )

    def prepare_missed_audience_retry(self, session_id: str) -> None:
        """Explicitly re-open a missed sample for a fresh delayed observation."""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE sessions SET audience_outcome=NULL,qualification_status='pending',
                   audience_attempts=0,audience_retry_at=NULL,updated_at=?
                   WHERE session_id=? AND audience_outcome='missed'
                     AND qualification_status='viewer_check_failed' AND status='RECORDING'""",
                (_utc_now(), session_id),
            )

    def begin_audience_attempt(self, session_id: str) -> int:
        """Atomically increment and return the next 30-minute check attempt."""
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE sessions SET audience_attempts=audience_attempts+1,
                       audience_retry_at=NULL, updated_at=?
                   WHERE session_id=? AND qualification_status='pending'
                     AND audience_outcome IS NULL""",
                (_utc_now(), session_id),
            )
            row = self._conn.execute(
                "SELECT audience_attempts FROM sessions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown session id: {session_id}")
            return int(row["audience_attempts"])

    def record_audience_failure(
        self,
        session_id: str,
        *,
        attempt: int,
        retry_at: str | None,
        exhausted: bool,
        captured_at: str,
    ) -> LiveSession:
        status = "viewer_check_failed" if exhausted else "pending"
        with self._lock, self._conn:
            self._conn.execute(
                """UPDATE sessions SET qualification_status=?, audience_retry_at=?,
                       audience_outcome=CASE WHEN ? THEN 'failed' ELSE audience_outcome END,
                       audience_captured_at=CASE WHEN ? THEN ? ELSE audience_captured_at END,
                       updated_at=? WHERE session_id=?""",
                (
                    status,
                    None if exhausted else retry_at,
                    int(exhausted),
                    int(exhausted),
                    captured_at,
                    _utc_now(),
                    session_id,
                ),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown session id: {session_id}")
        result = self.get_session(session_id)
        assert result is not None
        self.add_event(
            "ERROR" if exhausted else "WARNING",
            "audience",
            (
                f"Audience check exhausted after attempt {attempt}"
                if exhausted
                else f"Audience check attempt {attempt} failed; retry scheduled for {retry_at}"
            ),
            session_id=session_id,
        )
        return result

    def set_qualification_status(
        self,
        session_id: str,
        status: str,
        *,
        retry_at: str | None = None,
        cleanup_error: str | None = None,
        outcome: str | None = None,
    ) -> LiveSession:
        allowed = {
            "pending",
            "qualified",
            "rejected_low_viewers",
            "low_viewers_deferred",
            "viewer_check_failed",
            "ended_before_threshold",
            "cleanup_failed",
            "legacy",
        }
        if status not in allowed:
            raise ValueError(f"Unknown qualification status: {status}")
        return self._update_session_artifacts(
            session_id,
            "qualification_status=?, audience_retry_at=?, cleanup_error=?, "
            "audience_outcome=COALESCE(?, audience_outcome)",
            (status, retry_at, cleanup_error, outcome),
        )

    def update_live_observation(
        self,
        session_id: str,
        *,
        current_viewers: int | None,
        current_viewers_raw: str | None,
        observed_at: str,
    ) -> LiveSession:
        if current_viewers is not None and current_viewers < 0:
            raise ValueError("current_viewers cannot be negative")
        return self._update_session_artifacts(
            session_id,
            "current_viewers=?, current_viewers_raw=?, observed_at=?",
            (current_viewers, current_viewers_raw, observed_at),
        )

    def save_transcript(self, session_id: str, text: str, path: str) -> LiveSession:
        return self._update_session_artifacts(
            session_id,
            "transcript_text=?, transcript_path=?",
            (text, path),
        )

    def mark_uploaded(self, session_id: str, record_id: str) -> LiveSession:
        return self._update_session_artifacts(
            session_id, "feishu_record_id=?", (record_id,)
        )

    def finalize_uploaded(self, session_id: str, record_id: str) -> LiveSession:
        """Atomically persist the confirmed remote ID and local completion state."""
        if not isinstance(record_id, str) or not record_id.strip():
            raise ValueError("飞书记录 ID 不能为空")
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT status,feishu_record_id FROM sessions WHERE session_id=?", (session_id,)
            ).fetchone()
            if row is None:
                raise KeyError(session_id)
            if row["status"] not in {"UPLOADING", "COMPLETED"}:
                raise ValueError("只有上传中任务可以标记完成")
            if row["feishu_record_id"] and row["feishu_record_id"] != record_id:
                raise ValueError("已保存的飞书记录 ID 不匹配")
            self._conn.execute(
                "UPDATE sessions SET feishu_record_id=?,status='COMPLETED',error=NULL,updated_at=? WHERE session_id=?",
                (record_id, _utc_now(), session_id),
            )
        result = self.get_session(session_id)
        assert result is not None
        return result

    def get_or_create_upload_token(self, session_id: str) -> str:
        """Persist one UUIDv4 per session so an ambiguous network retry is idempotent."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT client_token FROM upload_tokens WHERE session_id=?",
                (session_id,),
            ).fetchone()
            if row:
                return str(row["client_token"])
            token = str(uuid.uuid4())
            self._conn.execute(
                "INSERT INTO upload_tokens(session_id, client_token, created_at) VALUES(?, ?, ?)",
                (session_id, token, _utc_now()),
            )
            return token

    def _update_session_artifacts(
        self, session_id: str, assignments: str, params: Sequence[object]
    ) -> LiveSession:
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE sessions SET {assignments}, updated_at=? WHERE session_id=?",
                (*params, _utc_now(), session_id),
            )
            if self._conn.execute("SELECT changes()").fetchone()[0] == 0:
                raise KeyError(f"Unknown session id: {session_id}")
        result = self.get_session(session_id)
        assert result is not None
        return result

    def media_path_referenced_elsewhere(self, session_id: str, path: Path) -> bool:
        """Check all persisted media references before destructive cleanup."""
        resolved = path.resolve()
        with self._lock:
            rows = self._conn.execute(
                "SELECT video_path, audio_path, recording_path FROM sessions WHERE session_id != ?",
                (session_id,),
            ).fetchall()
            segments = self._conn.execute(
                "SELECT video_path, audio_path FROM recording_segments WHERE session_id != ?",
                (session_id,),
            ).fetchall()
        return any(Path(raw).resolve() == resolved for row in (*rows, *segments) for raw in row if raw)

    def list_recording_segments(self, session_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM recording_segments WHERE session_id=? ORDER BY rowid", (session_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_recording_segment(self, segment_id: str, session_id: str,
                              video_path: str | None, audio_path: str | None) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO recording_segments(segment_id,session_id,video_path,audio_path,status) VALUES(?,?,?,?,?)",
                (segment_id, session_id, video_path, audio_path, "starting"),
            )

    def update_recording_segment(self, segment_id: str, status: str, *,
                                 timestamp: str | None = None, reason: str | None = None) -> None:
        with self._lock, self._conn:
            if status == "recording":
                self._conn.execute("UPDATE recording_segments SET status=?,started_at=? WHERE segment_id=?",
                                   (status, timestamp or _utc_now(), segment_id))
            else:
                self._conn.execute("UPDATE recording_segments SET status=?,ended_at=?,reason=? WHERE segment_id=?",
                                   (status, timestamp, reason, segment_id))

    def prepare_interrupted_resume(self, session_id: str) -> None:
        """Retain the previous paths and observation before a new segment starts."""
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
            if row is None or row["status"] != "INTERRUPTED":
                return
            snapshot = json.dumps({k: row[k] for k in (
                "audience_basis", "audience_due_at", "audience_value", "audience_captured_at",
                "audience_outcome", "qualification_status", "qualification_minutes", "minimum_viewers")}, ensure_ascii=False)
            cur = self._conn.execute(
                "UPDATE recording_segments SET status='interrupted',ended_at=?,reason=?,audience_snapshot=? "
                "WHERE session_id=? AND status IN ('starting','recording')",
                (row["ended_at"], row["error"], snapshot, session_id),
            )
            if cur.rowcount == 0:
                previous = self._conn.execute(
                    "SELECT segment_id FROM recording_segments WHERE session_id=? AND video_path IS ? AND audio_path IS ? ORDER BY rowid DESC LIMIT 1",
                    (session_id, row["video_path"], row["audio_path"] or row["recording_path"]),
                ).fetchone()
                if previous:
                    self._conn.execute("UPDATE recording_segments SET status='interrupted', audience_snapshot=? WHERE segment_id=?", (snapshot, previous["segment_id"]))
                else:
                    self._conn.execute(
                        """INSERT INTO recording_segments(segment_id,session_id,video_path,audio_path,
                           started_at,ended_at,status,reason,audience_snapshot) VALUES(?,?,?,?,?,?,?,?,?)""",
                        (str(uuid.uuid4()), session_id, row["video_path"], row["audio_path"] or row["recording_path"],
                         row["recording_started_at"], row["ended_at"], "interrupted", row["error"], snapshot),
                    )
            self._conn.execute(
                """UPDATE sessions SET status='WAITING',audience_basis=NULL,audience_due_at=NULL,
                   audience_value=NULL,audience_raw=NULL,audience_captured_at=NULL,audience_outcome=NULL,
                   audience_attempts=0,audience_retry_at=NULL,qualification_status='pending',
                   recording_started_at=NULL,qualification_minutes=NULL,minimum_viewers=NULL,
                   ended_at=NULL,duration_seconds=NULL,
                   retry_at=NULL,updated_at=? WHERE session_id=?""",
                (_utc_now(), session_id),
            )

    def interrupt_orphaned_recordings(self) -> list[LiveSession]:
        """Mark recordings from a previous process as interrupted after restart."""
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT session_id FROM sessions WHERE status=?",
                (SessionStatus.RECORDING.value,),
            ).fetchall()
            now = _utc_now()
            for row in rows:
                session_id = row["session_id"]
                self._conn.execute(
                    """UPDATE sessions SET status=?, error=?, retry_at=NULL, updated_at=?
                       WHERE session_id=?""",
                    (
                        SessionStatus.INTERRUPTED.value,
                        "Application restarted while recording",
                        now,
                        session_id,
                    ),
                )
                self._insert_event(
                    "WARNING",
                    "recovery",
                    "Recording marked interrupted after restart",
                    session_id,
                    now,
                )
        return [self.get_session(row["session_id"]) for row in rows]  # type: ignore[list-item]

    def recoverable_sessions(self, *, now: str | None = None) -> list[LiveSession]:
        """Return resumable work; RECORDING must first be marked orphaned/interrupted."""
        timestamp = now or _utc_now()
        resumable = (
            SessionStatus.WAITING.value,
            SessionStatus.CAPACITY_INSUFFICIENT.value,
            SessionStatus.AUDIENCE_PENDING.value,
            SessionStatus.TRANSCRIBING.value,
            SessionStatus.UPLOADING.value,
            SessionStatus.INTERRUPTED.value,
        )
        with self._lock:
            rows = self._conn.execute(
                f"""SELECT * FROM sessions WHERE status IN ({",".join("?" for _ in resumable)})
                    OR (status=? AND (retry_at IS NULL OR retry_at <= ?))
                    ORDER BY updated_at""",
                (*resumable, SessionStatus.FAILED.value, timestamp),
            ).fetchall()
        return [self._session_from_row(row) for row in rows]

    def add_event(
        self,
        level: str,
        component: str,
        message: str,
        *,
        session_id: str | None = None,
    ) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO events(created_at, level, component, message, session_id) VALUES(?, ?, ?, ?, ?)",
                (_utc_now(), level.upper(), component, message, session_id),
            )
            return int(cur.lastrowid)

    def recent_events(self, *, limit: int = 200) -> list[Event]:
        if limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            Event(
                id=row["id"],
                created_at=row["created_at"],
                level=row["level"],
                component=row["component"],
                message=row["message"],
                session_id=row["session_id"],
            )
            for row in rows
        ]

    def _insert_event(
        self, level: str, component: str, message: str, session_id: str, created_at: str
    ) -> None:
        self._conn.execute(
            "INSERT INTO events(created_at, level, component, message, session_id) VALUES(?, ?, ?, ?, ?)",
            (created_at, level, component, message, session_id),
        )

    def _session_from_row(self, row: sqlite3.Row) -> LiveSession:
        with self._lock:
            phrases = self._conn.execute(
                "SELECT phrase FROM session_keywords WHERE session_id=? ORDER BY phrase COLLATE NOCASE",
                (row["session_id"],),
            ).fetchall()
        return LiveSession(
            session_id=row["session_id"],
            recording_task_id=row["recording_task_id"],
            room_id=row["room_id"],
            name=row["name"],
            url=row["url"],
            status=SessionStatus(row["status"]),
            started_at=row["started_at"],
            start_time_source=row["start_time_source"],
            identity_basis=row["identity_basis"],
            first_seen_at=row["first_seen_at"],
            keyword_phrases=tuple(item["phrase"] for item in phrases),
            audience_due_at=row["audience_due_at"],
            audience_basis=row["audience_basis"],
            qualification_minutes=row["qualification_minutes"],
            minimum_viewers=row["minimum_viewers"],
            audience_value=row["audience_value"],
            audience_raw=row["audience_raw"],
            audience_captured_at=row["audience_captured_at"],
            audience_outcome=row["audience_outcome"],
            current_viewers=row["current_viewers"],
            current_viewers_raw=row["current_viewers_raw"],
            observed_at=row["observed_at"],
            recording_started_at=row["recording_started_at"],
            recording_path=row["recording_path"],
            video_path=row["video_path"],
            audio_path=row["audio_path"],
            ended_at=row["ended_at"],
            duration_seconds=row["duration_seconds"],
            transcript_text=row["transcript_text"],
            transcript_path=row["transcript_path"],
            feishu_record_id=row["feishu_record_id"],
            error=row["error"],
            qualification_status=row["qualification_status"],
            audience_attempts=row["audience_attempts"],
            audience_retry_at=row["audience_retry_at"],
            cleanup_error=row["cleanup_error"],
            capacity_since=row["capacity_since"],
            capacity_released_at=row["capacity_released_at"],
            retry_at=row["retry_at"],
            attempt_count=row["attempt_count"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
