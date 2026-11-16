from __future__ import annotations

import base64
import ctypes
import json
import os
import tempfile
import uuid
from ctypes import wintypes
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = APP_ROOT / "config" / "settings.json"


@dataclass
class Settings:
    poll_interval_seconds: int = 300
    max_concurrent_recordings: int = 10
    recordings_dir: str = str(APP_ROOT / "recordings")
    video_recording_enabled: bool = True
    audio_recording_enabled: bool = True
    qualification_minutes: int = 30
    minimum_viewers: int = 100
    audience_retry_count: int = 3
    audience_retry_interval_seconds: int = 30
    video_dir: str = str(APP_ROOT / "data" / "recordings" / "video")
    audio_dir: str = str(APP_ROOT / "data" / "recordings" / "audio")
    video_mime_type: str = "video/webm;codecs=vp8,opus"
    video_bitrate_bps: int = 2_500_000
    max_recording_size_gib: int = 20
    transcripts_dir: str = str(APP_ROOT / "transcripts")
    browser_profile_dir: str = str(APP_ROOT / "browser-profile")
    browser_channel: str = "chrome"
    browser_headless: bool = False
    audio_sample_rate: int = 16000
    audio_channels: int = 1
    asr_model_path: str = ""
    asr_device: str = "auto"
    disk_warning_gib: int = 10
    feishu_app_token: str = ""
    feishu_table_id: str = ""
    feishu_app_id: str = ""
    feishu_app_secret_encrypted: str = field(default="", repr=False)
    feishu_field_names: tuple[str, ...] = (
        "直播间名称",
        "开播时间",
        "结束时间",
        "在线人数",
        "直播讲解内容",
    )


def load_settings(path: Path = CONFIG_PATH) -> Settings:
    if not path.exists():
        settings = Settings()
        save_settings(settings, path)
        return settings
    raw = json.loads(path.read_text(encoding="utf-8"))
    known = {field for field in Settings.__dataclass_fields__}
    values = {key: value for key, value in raw.items() if key in known}
    if "feishu_field_names" in values:
        values["feishu_field_names"] = tuple(values["feishu_field_names"])
    return Settings(**values)


def save_settings(settings: Settings, path: Path = CONFIG_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".settings-", suffix=".tmp", delete=False) as f:
            name = f.name
            f.write(json.dumps(asdict(settings), ensure_ascii=False, indent=2) + "\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
    finally:
        if name and os.path.exists(name):
            os.unlink(name)


class FeishuSecretError(RuntimeError):
    """A saved secret cannot be securely stored or opened."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data: bytes, *, decrypt: bool) -> bytes:
    if os.name != "nt":
        raise FeishuSecretError("飞书密钥保护需要 Windows 当前用户账户。")
    buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    input_blob = _DataBlob(len(data), buffer)
    output_blob = _DataBlob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        func = crypt32.CryptUnprotectData
        func.argtypes = [ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_DataBlob)]
    else:
        func = crypt32.CryptProtectData
        func.argtypes = [ctypes.POINTER(_DataBlob), wintypes.LPCWSTR, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                         ctypes.POINTER(_DataBlob)]
    func.restype = wintypes.BOOL
    if not func(ctypes.byref(input_blob), None, None, None, None, 1, ctypes.byref(output_blob)):
        raise FeishuSecretError("无法保护或读取飞书密钥，请在原 Windows 用户账户下重新配置。")
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


_KEYRING_PREFIX = "keyring:v1:"
_KEYRING_SERVICE = "whatnot-monitor.feishu"


_TRUSTED_KEYRING_MODULES = {
    "keyring.backends.SecretService",
    "keyring.backends.libsecret",
    "keyring.backends.kwallet",
    "keyring.backends.macOS",
}


class _TrustedKeyrings:
    """Call trusted stores directly, never the chain's arbitrary backends."""

    def __init__(self, backends):
        self.backends = backends

    def set_password(self, service, reference, secret):
        for backend in self.backends:
            try:
                backend.set_password(service, reference, secret)
                return
            except Exception:  # noqa: BLE001, S112 - isolated OS keyring implementations
                continue
        raise FeishuSecretError("无法保存密钥到系统密钥环；请确认密钥环已解锁。")

    def get_password(self, service, reference):
        errors = False
        for backend in self.backends:
            try:
                secret = backend.get_password(service, reference)
            except Exception:  # noqa: BLE001 - continue only among trusted backends
                errors = True
                continue
            if secret is not None:
                return secret
        if errors:
            raise FeishuSecretError("无法读取系统密钥环中的飞书密钥。")
        return None


def _system_keyring():
    """Require OS-backed stores; never call a plaintext or unknown backend."""
    try:
        import keyring
        backend = keyring.get_keyring()
        module = type(backend).__module__
        candidates = list(backend.backends) if module == "keyring.backends.chainer" else [backend]
        # The selected backend can change between restarts. Search other viable
        # OS-backed stores on reads, while rejecting every untrusted backend.
        registry = getattr(keyring, "backend", None)
        if registry is not None and callable(getattr(registry, "get_all_keyring", None)):
            candidates.extend(registry.get_all_keyring())
        trusted = []
        seen_types = set()
        for candidate in candidates:
            backend_type = type(candidate)
            if backend_type.__module__ in _TRUSTED_KEYRING_MODULES and backend_type not in seen_types:
                trusted.append(candidate)
                seen_types.add(backend_type)
        if not trusted:
            raise FeishuSecretError("没有可用的系统密钥环；请启用系统密钥环或使用环境变量。")
        return _TrustedKeyrings(trusted)
    except FeishuSecretError:
        raise
    except Exception:  # noqa: BLE001 - keyring backends may raise platform-specific errors
        raise FeishuSecretError("无法使用系统密钥环；请安装 keyring 并启用系统密钥环。") from None


def _is_windows() -> bool:
    return os.name == "nt"


def set_feishu_secret(settings: Settings, secret: str) -> Settings:
    if not secret:
        return replace(settings, feishu_app_secret_encrypted="")
    if _is_windows():
        stored = base64.b64encode(_dpapi(secret.encode("utf-8"), decrypt=False)).decode("ascii")
    else:
        # A fresh identifier ensures a failed config save does not overwrite the
        # secret still referenced by the previous on-disk configuration.
        reference = uuid.uuid4().hex
        try:
            _system_keyring().set_password(_KEYRING_SERVICE, reference, secret)
        except FeishuSecretError:
            raise
        except Exception:  # noqa: BLE001 - do not expose backend internals or secrets
            raise FeishuSecretError("无法保存密钥到系统密钥环；请确认密钥环已解锁。") from None
        stored = _KEYRING_PREFIX + reference
    return replace(settings, feishu_app_secret_encrypted=stored)


def has_feishu_secret(settings: Settings) -> bool:
    return bool(settings.feishu_app_secret_encrypted or os.getenv("FEISHU_APP_SECRET", ""))


def feishu_credentials(settings: Settings | None = None) -> tuple[str, str]:
    app_id = (settings.feishu_app_id if settings else "") or os.getenv("FEISHU_APP_ID", "")
    encrypted = settings.feishu_app_secret_encrypted if settings else ""
    if encrypted.startswith(_KEYRING_PREFIX):
        reference = encrypted[len(_KEYRING_PREFIX):]
        if len(reference) != 32 or any(c not in "0123456789abcdef" for c in reference):
            raise FeishuSecretError("密钥环引用无效，请重新配置飞书密钥。")
        try:
            secret = _system_keyring().get_password(_KEYRING_SERVICE, reference)
        except FeishuSecretError:
            raise
        except Exception:  # noqa: BLE001 - do not expose backend internals or secrets
            raise FeishuSecretError("无法读取系统密钥环中的飞书密钥。") from None
        if secret is None:
            raise FeishuSecretError("系统密钥环中找不到飞书密钥，请重新配置。")
    elif encrypted:
        try:
            secret = _dpapi(base64.b64decode(encrypted, validate=True), decrypt=True).decode("utf-8")
        except (ValueError, UnicodeError) as exc:
            raise FeishuSecretError("已保存的飞书密钥无效，请重新配置。") from exc
    else:
        secret = os.getenv("FEISHU_APP_SECRET", "")
    return app_id, secret
