"""Cleanup tests use only isolated temporary databases/files; no real recordings."""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from whatnot_monitor.config import Settings
from whatnot_monitor.runtime import MonitorRuntime
from whatnot_monitor.store import Store


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        recordings_dir=str(tmp_path / "legacy"), video_dir=str(tmp_path / "video"),
        audio_dir=str(tmp_path / "audio"), transcripts_dir=str(tmp_path / "transcripts"),
    )
    store = Store(tmp_path / "monitor.sqlite3")
    value = MonitorRuntime(store, settings)
    yield value
    value._postprocess.shutdown(wait=True)
    store.close()


def create(runtime, sid, status="WAITING", media=True):
    store = runtime.store
    store.create_session(
        sid, "seller", sid, f"https://www.whatnot.com/live/{sid}",
        started_at=None, start_time_source="first_seen_estimate",
        first_seen_at="2026-11-13T00:00:00+00:00", keyword_phrases=("toys",),
    )
    if status == "COMPLETED":
        for next_status in ("RECORDING", "TRANSCRIBING", "UPLOADING", "COMPLETED"):
            store.transition(sid, next_status)
    elif status != "WAITING":
        store.transition(sid, status)
    paths = []
    if media:
        video = Path(runtime.settings.video_dir) / sid / "video.webm"
        audio = Path(runtime.settings.audio_dir) / sid / "audio.webm"
        transcript = Path(runtime.settings.transcripts_dir) / f"{sid}.txt"
        paths = [video, audio, audio.with_suffix(".wav"), audio.with_name("audio.tmp.wav"), transcript]
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"isolated synthetic fixture")
        store.save_recording_assets(sid, video_path=str(video), audio_path=str(audio))
        store.save_recording(sid, str(audio))
        store.save_transcript(sid, "fixture", str(transcript))
    return paths


@pytest.mark.parametrize("status", ["WAITING", "CAPACITY_INSUFFICIENT", "FAILED", "INTERRUPTED", "COMPLETED"])
def test_clear_history_removes_inactive_files_and_relations_but_preserves_recording(runtime, status):
    store = runtime.store
    keyword_id = store.upsert_keyword("toys")
    deleted_paths = create(runtime, "old", status)
    live_paths = create(runtime, "live", "RECORDING")
    store.add_session_keyword_source("old", keyword_id, "toys")
    store.get_or_create_upload_token("old")
    store.add_event("info", "recording", "old-specific", session_id="old")
    store.add_event("info", "recording", "live-specific", session_id="live")
    runtime._recorders["live"] = (SimpleNamespace(video_path=live_paths[0], audio_path=live_paths[1]), object())
    result = runtime.request_clear_history()
    assert result == {"deleted": 1, "retained": 1, "failures": {}}
    assert store.get_session("old") is None
    assert store.get_session("live").status == "RECORDING"
    assert "live" in runtime._recorders
    assert all(not path.exists() for path in deleted_paths)
    assert all(path.exists() for path in live_paths)
    assert not deleted_paths[0].parent.exists()
    assert [kw.phrase for kw in store.list_keywords()] == ["toys"]
    for table in ("session_keywords", "session_keyword_sources", "upload_tokens", "events"):
        assert store._conn.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id='old'").fetchone()[0] == 0
    assert any(event.message == "live-specific" for event in store.recent_events())
    # Successful deletion is durable, not just removal of a displayed row.
    with Store(store._db_path) as reopened:
        assert reopened.get_session("old") is None
        assert reopened.get_session("live") is not None


def test_registered_outside_path_blocks_all_file_deletion_and_retains_record(runtime, tmp_path):
    paths = create(runtime, "unsafe", "FAILED")
    outside = tmp_path / "outside.webm"
    outside.write_bytes(b"must not delete")
    runtime.store.save_recording_assets("unsafe", video_path=str(outside), audio_path=str(paths[1]))
    result = runtime.request_clear_history()
    assert result["deleted"] == 0 and "unsafe" in result["failures"]
    assert outside.exists() and all(path.exists() for path in paths)
    assert runtime.store.get_session("unsafe").cleanup_error


def test_shared_active_media_is_not_deleted(runtime):
    paths = create(runtime, "live", "RECORDING")
    create(runtime, "old", "FAILED", media=False)
    runtime.store.save_recording_assets("old", video_path=str(paths[0]), audio_path=str(paths[1]))
    result = runtime.request_clear_history()
    assert result["deleted"] == 0 and "old" in result["failures"]
    assert all(path.exists() for path in paths)


def test_disk_error_keeps_row_for_retry_and_does_not_block_other_cleanup(runtime, monkeypatch):
    paths = create(runtime, "broken", "FAILED")
    other = create(runtime, "other", "FAILED")
    native_unlink = Path.unlink

    def unlink(path, *args, **kwargs):
        if path == paths[0]:
            raise PermissionError("injected locked media")
        return native_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    result = runtime.request_clear_history()
    assert result["deleted"] == 1 and "broken" in result["failures"]
    assert runtime.store.get_session("broken") is not None
    assert paths[0].exists() and all(not path.exists() for path in other)
    monkeypatch.undo()
    assert runtime.request_clear_history()["deleted"] == 1


def test_clear_history_rejects_queued_or_running_postprocessing(runtime):
    paths = create(runtime, "old", "FAILED")
    release = threading.Event()
    future = runtime._submit_postprocess(lambda: release.wait(10))
    try:
        with pytest.raises(RuntimeError, match="后台检查尚未结束"):
            runtime.request_clear_history()
        assert all(path.exists() for path in paths)
        assert runtime.store.get_session("old") is not None
    finally:
        release.set()
        future.result(timeout=5)
    assert runtime.request_clear_history()["deleted"] == 1


def test_running_owner_queues_cleanup_even_when_paused_without_touching_files(runtime):
    paths = create(runtime, "old", "FAILED")
    runtime._thread = SimpleNamespace(is_alive=lambda: True)
    runtime._paused.set()
    assert runtime.request_clear_history() is None
    assert runtime._clear_history_requested.is_set()
    assert all(path.exists() for path in paths)
    runtime._stop.set()
    with pytest.raises(RuntimeError, match="正在停止"):
        runtime.request_clear_history()


def test_live_recorder_id_protects_transitional_db_status(runtime):
    paths = create(runtime, "starting", "WAITING")
    runtime._recorders["starting"] = (SimpleNamespace(video_path=paths[0], audio_path=paths[1]), object())
    assert runtime.request_clear_history() == {"deleted": 0, "retained": 1, "failures": {}}
    assert all(path.exists() for path in paths)


def test_store_clears_more_than_ui_limit(runtime):
    store = runtime.store
    with store._conn:
        store._conn.executemany(
            """INSERT INTO sessions(session_id,recording_task_id,room_id,name,url,status,
               start_time_source,first_seen_at,created_at,updated_at)
               VALUES(?,?, 'seller','old','https://www.whatnot.com/live/old','WAITING',
               'first_seen_estimate','2026-11-13','2026-11-13','2026-11-13')""",
            ((f"show-{i}", f"task-{i}") for i in range(5001)),
        )
    result = store.clear_non_recording_sessions(lambda *_: None)
    assert result == {"deleted": 5001, "retained": 0, "failures": {}}
    assert store.list_sessions() == []


def test_absent_media_and_legacy_only_paths_are_safe(runtime):
    paths = create(runtime, "legacy", "FAILED")
    runtime.store.save_recording_assets("legacy", video_path=None, audio_path=None)
    for path in paths:
        path.unlink()
    assert runtime.request_clear_history()["deleted"] == 1


def test_unrelated_file_in_task_folder_is_not_swept(runtime):
    paths = create(runtime, "old", "FAILED")
    unrelated = paths[0].parent / "keep.txt"
    unrelated.write_text("unregistered", encoding="utf-8")
    assert runtime.request_clear_history()["deleted"] == 1
    assert unrelated.read_text(encoding="utf-8") == "unregistered"


def test_rejected_transcript_path_does_not_delete_media(runtime, tmp_path):
    paths = create(runtime, "old", "FAILED")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    runtime.store.save_transcript("old", "keep", str(outside))
    result = runtime.request_clear_history()
    assert "old" in result["failures"]
    assert all(path.exists() for path in paths) and outside.exists()
