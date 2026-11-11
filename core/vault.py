"""加密凭证库。

Windows 上用系统 DPAPI（CryptProtectData，密文绑定当前 Windows 用户），
等价于「系统凭据库」；其它平台或 DPAPI 不可用时回退到本机密钥文件 + 认证加密。
SQLite 里只存引用与版本戳，凭证明文/密文都不进数据库表字段以外的任何地方。
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import hmac
import json
import os
import threading
import time
from pathlib import Path

from . import config


class VaultError(RuntimeError):
    pass


class VersionConflict(VaultError):
    """CAS 失败：旧结果试图覆盖新凭证。"""


# ---------------------------------------------------------------- Windows DPAPI
class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def _as_blob(data: bytes):
    buf = ctypes.create_string_buffer(data, len(data))
    blob = _DATA_BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_byte)))
    return blob, buf  # buf 必须保持引用，避免被 GC


def _dpapi_available() -> bool:
    return os.name == "nt" and hasattr(ctypes, "windll")


def _dpapi_protect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, _keep = _as_blob(data)
    out = _DATA_BLOB()
    ok = crypt32.CryptProtectData(
        ctypes.byref(src), ctypes.c_wchar_p("nte-assistant credential"),
        None, None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise VaultError("CryptProtectData 调用失败")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    src, _keep = _as_blob(data)
    out = _DATA_BLOB()
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(out)
    )
    if not ok:
        raise VaultError("CryptUnprotectData 调用失败（可能换过 Windows 用户或文件被篡改）")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


# ---------------------------------------------------------------- 非 Windows 回退
_FALLBACK_MAGIC = b"NTEF1"


def _fallback_key() -> bytes:
    config.ensure_data_dir()
    path = config.DATA_DIR / ".vault.key"
    if path.exists():
        return path.read_bytes()
    key = os.urandom(32)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(key)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def _fallback_protect(data: bytes) -> bytes:
    key = _fallback_key()
    nonce = os.urandom(16)
    stream = hashlib.shake_256(key + b"enc" + nonce).digest(len(data))
    ct = bytes(a ^ b for a, b in zip(data, stream))
    mac = hmac.new(key, _FALLBACK_MAGIC + nonce + ct, hashlib.sha256).digest()
    return _FALLBACK_MAGIC + nonce + mac + ct


def _fallback_unprotect(blob: bytes) -> bytes:
    if not blob.startswith(_FALLBACK_MAGIC):
        raise VaultError("未知密文格式")
    pos = len(_FALLBACK_MAGIC)
    nonce, mac, ct = blob[pos:pos + 16], blob[pos + 16:pos + 48], blob[pos + 48:]
    key = _fallback_key()
    expect = hmac.new(key, _FALLBACK_MAGIC + nonce + ct, hashlib.sha256).digest()
    if not hmac.compare_digest(mac, expect):
        raise VaultError("密文校验失败：文件被篡改或密钥文件丢失")
    stream = hashlib.shake_256(key + b"enc" + nonce).digest(len(ct))
    return bytes(a ^ b for a, b in zip(ct, stream))


def _protect(data: bytes) -> tuple[bytes, str]:
    if _dpapi_available():
        try:
            return _dpapi_protect(data), "dpapi"
        except Exception:
            pass
    return _fallback_protect(data), "fallback-hmac"


def _unprotect(blob: bytes, scheme: str) -> bytes:
    if scheme == "dpapi":
        return _dpapi_unprotect(blob)
    return _fallback_unprotect(blob)


# ---------------------------------------------------------------- Vault
class Vault:
    """vault.json 存密文；原子写 + 版本号 CAS。"""

    def __init__(self, path: Path | None = None):
        self.path = path or config.VAULT_PATH
        self._lock = threading.RLock()
        config.ensure_data_dir()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"format": 1, "entries": {}}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or not isinstance(data.get("entries"), dict):
                raise ValueError("格式错误")
            return data
        except (json.JSONDecodeError, ValueError) as exc:
            raise VaultError(f"凭证库读取失败: {exc}") from exc

    def _write(self, data: dict) -> None:
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def put(self, ref: str, plaintext: str, expected_version: int | None = None) -> int:
        """写入（或更新）凭证并返回新版本号；expected_version 用于 CAS 防旧值覆盖。"""
        with self._lock:
            data = self._read()
            entry = data["entries"].get(ref)
            current = int(entry.get("version", 0)) if entry else 0
            if expected_version is not None and current != expected_version:
                raise VersionConflict(
                    f"凭证版本冲突: 期望 {expected_version}, 实际 {current}"
                )
            blob, scheme = _protect(plaintext.encode("utf-8"))
            data["entries"][ref] = {
                "cipher": base64.b64encode(blob).decode("ascii"),
                "scheme": scheme,
                "version": current + 1,
                "updatedAt": int(time.time()),
            }
            self._write(data)
            return current + 1

    def get(self, ref: str) -> tuple[str, int] | None:
        with self._lock:
            entry = self._read()["entries"].get(ref)
        if not entry:
            return None
        try:
            blob = base64.b64decode(entry["cipher"])
            text = _unprotect(blob, entry.get("scheme", "fallback-hmac")).decode("utf-8")
        except Exception as exc:
            raise VaultError(f"凭证解密失败: {exc}") from exc
        return text, int(entry.get("version", 0))

    def version(self, ref: str) -> int:
        with self._lock:
            entry = self._read()["entries"].get(ref)
        return int(entry.get("version", 0)) if entry else 0

    def delete(self, ref: str) -> None:
        with self._lock:
            data = self._read()
            if data["entries"].pop(ref, None) is not None:
                self._write(data)

    def has(self, ref: str) -> bool:
        with self._lock:
            return ref in self._read()["entries"]
