from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from whatnot_monitor.models import SessionStatus
from whatnot_monitor.store import Store


def make_store(tmp_path):
    return Store(tmp_path / "database" / "monitor.sqlite3")


def create_session(store, session_id="show-1", room_id="seller-1", **overrides):
    values = {
        "session_id": session_id,
        "room_id": room_id,
        "name": "Seller live",
        "url": f"https://www.whatnot.com/show/{session_id}",
        "started_at": None,
        "start_time_source": "first_seen_estimate",
        "first_seen_at": "2026-11-11T10:00:00+00:00",
        "keyword_phrases": ("cards",),
        "audience_due_at": "2026-11-11T10:30:00+00:00",
    }
    values.update(overrides)
    return store.create_session(**values)


def test_keyword_crud_and_duplicate_upsert(tmp_path):
    store = make_store(tmp_path)
    try:
        keyword_id = store.upsert_keyword("Pokemon")
        assert store.upsert_keyword("pokemon", enabled=False) == keyword_id
        assert len(store.list_keywords()) == 1
        assert store.list_keywords()[0].enabled is False
        store.upsert_keyword("Sports cards")
        store.set_keyword_enabled(keyword_id, True)
        assert [kw.phrase for kw in store.list_keywords(enabled_only=True)] == [
            "Pokemon",
            "Sports cards",
        ]
        store.delete_keyword(keyword_id)
        assert [kw.phrase for kw in store.list_keywords()] == ["Sports cards"]
    finally:
        store.close()


def test_session_idempotency_and_same_room_can_have_distinct_sessions(tmp_path):
    store = make_store(tmp_path)
    try:
        first, created = create_session(store)
        duplicate, duplicate_created = create_session(store, name="Changed title")
        next_session, next_created = create_session(store, session_id="show-2")
        assert created is True
        assert duplicate_created is False
        assert duplicate.name == "Seller live"
        assert duplicate.recording_task_id == first.recording_task_id
        assert next_created is True
        assert next_session.room_id == first.room_id
        assert next_session.session_id != first.session_id
        assert next_session.recording_task_id != first.recording_task_id
        assert len(store.list_sessions()) == 2
        assert store.find_active_session_for_room("seller-1").session_id == "show-2"
        assert first.id == first.session_id
    finally:
        store.close()


def test_migration_repairs_live_viewer_badge_names_using_seller_handle(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    store = Store(db)
    session, _ = create_session(
        store,
        session_id="show-badge",
        room_id="whatnot-seller:socalluv2",
        name="Live · 124",
    )
    assert session.name == "Live · 124"
    store.close()

    reopened = Store(db)
    try:
        backups = list((tmp_path / "backups").glob("monitor-before-migration-*.sqlite3"))
        assert backups
        repaired = reopened.get_session("show-badge")
        assert repaired is not None
        assert repaired.name == "socalluv2"
        assert repaired.status == SessionStatus.WAITING
    finally:
        reopened.close()


def test_session_persists_artifacts_audience_and_events_across_reopen(tmp_path):
    db = tmp_path / "monitor.sqlite3"
    store = Store(db)
    session, _ = create_session(store)
    store.update_live_observation(
        session.session_id,
        current_viewers=1250,
        current_viewers_raw="1.25K",
        observed_at="2026-11-11T10:02:00+00:00",
    )
    store.save_recording_assets(
        session.session_id,
        video_path="recordings/video/show-1.webm",
        audio_path="recordings/audio/show-1.webm",
    )
    store.save_recording(session.session_id, "recordings/show-1.wav")
    store.save_recording_started(session.session_id, "2026-11-11T10:01:00+00:00")
    store.save_audience(
        session.session_id,
        value=1250,
        raw_value="1,250",
        captured_at="2026-11-11T10:30:05+00:00",
    )
    store.transition(session.session_id, SessionStatus.RECORDING)
    store.transition(session.session_id, SessionStatus.TRANSCRIBING)
    store.save_transcript(
        session.session_id, "Original words.", "transcripts/show-1.txt"
    )
    store.add_event("warning", "test", "persisted event", session_id=session.session_id)
    store.close()

    reopened = Store(db)
    try:
        restored = reopened.get_session(session.session_id)
        assert restored is not None
        assert restored.status == SessionStatus.TRANSCRIBING
        assert restored.recording_path == "recordings/show-1.wav"
        assert restored.video_path == "recordings/video/show-1.webm"
        assert restored.audio_path == "recordings/audio/show-1.webm"
        assert restored.recording_started_at == "2026-11-11T10:01:00+00:00"
        assert restored.current_viewers == 1250
        assert restored.audience_value == 1250
        assert restored.qualification_status == "pending"
        assert restored.transcript_text == "Original words."
        assert reopened.recent_events(limit=1)[0].message == "persisted event"
        assert reopened.recoverable_sessions()[0].session_id == session.session_id
    finally:
        reopened.close()


def test_audience_due_query_returns_unresolved_due_sessions_only(tmp_path):
    store = make_store(tmp_path)
    try:
        due_at = datetime.now(UTC) - timedelta(minutes=1)
        create_session(store, audience_due_at=due_at.isoformat())
        store.transition("show-1", "RECORDING")
        future = datetime.now(UTC) + timedelta(minutes=5)
        create_session(store, session_id="show-2", audience_due_at=future.isoformat())
        due = store.sessions_due_for_audience(datetime.now(UTC).isoformat())
        assert [session.session_id for session in due] == ["show-1"]
        store.save_audience(
            "show-1", value=None, raw_value=None, captured_at=None, outcome="missed"
        )
        assert store.sessions_due_for_audience(datetime.now(UTC).isoformat()) == []
    finally:
        store.close()


def test_audience_attempts_and_retry_deadline_are_durable(tmp_path):
    db = tmp_path / "audience.sqlite3"
    store = Store(db)
    due = datetime.now(UTC) - timedelta(minutes=1)
    session, _ = create_session(store, audience_due_at=due.isoformat())
    store.transition(session.session_id, "RECORDING")
    try:
        assert [row.session_id for row in store.sessions_due_for_audience()] == [
            session.session_id
        ]
        attempt = store.begin_audience_attempt(session.session_id)
        retry_at = (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
        store.record_audience_failure(
            session.session_id,
            attempt=attempt,
            retry_at=retry_at,
            exhausted=False,
            captured_at=datetime.now(UTC).isoformat(),
        )
        deferred = store.get_session(session.session_id)
        assert deferred.audience_attempts == 1
        assert deferred.audience_retry_at == retry_at
        assert deferred.qualification_status == "pending"
        assert store.sessions_due_for_audience() == []
    finally:
        store.close()

    reopened = Store(db)
    try:
        restored = reopened.get_session(session.session_id)
        assert restored is not None
        assert restored.audience_attempts == 1
        assert restored.audience_retry_at == retry_at
    finally:
        reopened.close()


def test_invalid_transition_is_rejected_and_failed_work_can_retry(tmp_path):
    store = make_store(tmp_path)
    try:
        session, _ = create_session(store)
        with pytest.raises(ValueError, match="Invalid session transition"):
            store.transition(session.session_id, "COMPLETED")
        failed = store.transition(session.session_id, "FAILED", error="temporary")
        assert failed.status == SessionStatus.FAILED
        retried = store.transition(session.session_id, "TRANSCRIBING")
        assert retried.status == SessionStatus.TRANSCRIBING
        assert retried.error is None
    finally:
        store.close()


def test_upload_client_token_is_stable_and_unique_per_session(tmp_path):
    store = make_store(tmp_path)
    create_session(store)
    create_session(store, session_id="show-2")
    first = store.get_or_create_upload_token("show-1")
    assert store.get_or_create_upload_token("show-1") == first
    second = store.get_or_create_upload_token("show-2")
    assert second != first
    store.close()

    reopened = Store(tmp_path / "database" / "monitor.sqlite3")
    try:
        assert reopened.get_or_create_upload_token("show-1") == first
    finally:
        reopened.close()


def test_keyword_scan_lifecycle_is_persisted_and_aggregated(tmp_path):
    store = make_store(tmp_path)
    try:
        keyword_id = store.upsert_keyword("Trading cards")
        began = "2026-11-11T10:00:00+00:00"
        ended = "2026-11-11T10:00:07+00:00"
        store.begin_keyword_scan(keyword_id, began)
        scanning = store.list_keywords()[0]
        assert scanning.scan_status == "SCANNING"
        assert scanning.scan_started_at == began
        store.finish_keyword_scan(
            keyword_id,
            started_at=began,
            ended_at=ended,
            result="Found 3 live rooms",
            discovered_count=3,
            recording_count=2,
        )
        keyword = store.list_keywords()[0]
        assert keyword.scan_started_at is None
        assert keyword.last_scanned_at == ended
        assert keyword.last_result == "Found 3 live rooms"
        assert keyword.discovered_count == 3
        assert keyword.recording_count == 2
        assert keyword.scan_status == "OK"
        assert store.keyword_scan_counts() == {
            "keyword_count": 1,
            "enabled_count": 1,
            "discovered_count": 3,
            "recording_count": 2,
            "failed_count": 0,
        }
    finally:
        store.close()


def test_capacity_insufficient_is_explicit_and_retryable(tmp_path):
    store = make_store(tmp_path)
    try:
        session, _ = create_session(store)
        limited = store.transition(
            session.session_id,
            SessionStatus.CAPACITY_INSUFFICIENT,
            error="All recorder slots are occupied",
        )
        assert limited.status == SessionStatus.CAPACITY_INSUFFICIENT
        assert limited.capacity_since
        started_capacity = limited.capacity_since
        assert limited.error == "All recorder slots are occupied"
        assert limited in store.recoverable_sessions()
        recording = store.transition(session.session_id, SessionStatus.RECORDING)
        assert recording.status == SessionStatus.RECORDING
        assert recording.capacity_since == started_capacity
        assert recording.capacity_released_at
        assert recording.capacity_released_at >= recording.capacity_since
    finally:
        store.close()


def test_existing_database_migrates_new_columns_without_losing_rows(tmp_path):
    import sqlite3

    db_path = tmp_path / "legacy.sqlite3"
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        CREATE TABLE keywords (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL COLLATE NOCASE UNIQUE,
            enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0, 1)),
            last_scanned_at TEXT,
            last_result TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        INSERT INTO keywords(phrase, enabled, created_at, updated_at)
        VALUES('legacy', 1, 'created', 'updated');
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY,
            room_id TEXT NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            status TEXT NOT NULL,
            started_at TEXT,
            start_time_source TEXT NOT NULL,
            first_seen_at TEXT NOT NULL,
            audience_due_at TEXT,
            audience_value INTEGER,
            audience_raw TEXT,
            audience_captured_at TEXT,
            audience_outcome TEXT,
            current_viewers INTEGER,
            current_viewers_raw TEXT,
            observed_at TEXT,
            recording_path TEXT,
            ended_at TEXT,
            duration_seconds REAL,
            transcript_text TEXT,
            transcript_path TEXT,
            feishu_record_id TEXT,
            error TEXT,
            retry_at TEXT,
            attempt_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE session_keywords (
            session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
            phrase TEXT NOT NULL,
            PRIMARY KEY (session_id, phrase)
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            level TEXT NOT NULL,
            component TEXT NOT NULL,
            message TEXT NOT NULL,
            session_id TEXT REFERENCES sessions(session_id) ON DELETE SET NULL
        );
        CREATE TABLE upload_tokens (
            session_id TEXT PRIMARY KEY REFERENCES sessions(session_id) ON DELETE CASCADE,
            client_token TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL
        );
        INSERT INTO sessions(session_id, room_id, name, url, status,
          start_time_source, first_seen_at, recording_path, transcript_text,
          feishu_record_id, created_at, updated_at)
        VALUES('old-show', 'seller', 'Old', 'https://example.com', 'WAITING',
          'first_seen_estimate', 'seen', 'recordings/old.webm', 'Legacy transcript',
          'rec-legacy', 'created', 'updated');
        INSERT INTO session_keywords(session_id, phrase) VALUES('old-show', 'legacy');
        INSERT INTO events(created_at, level, component, message, session_id)
          VALUES('event-time', 'INFO', 'legacy', 'preserved event', 'old-show');
        INSERT INTO upload_tokens(session_id, client_token, created_at)
          VALUES('old-show', 'token-legacy', 'created');
        """
    )
    connection.commit()
    connection.close()

    store = Store(db_path)
    try:
        backups = list((tmp_path / "backups").glob("legacy-before-migration-*.sqlite3"))
        assert len(backups) == 1
        with sqlite3.connect(backups[0]) as backup_connection:
            backup_columns = {
                row[1]
                for row in backup_connection.execute("PRAGMA table_info(sessions)")
            }
            assert "recording_task_id" not in backup_columns
            backup_session = backup_connection.execute(
                "SELECT recording_path, transcript_text FROM sessions WHERE session_id='old-show'"
            ).fetchone()
            assert backup_session == (
                "recordings/old.webm",
                "Legacy transcript",
            )

        keyword = store.list_keywords()[0]
        assert keyword.phrase == "legacy"
        assert keyword.scan_status == "NEVER"
        session = store.get_session("old-show")
        assert session is not None
        original_task_id = session.recording_task_id
        assert session.identity_basis == "legacy_fallback"
        assert session.capacity_since is None
        assert session.recording_path == "recordings/old.webm"
        assert session.audio_path == "recordings/old.webm"
        assert session.video_path is None
        assert session.qualification_status == "pending"
        assert session.transcript_text == "Legacy transcript"
        assert session.feishu_record_id == "rec-legacy"
        assert store.session_source_phrases("old-show") == ["legacy"]
        source = store.session_keyword_sources("old-show")[0]
        assert source.keyword_id == keyword.id
        assert store.get_or_create_upload_token("old-show") == "token-legacy"
        assert store.recent_events(limit=1)[0].message == "preserved event"
        assert session.recording_started_at is None
        # Reopening the already migrated database is idempotent.
        store.close()
        store = Store(db_path)
        assert len(list((tmp_path / "backups").glob("legacy-before-migration-*.sqlite3"))) == 1
        assert store.list_keywords()[0].phrase == "legacy"
        migrated = store.get_session("old-show")
        assert migrated.session_id == "old-show"
        assert migrated.recording_task_id == original_task_id
    finally:
        store.close()


def test_keyword_sources_keep_snapshot_across_rename_and_delete(tmp_path):
    store = make_store(tmp_path)
    try:
        keyword_id = store.upsert_keyword("Trading cards")
        create_session(store)
        source = store.add_session_keyword_source("show-1", keyword_id, "Trading cards")
        assert source.keyword_id == keyword_id
        assert store.session_source_phrases("show-1") == ["Trading cards"]

        store.upsert_keyword("Collectible cards", keyword_id=keyword_id)
        assert (
            store.session_keyword_sources("show-1")[0].phrase_snapshot
            == "Trading cards"
        )

        store.delete_keyword(keyword_id)
        deleted_source = store.session_keyword_sources("show-1")[0]
        assert deleted_source.keyword_id is None
        assert deleted_source.phrase_snapshot == "Trading cards"
        assert store.session_source_phrases("show-1") == ["Trading cards"]
    finally:
        store.close()


def test_capacity_interval_is_retained_after_release_and_reset_on_reentry(tmp_path):
    store = make_store(tmp_path)
    try:
        session, _ = create_session(store)
        limited = store.transition(
            session.session_id,
            SessionStatus.CAPACITY_INSUFFICIENT,
            error="At capacity",
        )
        first_start = limited.capacity_since
        released = store.transition(session.session_id, SessionStatus.RECORDING)
        first_release = released.capacity_released_at
        assert first_start and first_release and first_start <= first_release

        second_session, _ = create_session(store, session_id="show-2")
        limited_again = store.transition(
            second_session.session_id,
            SessionStatus.CAPACITY_INSUFFICIENT,
            error="At capacity again",
        )
        assert limited_again.capacity_since >= first_release
        assert limited_again.capacity_released_at is None
    finally:
        store.close()
