"""Feishu Bitable API client for the existing Whatnot archive table."""

from __future__ import annotations

import os
import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol, Self
from urllib.parse import quote

import httpx

BASE_URL = "https://open.feishu.cn/open-apis"
TOKEN_URL = f"{BASE_URL}/auth/v3/tenant_access_token/internal"

# Each installation must supply its own destination.
DEFAULT_APP_TOKEN = ""
DEFAULT_TABLE_ID = ""

ROOM_NAME_FIELD = "直播间名称"
START_TIME_FIELD = "开播时间"
END_TIME_FIELD = "结束时间"
AUDIENCE_FIELD = "在线人数"
TRANSCRIPT_FIELD = "直播讲解内容"

REQUIRED_FIELDS: Mapping[str, int] = {
    ROOM_NAME_FIELD: 1,  # Text
    START_TIME_FIELD: 5,  # Date
    END_TIME_FIELD: 5,
    AUDIENCE_FIELD: 2,  # Number
    TRANSCRIPT_FIELD: 1,
}


class FeishuError(RuntimeError):
    """Base error raised by the Bitable client."""


class FeishuConfigurationError(FeishuError):
    """Missing or invalid local Feishu configuration."""


class FeishuApiError(FeishuError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        api_code: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.api_code = api_code


class FeishuSchemaError(FeishuError):
    """The configured destination table is missing expected fields/types."""


class HttpTransport(Protocol):
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response: ...


@dataclass(frozen=True, slots=True)
class FeishuConfig:
    app_id: str
    app_secret: str = field(repr=False)
    app_token: str = DEFAULT_APP_TOKEN
    table_id: str = DEFAULT_TABLE_ID
    timeout_seconds: float = 20.0

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FeishuConfig:
        values = os.environ if env is None else env
        app_id = (
            values.get("FEISHU_APP_ID") or values.get("WHATNOT_FEISHU_APP_ID") or ""
        ).strip()
        app_secret = (
            values.get("FEISHU_APP_SECRET")
            or values.get("WHATNOT_FEISHU_APP_SECRET")
            or ""
        ).strip()
        if not app_id or not app_secret:
            raise FeishuConfigurationError(
                "请配置环境变量 WHATNOT_FEISHU_APP_ID 和 WHATNOT_FEISHU_APP_SECRET。"
            )
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            app_token=(
                values.get("FEISHU_APP_TOKEN")
                or values.get("WHATNOT_FEISHU_APP_TOKEN")
                or DEFAULT_APP_TOKEN
            ).strip(),
            table_id=(
                values.get("FEISHU_TABLE_ID")
                or values.get("WHATNOT_FEISHU_TABLE_ID")
                or DEFAULT_TABLE_ID
            ).strip(),
        )


@dataclass(frozen=True, slots=True)
class ConnectionCheck:
    app_token: str
    table_id: str
    fields: Mapping[str, int]
    write_permission: str = "not tested（尚未测试；验证过程不会创建数据行）"


class FeishuBitableClient:
    def __init__(
        self, config: FeishuConfig, *, transport: HttpTransport | None = None
    ) -> None:
        if not config.app_token or not config.table_id:
            raise FeishuConfigurationError(
                "飞书应用 App Token 和数据表 ID 不能为空。"
            )
        self.config = config
        self._client = httpx.Client(
            timeout=config.timeout_seconds,
            transport=transport,
        )
        self._token: str | None = None
        self._token_expires_at = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _get_token(self, *, force_refresh: bool = False) -> str:
        if (
            self._token
            and not force_refresh
            and time.monotonic() < self._token_expires_at
        ):
            return self._token
        try:
            response = self._client.post(
                TOKEN_URL,
                json={
                    "app_id": self.config.app_id,
                    "app_secret": self.config.app_secret,
                },
            )
        except httpx.HTTPError as exc:
            raise FeishuApiError(
                f"飞书认证请求失败：{exc}"
            ) from exc
        payload = self._decode(response, "飞书认证")
        code = payload.get("code", 0)
        if code not in (0, None):
            # Deliberately do not include the request body, which contains app credentials.
            raise FeishuApiError(
                _api_message(payload, "飞书认证失败"),
                status_code=response.status_code,
                api_code=_int_or_none(code),
            )
        token = payload.get("tenant_access_token")
        if not token:
            raise FeishuApiError(
                "飞书认证响应中未包含 tenant access token。"
            )
        expires_in = max(
            60, int(payload.get("expire", payload.get("expires_in", 7200)))
        )
        self._token = str(token)
        self._token_expires_at = (
            time.monotonic() + expires_in - min(60, expires_in // 10)
        )
        return self._token

    @staticmethod
    def _decode(response: httpx.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise FeishuApiError(
                f"{operation}返回了无效的 JSON（HTTP {response.status_code}）。",
                status_code=response.status_code,
            ) from exc
        if not isinstance(payload, dict):
            raise FeishuApiError(
                f"{operation}返回了意外响应（HTTP {response.status_code}）。",
                status_code=response.status_code,
            )
        if response.is_error:
            raise FeishuApiError(
                _api_message(
                    payload, f"{operation}失败（HTTP {response.status_code}）"
                ),
                status_code=response.status_code,
                api_code=_int_or_none(payload.get("code")),
            )
        return payload

    def _api_request(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        body: Mapping[str, Any] | None = None,
        operation: str,
    ) -> dict[str, Any]:
        url = f"{BASE_URL}/{path.lstrip('/')}"
        for attempt in range(2):
            token = self._get_token(force_refresh=attempt > 0)
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=body,
                    headers={"Authorization": f"Bearer {token}"},
                )
            except httpx.HTTPError as exc:
                raise FeishuApiError(f"{operation}请求失败：{exc}") from exc
            payload = self._decode(response, operation)
            code = payload.get("code", 0)
            if code in (0, None):
                return payload
            api_error = FeishuApiError(
                _api_message(payload, f"{operation}失败"),
                status_code=response.status_code,
                api_code=_int_or_none(code),
            )
            if attempt == 0 and api_error.api_code in {99991663, 99991665, 99991671}:
                continue
            raise api_error
        raise FeishuApiError(f"{operation}在刷新认证信息后仍然失败。")

    def validate_access(self) -> ConnectionCheck:
        """Check authentication, table visibility, and the unchanged expected schema.

        This deliberately performs no write. A successful result therefore does not
        claim that the app has permission to create records.
        """
        app_token = quote(self.config.app_token, safe="")
        table_id = quote(self.config.table_id, safe="")
        actual: dict[str, int] = {}
        page_token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            params: dict[str, Any] = {"page_size": 100}
            if page_token:
                params["page_token"] = page_token
            response = self._api_request(
                "GET",
                f"bitable/v1/apps/{app_token}/tables/{table_id}/fields",
                params=params,
                operation="查询飞书字段",
            )
            data = response.get("data")
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise FeishuApiError("飞书未返回所配置数据表的字段列表。")
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("field_name"), str):
                    try:
                        actual[item["field_name"]] = int(item.get("type"))
                    except (TypeError, ValueError):
                        continue
            if not data.get("has_more", False):
                break
            next_token = data.get("page_token")
            if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
                raise FeishuApiError("飞书字段分页标记无效，无法完成数据表检查。")
            seen_tokens.add(next_token)
            page_token = next_token
        missing = [name for name in REQUIRED_FIELDS if name not in actual]
        mismatched = [
            f"{name}（期望类型 {expected}，实际类型 {actual[name]}；expected type {expected}）"
            for name, expected in REQUIRED_FIELDS.items()
            if name in actual and actual[name] != expected
        ]
        if missing or mismatched:
            parts = []
            if missing:
                parts.append("缺少字段：" + ", ".join(missing))
            if mismatched:
                parts.append("字段类型不匹配：" + "; ".join(mismatched))
            raise FeishuSchemaError(
                "现有飞书多维表格结构不匹配：" + " | ".join(parts)
            )
        return ConnectionCheck(
            app_token=self.config.app_token,
            table_id=self.config.table_id,
            fields={name: actual[name] for name in REQUIRED_FIELDS},
        )

    def create_record(
        self,
        fields: Mapping[str, Any],
        *,
        client_token: str | None = None,
    ) -> dict[str, Any]:
        """Add one row to the configured table; never creates or alters table schema."""
        if not fields:
            raise FeishuConfigurationError("至少需要提供一个多维表格字段。")
        unknown = set(fields) - set(REQUIRED_FIELDS)
        if unknown:
            raise FeishuConfigurationError(
                "未知的多维表格字段：" + ", ".join(sorted(unknown))
            )
        if client_token is None:
            client_token = str(uuid.uuid4())
        try:
            parsed_token = uuid.UUID(client_token)
        except (ValueError, AttributeError) as exc:
            raise FeishuConfigurationError(
                "client_token 必须是 UUIDv4，才能安全重试。"
            ) from exc
        if parsed_token.version != 4:
            raise FeishuConfigurationError(
                "client_token 必须是 UUIDv4，才能安全重试。"
            )

        app_token = quote(self.config.app_token, safe="")
        table_id = quote(self.config.table_id, safe="")
        response = self._api_request(
            "POST",
            f"bitable/v1/apps/{app_token}/tables/{table_id}/records",
            params={"client_token": client_token},
            body={"fields": dict(fields)},
            operation="创建飞书记录",
        )
        data = response.get("data")
        record = data.get("record") if isinstance(data, dict) else None
        if not isinstance(record, dict) or not isinstance(record.get("record_id"), str) or not record["record_id"].strip():
            raise FeishuApiError("飞书未确认记录已创建。")
        return record


def build_record_fields(
    *,
    room_name: str,
    start_time: datetime,
    end_time: datetime | None,
    audience_value: int | None,
    transcript: str,
) -> dict[str, Any]:
    """Map a finished session to the five existing columns without inventing values."""
    if not isinstance(audience_value, (int, type(None))) or isinstance(
        audience_value, bool
    ):
        raise FeishuConfigurationError(
            "在线人数必须为整数或 None（integer or None）。"
        )
    if audience_value is not None and audience_value < 0:
        raise FeishuConfigurationError(
            "在线人数不能为负数。"
        )
    fields: dict[str, Any] = {
        ROOM_NAME_FIELD: room_name,
        START_TIME_FIELD: _datetime_to_epoch_ms(start_time),
        TRANSCRIPT_FIELD: transcript,
    }
    if end_time is not None:
        fields[END_TIME_FIELD] = _datetime_to_epoch_ms(end_time)
    if audience_value is not None:
        fields[AUDIENCE_FIELD] = audience_value
    return fields


def _datetime_to_epoch_ms(value: datetime) -> int:
    if value.tzinfo is None:
        # Naive times follow the machine's configured local timezone; caller should
        # prefer timezone-aware values so the recorded instant is unambiguous.
        value = value.astimezone()
    return round(value.astimezone(UTC).timestamp() * 1000)


def _api_message(payload: Mapping[str, Any], fallback: str) -> str:
    code = payload.get("code")
    message = (
        payload.get("msg") or payload.get("error_description") or payload.get("error")
    )
    if message:
        return f"{fallback}: {message}" + (
            f"（错误代码 {code}）" if code is not None else ""
        )
    return fallback + (f"（错误代码 {code}）" if code is not None else "")


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
