import json
import sys
from types import SimpleNamespace

import pytest
from whatnot_monitor import config


class FakeKeyring:
    def __init__(self):
        self.values = {}

    def set_password(self, service, reference, secret):
        self.values[(service, reference)] = secret

    def get_password(self, service, reference):
        return self.values.get((service, reference))


def test_defaults_do_not_include_user_table_identifiers():
    settings = config.Settings()
    assert settings.feishu_app_token == ""
    assert settings.feishu_table_id == ""


def test_non_windows_secret_round_trip_stores_only_reference(monkeypatch, tmp_path):
    ring = FakeKeyring()
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    monkeypatch.setattr(config, "_system_keyring", lambda: ring)
    settings = config.set_feishu_secret(config.Settings(feishu_app_id="app-id"), "sensitive-secret")
    path = tmp_path / "settings.json"
    config.save_settings(settings, path)
    saved = path.read_text(encoding="utf-8")
    assert "sensitive-secret" not in saved
    assert json.loads(saved)["feishu_app_secret_encrypted"].startswith("keyring:v1:")
    assert config.feishu_credentials(config.load_settings(path)) == ("app-id", "sensitive-secret")


def test_failed_replacement_preserves_previous_secret(monkeypatch):
    ring = FakeKeyring()
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    monkeypatch.setattr(config, "_system_keyring", lambda: ring)
    prior = config.set_feishu_secret(config.Settings(), "old-secret")
    replacement = config.set_feishu_secret(prior, "new-secret")
    assert prior.feishu_app_secret_encrypted != replacement.feishu_app_secret_encrypted
    assert config.feishu_credentials(prior)[1] == "old-secret"


def test_keyring_failure_never_saves_plaintext(monkeypatch):
    monkeypatch.setattr(config, "_is_windows", lambda: False)
    monkeypatch.setattr(config, "_system_keyring", lambda: (_ for _ in ()).throw(config.FeishuSecretError("unavailable")))
    settings = config.Settings()
    with pytest.raises(config.FeishuSecretError):
        config.set_feishu_secret(settings, "sensitive-secret")
    assert settings.feishu_app_secret_encrypted == ""


def test_untrusted_keyring_backend_is_rejected(monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: FakeKeyring()))
    with pytest.raises(config.FeishuSecretError):
        config._system_keyring()


def test_libsecret_backend_supported(monkeypatch):
    libsecret_type = type("Keyring", (FakeKeyring,), {"__module__": "keyring.backends.libsecret"})
    safe = libsecret_type()
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: safe))
    ring = config._system_keyring()
    ring.set_password("svc", "ref", "secret")
    assert ring.get_password("svc", "ref") == "secret"


def test_chain_directly_uses_only_trusted_backend_and_reads_previous_backend(monkeypatch):
    safe_type = type("Keyring", (FakeKeyring,), {"__module__": "keyring.backends.SecretService"})
    previous_type = type("Keyring", (FakeKeyring,), {"__module__": "keyring.backends.kwallet"})
    safe, previous, unsafe = safe_type(), previous_type(), FakeKeyring()
    previous.set_password("svc", "old-ref", "old-secret")
    chain_type = type("ChainerBackend", (), {"__module__": "keyring.backends.chainer"})
    chain = chain_type()
    chain.backends = [unsafe, safe]
    registry = SimpleNamespace(get_all_keyring=lambda: [unsafe, previous])
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: chain, backend=registry))
    ring = config._system_keyring()
    ring.set_password("svc", "new-ref", "new-secret")
    assert unsafe.values == {}
    assert safe.get_password("svc", "new-ref") == "new-secret"
    assert ring.get_password("svc", "old-ref") == "old-secret"


def test_chain_never_falls_back_to_insecure_store(monkeypatch):
    class Locked(FakeKeyring):
        __module__ = "keyring.backends.SecretService"

        def set_password(self, *args):
            raise RuntimeError("locked")

    unsafe = FakeKeyring()
    chain_type = type("ChainerBackend", (), {"__module__": "keyring.backends.chainer"})
    chain = chain_type()
    chain.backends = [Locked(), unsafe]
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: chain))
    with pytest.raises(config.FeishuSecretError):
        config._system_keyring().set_password("svc", "ref", "secret")
    assert unsafe.values == {}


def test_read_finds_old_store_after_selected_backend_changes(monkeypatch):
    new_type = type("Keyring", (FakeKeyring,), {"__module__": "keyring.backends.libsecret"})
    old_type = type("Keyring", (FakeKeyring,), {"__module__": "keyring.backends.kwallet"})
    current, old = new_type(), old_type()
    old.set_password("svc", "ref", "prior-secret")
    registry = SimpleNamespace(get_all_keyring=lambda: [old])
    monkeypatch.setitem(sys.modules, "keyring", SimpleNamespace(get_keyring=lambda: current, backend=registry))
    assert config._system_keyring().get_password("svc", "ref") == "prior-secret"


def test_missing_secret_reference_and_legacy_dpapi(monkeypatch):
    monkeypatch.setattr(config, "_system_keyring", lambda: FakeKeyring())
    with pytest.raises(config.FeishuSecretError):
        config.feishu_credentials(config.Settings(feishu_app_secret_encrypted="keyring:v1:" + "a" * 32))
    monkeypatch.setattr(config, "_dpapi", lambda data, *, decrypt: b"legacy-secret")
    assert config.feishu_credentials(config.Settings(feishu_app_secret_encrypted="YWJj"))[1] == "legacy-secret"


def test_environment_fallback(monkeypatch):
    monkeypatch.setenv("FEISHU_APP_ID", "environment-id")
    monkeypatch.setenv("FEISHU_APP_SECRET", "environment-secret")
    assert config.feishu_credentials(config.Settings()) == ("environment-id", "environment-secret")
