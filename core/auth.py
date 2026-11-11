"""授权：手机号 + 短信验证码 → 用户中心 V2 登录 → 加密落库。

硬性约束：
- 验证码只在内存里，不落盘、不进日志；
- 一次只允许一个进行中的授权会话，避免并发触发短信风控；同号未过期时**复用**
  该会话（页面误关闭/刷新后可继续输入，不必重发短信）；
- 同一手机号 60 秒内不重复发送；
- 账号表只存掩码手机号，凭证进加密库，DB 只留引用与版本戳。
"""
from __future__ import annotations

import json
import re
import secrets
import threading
import time
import uuid
from pathlib import Path

from .adapter import AdapterError
from .logutil import phone_mask
from .vault import VaultError

PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
SESSION_TTL = 10 * 60
RESEND_COOLDOWN = 60


class AuthError(RuntimeError):
    pass


class AuthManager:
    def __init__(self, store, vault, adapter, logger):
        self.store = store
        self.vault = vault
        self.adapter = adapter
        self.log = logger
        self._lock = threading.RLock()
        self._sessions: dict[str, dict] = {}
        self._last_sent: dict[str, float] = {}

    # ------------------------------------------------------------ 会话
    def _prune(self, now: float) -> None:
        for sid, sess in list(self._sessions.items()):
            if now - sess["createdAt"] > SESSION_TTL:
                self._sessions.pop(sid, None)

    def pending(self) -> dict | None:
        """当前进程中还没过期的授权会话（用于页面刷新/误关闭弹窗后接着输入）。

        之前会话只存在浏览器变量里，误点弹窗外就彻底丢了 sessionId，而服务端
        仍占着这个会话，用户只能干等 10 分钟才能重发 —— 这里把会话状态暴露出来，
        页面重新打开时可以直接续上，不用重发短信。
        """
        with self._lock:
            now = time.time()
            self._prune(now)
            if not self._sessions:
                return None
            sid, sess = next(iter(self._sessions.items()))
            sent_at = self._last_sent.get(sess["phone"], sess["createdAt"])
            return {
                "sessionId": sid,
                "phoneMask": phone_mask(sess["phone"]),
                "expiresIn": max(0, int(SESSION_TTL - (now - sess["createdAt"]))),
                "cooldownSec": max(0, int(RESEND_COOLDOWN - (now - sent_at))),
            }

    def start(self, phone: str, *, force: bool = False) -> dict:
        phone = re.sub(r"\D", "", phone or "")
        if not PHONE_RE.match(phone):
            raise AuthError("手机号格式不正确")
        with self._lock:
            now = time.time()
            self._prune(now)
            # 同一号码还在有效期内：直接复用，不重发短信。
            # 原来这里无条件报「已有一个授权会话在进行中」，用户误点弹窗外丢了
            # sessionId 之后就被自己的会话挡住 10 分钟，纯属自锁。
            live = [sid for sid, s in self._sessions.items() if s["phone"] == phone]
            if live and not force:
                sid = live[-1]
                sess = self._sessions[sid]
                expires = max(0, int(SESSION_TTL - (now - sess["createdAt"])))
                self.log.info("auth.resume", f"复用未过期的授权会话（{phone_mask(phone)}，剩余 {expires}s）")
                return {"sessionId": sid, "phoneMask": phone_mask(phone),
                        "resumed": True, "cooldownSec": 0, "expiresIn": expires}
            # 其它号码的残留会话不该挡住当前号码；force 则连同号的一起废掉
            for sid, s in list(self._sessions.items()):
                if force or s["phone"] != phone:
                    self._sessions.pop(sid, None)
            last = self._last_sent.get(phone, 0)
            wait = RESEND_COOLDOWN - (now - last)
            if wait > 0:
                raise AuthError(f"发送过于频繁，请 {int(wait) + 1} 秒后再试")
            device_id = uuid.uuid4().hex
            self.adapter.send_captcha(phone, device_id)
            self._last_sent[phone] = now
            sid = secrets.token_urlsafe(16)
            self._sessions[sid] = {
                "phone": phone, "deviceId": device_id, "createdAt": now,
            }
        self.log.info("auth.send", f"验证码已发送至 {phone_mask(phone)}")
        return {"sessionId": sid, "phoneMask": phone_mask(phone),
                "resumed": False, "cooldownSec": RESEND_COOLDOWN,
                "expiresIn": int(SESSION_TTL)}

    def complete(self, session_id: str, code: str, *, skip_check: bool = False,
                 probe_v1: bool = False, remark: str = "") -> dict:
        with self._lock:
            sess = self._sessions.get(session_id)
        if not sess:
            raise AuthError("授权会话不存在或已过期，请重新发送验证码")
        code = (code or "").strip()
        if not re.match(r"^\d{4,8}$", code):
            raise AuthError("验证码格式不正确")
        phone, device_id = sess["phone"], sess["deviceId"]

        tiger_token, tiger_uid, variant = self.adapter.sms_login(
            phone, code, device_id, skip_check=skip_check
        )
        # V2 是 1.2.8 当前协议；V1 对照默认不做（避免用同一票据额外试探登录端点）
        login = self.adapter.uc_login(tiger_token, tiger_uid, device_id, protocol="v2")
        if probe_v1:
            try:
                v1 = self.adapter.uc_login(tiger_token, tiger_uid, device_id, protocol="v1")
                if v1.get("refreshToken"):
                    login["refreshToken"] = v1["refreshToken"]
                    self.log.info("auth.v1", "V1 对照返回了 refreshToken")
            except AdapterError as exc:
                self.log.warn("auth.v1", f"V1 对照不可用：{exc.kind}")
        roles = self.adapter.get_game_roles(login["accessToken"], login["uid"], device_id)
        role = roles[0] if roles else {}
        if not role.get("roleId"):
            self.log.warn("auth.roles", "未取到异环角色，roleId 留空，稍后刷新会重试")

        account = self._persist(
            uid=login["uid"],
            access_token=login["accessToken"],
            refresh_token=login.get("refreshToken") or "",
            device_id=device_id,
            role=role,
            phone_mask=phone_mask(phone),
            remark=remark or phone_mask(phone),
            protocol="v2",
        )
        with self._lock:
            self._sessions.pop(session_id, None)
        self.log.info("auth.ok",
                      f"账号 {account['id']} 授权完成（{variant}），凭证已加密保存")
        self.store.add_audit(account["id"], "login", "ok", account["token_version"])
        return {"account": self._public(account), "variant": variant,
                "credentialType": account["credential_type"]}

    # ------------------------------------------------------------ 凭证迁移
    def import_token_file(self, path: str | Path, remark: str = "导入账号") -> dict:
        """把 nte-capture/TOKEN.txt 的现有凭证导入加密库（本地读取，不联网）。"""
        p = Path(path)
        if not p.exists():
            raise AuthError("找不到 TOKEN.txt")
        entries = []
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError:
                raw = {"refreshToken": line}
            if isinstance(raw, dict) and (raw.get("accessToken") or raw.get("refreshToken")):
                entries.append(raw)
        if not entries:
            raise AuthError("TOKEN.txt 里没有可用凭证")
        results = []
        for raw in entries:
            uid = str(raw.get("uid") or "").strip()
            if not uid:
                self.log.warn("auth.import", "跳过一条缺少 uid 的凭证")
                continue
            role_ids = raw.get("roleIds") or []
            role = {"roleId": str(role_ids[0])} if role_ids else {}
            account = self._persist(
                uid=uid,
                access_token=str(raw.get("accessToken") or ""),
                refresh_token=str(raw.get("refreshToken") or ""),
                device_id=str(raw.get("deviceId") or uuid.uuid4().hex),
                role=role,
                phone_mask="导入",
                remark=remark,
                protocol=str(raw.get("protocol") or "v2"),
                credential_type=str(raw.get("credentialType") or
                                    ("refreshToken" if raw.get("refreshToken") else "accessToken")),
            )
            results.append(self._public(account))
            self.store.add_audit(account["id"], "import", "ok", account["token_version"])
        self.log.info("auth.import", f"从本地凭证文件导入 {len(results)} 个账号")
        return {"accounts": results, "count": len(results)}

    # ------------------------------------------------------------ 内部
    def _persist(self, *, uid: str, access_token: str, refresh_token: str,
                 device_id: str, role: dict, phone_mask: str, remark: str,
                 protocol: str, credential_type: str = "accessToken") -> dict:
        if not access_token and not refresh_token:
            raise AuthError("没有可保存的凭证")
        ref = f"cred:{uid}"
        existing = self.store.find_by_uid(uid)
        expected = self.vault.version(ref)
        if expected and existing and existing["token_version"] > expected:
            expected = existing["token_version"]
        payload = json.dumps({
            "credentialType": credential_type,
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "uid": uid,
            "deviceId": device_id,
            "protocol": protocol,
            "obtainedAt": int(time.time()),
        }, ensure_ascii=False)
        try:
            version = self.vault.put(ref, payload, expected_version=expected)
        except VaultError as exc:
            raise AuthError(f"凭证写入失败：{exc}") from exc

        fields = {
            "remark": remark, "phone_mask": phone_mask,
            "role_id": str(role.get("roleId") or ""),
            "role_name": str(role.get("roleName") or ""),
            "server_id": str(role.get("serverId") or ""),
            "device_id": device_id,
            "credential_ref": ref,
            "credential_type": credential_type,
            "uid": uid,
            "adapter_version": self.adapter.cfg["_version"],
            "status": "ok", "enabled": 1, "last_error": "",
            "obtained_at": int(time.time()),
            "token_version": version,
        }
        if existing:
            self.store.update_account(existing["id"], **fields)
            fields["id"] = existing["id"]
            return fields
        fields["id"] = self.store.create_account(**fields)
        return fields

    @staticmethod
    def _public(account: dict) -> dict:
        return {k: account.get(k) for k in
                ("id", "remark", "phone_mask", "role_id", "role_name",
                 "credential_type", "status", "token_version")}

    def credential(self, account: dict) -> dict:
        ref = account.get("credential_ref") or f"cred:{account['uid']}"
        item = self.vault.get(ref)
        if not item:
            raise AuthError("凭证不存在，请重新授权")
        text, version = item
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise AuthError("凭证内容损坏") from exc
        data["_version"] = version
        return data
