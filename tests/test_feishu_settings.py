
import pytest

from whatnot_monitor.config import (
    FeishuSecretError,
    Settings,
    feishu_credentials,
    has_feishu_secret,
    load_settings,
    save_settings,
    set_feishu_secret,
)


def test_windows_secret_round_trip_and_environment_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "legacy-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "legacy-secret")
    settings = Settings()
    assert feishu_credentials(settings) == ("legacy-id", "legacy-secret")
    settings.feishu_app_id = "ui-id"
    settings = set_feishu_secret(settings, "example-secret-123")
    path = tmp_path / "settings.json"
    save_settings(settings, path)
    assert "example-secret-123" not in path.read_text(encoding="utf-8")
    assert "example-secret-123" not in repr(settings)
    restored = load_settings(path)
    assert has_feishu_secret(restored)
    assert feishu_credentials(restored) == ("ui-id", "example-secret-123")


def test_corrupt_secret_is_not_silently_replaced_by_environment(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_SECRET", "fallback")
    settings = Settings(feishu_app_secret_encrypted="!invalid!")
    with pytest.raises(FeishuSecretError):
        feishu_credentials(settings)
