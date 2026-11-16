from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from whatnot_monitor.config import Settings
from whatnot_monitor.integrations.asr import (
    ASRUnavailableError,
    FasterWhisperTranscriber,
    TranscriptionError,
    TranscriptResult,
    TranscriptSegment,
)
from whatnot_monitor.integrations.feishu import (
    AUDIENCE_FIELD,
    END_TIME_FIELD,
    REQUIRED_FIELDS,
    ROOM_NAME_FIELD,
    START_TIME_FIELD,
    TOKEN_URL,
    TRANSCRIPT_FIELD,
    FeishuApiError,
    FeishuBitableClient,
    FeishuConfig,
    FeishuConfigurationError,
    FeishuSchemaError,
    build_record_fields,
)

DEFAULT_APP_TOKEN = "test-base-token"
DEFAULT_TABLE_ID = "test-table-id"


def _success(data: dict) -> httpx.Response:
    return httpx.Response(200, json={"code": 0, "msg": "success", "data": data})


def _schema_items() -> list[dict]:
    # Observed remote schema, intentionally independent of REQUIRED_FIELDS.
    return [
        {"field_name": "直播间名称", "type": 1},
        {"field_name": "开播时间", "type": 5},
        {"field_name": "结束时间", "type": 5},
        {"field_name": "在线人数", "type": 2},
        {"field_name": "直播讲解内容", "type": 1},
    ]


def _transport(
    *,
    fields: list[dict] | None = None,
    record: dict | None = None,
    seen: list | None = None,
):
    requests = seen if seen is not None else []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(TOKEN_URL):
            assert json.loads(request.content) == {
                "app_id": "test-app",
                "app_secret": "test-secret",
            }
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-test", "expire": 7200}
            )
        if request.url.path.endswith(f"/tables/{DEFAULT_TABLE_ID}"):
            return _success({"table": {"table_id": DEFAULT_TABLE_ID}})
        if request.url.path.endswith(f"/tables/{DEFAULT_TABLE_ID}/fields"):
            return _success(
                {
                    "items": fields if fields is not None else _schema_items(),
                    "has_more": False,
                }
            )
        if request.url.path.endswith(f"/tables/{DEFAULT_TABLE_ID}/records"):
            return _success({"record": record or {"record_id": "rec-test"}})
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    return httpx.MockTransport(handler)


def _client(transport: httpx.MockTransport) -> FeishuBitableClient:
    return FeishuBitableClient(
        FeishuConfig(app_id="test-app", app_secret="test-secret", app_token=DEFAULT_APP_TOKEN, table_id=DEFAULT_TABLE_ID),
        transport=transport,
    )


def test_transcript_is_timestamped_and_saved_without_touching_audio(
    tmp_path: Path,
) -> None:
    audio = tmp_path / "source.wav"
    audio.write_bytes(b"audio fixture")
    destination = tmp_path / "nested" / "transcript.txt"
    result = TranscriptResult(
        segments=(
            TranscriptSegment(0.0, 1.27, "  Hello, world. "),
            TranscriptSegment(60.4, 62.0, "Price is $20."),
        ),
        language="en",
    )

    written = result.write_txt(destination)

    assert written.read_text(encoding="utf-8") == (
        "[00:00:00.000 - 00:00:01.270] Hello, world.\n"
        "[00:01:00.400 - 00:01:02.000] Price is $20.\n"
    )
    assert audio.read_bytes() == b"audio fixture"


def test_transcriber_reports_missing_file_before_loading_optional_dependency(
    tmp_path: Path,
) -> None:
    transcriber = FasterWhisperTranscriber(tmp_path / "model")

    with pytest.raises(TranscriptionError, match="does not exist"):
        transcriber.transcribe(tmp_path / "missing.wav")


def test_transcriber_has_clear_error_when_faster_whisper_is_not_installed(
    tmp_path: Path, monkeypatch,
) -> None:
    import sys

    # Exercise a missing optional dependency even on machines that installed it.
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    model = tmp_path / "model"
    model.mkdir()
    source = tmp_path / "audio.wav"
    source.write_bytes(b"audio")

    transcriber = FasterWhisperTranscriber(model)
    with pytest.raises(ASRUnavailableError, match="faster-whisper is not installed"):
        transcriber.transcribe(source)


def test_feishu_schema_check_does_not_create_rows() -> None:
    requests: list[httpx.Request] = []
    client = _client(_transport(seen=requests))
    try:
        result = client.validate_access()
    finally:
        client.close()

    assert result.app_token == DEFAULT_APP_TOKEN
    assert result.table_id == DEFAULT_TABLE_ID
    assert result.write_permission.startswith("not tested")
    assert result.fields["在线人数"] == 2
    assert "开播30分钟在线人数" not in result.fields
    assert Settings().feishu_field_names == tuple(result.fields)
    assert all(not request.url.path.endswith("/records") for request in requests)
    assert all(not request.url.path.endswith(f"/tables/{DEFAULT_TABLE_ID}") for request in requests)
    assert any(request.url.path.endswith("/fields") for request in requests)


def test_feishu_schema_reads_all_field_pages() -> None:
    requests: list[httpx.Request] = []
    fields = _schema_items()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url == httpx.URL(TOKEN_URL):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-test"})
        assert request.url.path.endswith(f"/tables/{DEFAULT_TABLE_ID}/fields")
        if request.url.params.get("page_token") == "next-page":
            return _success({"items": fields[2:], "has_more": False})
        return _success({"items": fields[:2], "has_more": True, "page_token": "next-page"})

    with _client(httpx.MockTransport(handler)) as client:
        result = client.validate_access()
    assert result.fields == REQUIRED_FIELDS
    assert sum(request.url.path.endswith("/fields") for request in requests) == 2


def test_feishu_schema_rejects_repeated_pagination_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(TOKEN_URL):
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t-test"})
        return _success({"items": [], "has_more": True, "page_token": "repeat"})

    with _client(httpx.MockTransport(handler)) as client, pytest.raises(
        FeishuApiError, match="分页"
    ):
        client.validate_access()


def test_feishu_schema_validation_reports_missing_or_wrong_fields() -> None:
    fields = [
        {"field_name": name, "type": field_type}
        for name, field_type in REQUIRED_FIELDS.items()
        if name != AUDIENCE_FIELD
    ]
    fields.append({"field_name": START_TIME_FIELD, "type": 1})
    client = _client(_transport(fields=fields))
    try:
        with pytest.raises(FeishuSchemaError) as exc:
            client.validate_access()
    finally:
        client.close()
    assert AUDIENCE_FIELD in str(exc.value)
    assert "expected type 5" in str(exc.value)


def test_build_record_fields_uses_ms_and_omits_unknown_audience() -> None:
    started = datetime(2026, 11, 11, 9, 30, tzinfo=UTC)
    ended = datetime(2026, 11, 11, 10, 20, tzinfo=UTC)

    fields = build_record_fields(
        room_name="Room A",
        start_time=started,
        end_time=ended,
        audience_value=None,
        transcript="Original transcript",
    )

    assert fields[ROOM_NAME_FIELD] == "Room A"
    assert fields[START_TIME_FIELD] == 1_794_389_400_000
    assert fields[END_TIME_FIELD] == 1_794_392_400_000
    assert AUDIENCE_FIELD not in fields
    assert fields[TRANSCRIPT_FIELD] == "Original transcript"


def test_build_record_fields_omits_unknown_show_end_time() -> None:
    fields = build_record_fields(
        room_name="Room A", start_time=datetime(2026, 11, 11, tzinfo=UTC),
        end_time=None, audience_value=42, transcript="筛选未达标",
    )
    assert END_TIME_FIELD not in fields
    assert fields[AUDIENCE_FIELD] == 42


@pytest.mark.parametrize("value", [1.5, True, False, "80", -1])
def test_build_record_fields_rejects_invalid_audience(value) -> None:
    with pytest.raises(FeishuConfigurationError):
        build_record_fields(
            room_name="Room A",
            start_time=datetime.now(UTC),
            end_time=datetime.now(UTC),
            audience_value=value,
            transcript="",
        )


@pytest.mark.parametrize("value", [0, 80])
def test_build_record_fields_maps_audience_to_observed_number_column(value) -> None:
    fields = build_record_fields(
        room_name="Room A",
        start_time=datetime.now(UTC),
        end_time=datetime.now(UTC),
        audience_value=value,
        transcript="Original transcript",
    )
    assert fields["在线人数"] == value
    assert "开播30分钟在线人数" not in fields
    assert fields["直播讲解内容"] == "Original transcript"


def test_create_record_uses_persistable_uuidv4_idempotency_key() -> None:
    requests: list[httpx.Request] = []
    client = _client(_transport(seen=requests))
    client_token = str(uuid.uuid4())
    fields = {ROOM_NAME_FIELD: "Room A", TRANSCRIPT_FIELD: "Transcript"}
    try:
        record = client.create_record(fields, client_token=client_token)
    finally:
        client.close()

    request = next(item for item in requests if item.url.path.endswith("/records"))
    assert request.url.params["client_token"] == client_token
    assert json.loads(request.content) == {"fields": fields}
    assert record["record_id"] == "rec-test"


def test_create_record_rejects_non_uuid4_retry_token() -> None:
    client = _client(_transport())
    try:
        with pytest.raises(FeishuConfigurationError, match="UUIDv4"):
            client.create_record(
                {ROOM_NAME_FIELD: "Room A"}, client_token=str(uuid.uuid1())
            )
    finally:
        client.close()


def test_feishu_api_error_surfaces_missing_permission() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url == httpx.URL(TOKEN_URL):
            return httpx.Response(
                200, json={"code": 0, "tenant_access_token": "t-test", "expire": 7200}
            )
        return httpx.Response(403, json={"code": 1254302, "msg": "Permission denied."})

    client = _client(httpx.MockTransport(handler))
    try:
        with pytest.raises(FeishuApiError) as exc:
            client.validate_access()
    finally:
        client.close()
    assert "Permission denied" in str(exc.value)
    assert exc.value.status_code == 403
    assert exc.value.api_code == 1254302


def test_config_reads_secrets_from_environment_without_repr_leak() -> None:
    config = FeishuConfig.from_env(
        {
            "WHATNOT_FEISHU_APP_ID": "cli-test",
            "WHATNOT_FEISHU_APP_SECRET": "secret-value",
            "WHATNOT_FEISHU_APP_TOKEN": DEFAULT_APP_TOKEN,
            "WHATNOT_FEISHU_TABLE_ID": DEFAULT_TABLE_ID,
        }
    )
    assert config.app_token == DEFAULT_APP_TOKEN
    assert config.table_id == DEFAULT_TABLE_ID
    assert "secret-value" not in repr(config)


@pytest.mark.parametrize("record", [{}, {"record_id": ""}, {"record_id": "  "}, {"record_id": 42}])
def test_create_record_requires_nonempty_record_id(monkeypatch, record):
    with _client(_transport()) as client:
        monkeypatch.setattr(client, "_api_request", lambda *_args, **_kwargs: {"data": {"record": record}})
        with pytest.raises(FeishuApiError, match="未确认"):
            client.create_record({ROOM_NAME_FIELD: "room"})
