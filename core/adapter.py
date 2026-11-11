"""版本化协议适配器：端点、签名、字段映射、错误分级。

所有协议事实来自 adapters/<version>.json；官方改版优先改配置。
本模块只发低频请求，且签到写操作单独隔离在 signin()，由上层写操作开关控制。
"""
from __future__ import annotations

import hashlib
import json
import secrets
import string
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .crypto import aes_ecb_b64
from .gacha import extract_item_names


class AdapterError(Exception):
    def __init__(self, kind: str, message: str, *, status: int | None = None,
                 code: Any = None, payload: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.status = status
        self.code = code
        self.payload = payload

    def as_dict(self) -> dict:
        return {"kind": self.kind, "message": self.message,
                "status": self.status, "code": self.code}


def _num(value, default=0):
    if isinstance(value, bool):
        return default
    try:
        f = float(value)
        return int(f) if f.is_integer() else f
    except (TypeError, ValueError):
        return default


class Adapter:
    def __init__(self, cfg: dict, logger=None):
        self.cfg = cfg
        self.log = logger
        self.ep = cfg["endpoints"]

    # ------------------------------------------------------------ 基础
    def url(self, endpoint_key: str, laohu: bool = False) -> str:
        base = self.cfg["laohu"] if laohu else self.cfg["bbs"]
        return base + self.ep[endpoint_key]

    def http(self, method: str, url: str, *, params: dict | None = None,
             form: dict | None = None, headers: dict | None = None,
             timeout: int | None = None) -> tuple[int, str]:
        if params:
            url = url + "?" + urllib.parse.urlencode(params)
        data = urllib.parse.urlencode(form).encode("utf-8") if form is not None else None
        h = {
            "User-Agent": self.cfg["userAgent"],
            "platform": "android",
            "Accept": "application/json, text/plain, */*",
        }
        if form is not None:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        if headers:
            h.update(headers)
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        # 显式禁用系统代理：避免本机代理配置干扰（动态抓包路径已弃用）
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            resp = opener.open(req, timeout=timeout or self.cfg["limits"]["timeoutSec"])
            return resp.status, resp.read().decode("utf-8", "ignore")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "ignore")
        except Exception as e:  # 网络异常统一成 -1，交给上层分类
            return -1, "ERR: " + str(e)

    def request_json(self, method: str, url: str, **kw) -> tuple[int, dict | None, str]:
        status, text = self.http(method, url, **kw)
        try:
            payload = json.loads(text)
            if not isinstance(payload, dict):
                payload = None
        except (json.JSONDecodeError, TypeError):
            payload = None
        return status, payload, text[:200]

    def classify(self, status: int, payload: dict | None, raw: str = "") -> AdapterError | None:
        """402 分级：鉴权失败 ≠ 协议改版 ≠ 普通业务错误。"""
        if status < 0:
            return AdapterError("network_error", "网络请求失败", status=status)
        if status in self.cfg.get("authExpiredStatus", [401, 402]):
            return AdapterError(
                "auth_expired", "凭证已失效或缺失（V2 需要重新短信登录）", status=status
            )
        if status >= 400:
            return AdapterError("api_error", f"HTTP {status}", status=status)
        if payload is None:
            return AdapterError("api_error", "响应不是 JSON", status=status, payload=None)
        msg = str(payload.get("msg") or payload.get("message") or raw or "")
        if any(k.lower() in msg.lower() for k in self.cfg["protocolChangedKeywords"]):
            return AdapterError("protocol_changed", "疑似协议改版：" + msg[:80],
                                status=status, code=payload.get("code"), payload=payload)
        if payload.get("ok") is True or payload.get("code") == 0:
            return None
        return AdapterError("api_error", msg[:120] or "接口返回失败",
                            status=status, code=payload.get("code"), payload=payload)

    # ------------------------------------------------------------ 签名与参数
    def make_ds(self, app_version: str | None = None) -> str:
        ver = app_version or self.cfg["appVersion"]
        ts = str(int(time.time()))
        nonce = "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))
        digest = hashlib.md5(
            f"{ts}{nonce}{ver}{self.cfg['dsSecret']}".encode()
        ).hexdigest()
        return f"{ts},{nonce},{digest}"

    def sign_params(self, params: dict) -> str:
        values = "".join(str(params[k]) for k in sorted(params))
        return hashlib.md5((values + self.cfg["secret"]).encode("utf-8")).hexdigest()

    def _common_sms(self, phone: str, device_id: str) -> dict:
        c = self.cfg
        return {
            "deviceType": c["deviceModel"], "deviceId": device_id,
            "deviceName": c["deviceModel"], "versionCode": "1",
            "appId": c["appIdLaohu"], "deviceSys": c["deviceSys"],
            "cellphone": phone, "deviceModel": c["deviceModel"],
            "sdkVersion": c["sdkVersion"], "bid": c["bid"],
            "channelId": c["channelId"],
        }

    def auth_headers(self, access_token: str, uid: str, device_id: str) -> dict:
        return {
            "authorization": access_token,
            "uid": str(uid),
            "deviceid": device_id,
            "appversion": self.cfg["appVersion"],
            "debug-uid": "3",
            "ds": self.make_ds(),
        }

    # ------------------------------------------------------------ 授权（短信验证码）
    def send_captcha(self, phone: str, device_id: str) -> None:
        p = {**self._common_sms(phone, device_id),
             "type": "16", "t": str(int(time.time())),
             "areaCodeId": self.cfg["areaCodeId"]}
        p["sign"] = self.sign_params(p)
        status, payload, raw = self.request_json(
            "POST", self.url("sendCaptcha", laohu=True), form=p
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        if not (payload and payload.get("code") == 0):
            raise AdapterError("api_error", "发送验证码未成功")

    def check_captcha(self, phone: str, code: str, device_id: str) -> bool:
        p = {**self._common_sms(phone, device_id),
             "t": str(int(time.time())), "captcha": code}
        p["sign"] = self.sign_params(p)
        status, payload, raw = self.request_json(
            "POST", self.url("checkCaptcha", laohu=True), form=p
        )
        err = self.classify(status, payload, raw)
        return not err and bool(payload and payload.get("code") == 0)

    def _login_params(self, phone: str, code: str, device_id: str, *,
                      plaintext_phone=False, cloud=False, with_type=True) -> dict:
        sec = self.cfg["cloudSecret"] if cloud else self.cfg["secret"]
        cell = phone if plaintext_phone else aes_ecb_b64(phone, sec)
        captcha = aes_ecb_b64(code, sec)
        c = self.cfg
        p = {
            "deviceType": c["deviceModel"], "idfa": "", "sign": "", "adm": "",
            "type": "16", "deviceId": device_id, "version": "1",
            "deviceName": c["deviceModel"], "mac": "",
            "t": str(int(time.time() * 1000)), "areaCodeId": c["areaCodeId"],
            "captcha": captcha, "appId": c["appIdLaohu"], "deviceSys": c["deviceSys"],
            "cellphone": cell, "deviceModel": c["deviceModel"],
            "sdkVersion": c["sdkVersion"], "bid": c["bid"],
            "channelId": c["channelId"],
        }
        if not with_type:
            p.pop("type", None)
        p["sign"] = self.sign_params(p)
        return p

    def sms_login(self, phone: str, code: str, device_id: str,
                  *, skip_check: bool = False) -> tuple[str, str, str]:
        """返回 (老虎 token, 老虎 userId, 命中的变体名)。验证码只在内存中。"""
        if not skip_check:
            self.check_captcha(phone, code, device_id)
        variants = [
            ("V1-复刻开源", lambda: self._login_params(phone, code, device_id)),
            ("V2-明文手机号", lambda: self._login_params(phone, code, device_id, plaintext_phone=True)),
            ("V3-云SDK密钥", lambda: self._login_params(phone, code, device_id, cloud=True)),
            ("V4-无type", lambda: self._login_params(phone, code, device_id, with_type=False)),
        ]
        last = "无可用响应"
        for name, make in variants:
            p = make()
            status, payload, raw = self.request_json(
                "POST", self.url("smsLogin", laohu=True), form=p
            )
            result = (payload or {}).get("result") or {}
            token, user_id = result.get("token"), result.get("userId")
            if token and user_id is not None:
                return str(token), str(user_id), name
            last = str((payload or {}).get("msg") or raw or status)
            time.sleep(1)
        raise AdapterError("api_error", "短信登录四组变体均未成功：" + last[:100])

    def uc_login(self, tiger_token: str, tiger_user_id: str, device_id: str,
                 protocol: str = "v2") -> dict:
        key = "ucLoginV2" if protocol == "v2" else "ucLoginV1"
        ver = self.cfg["appVersion"] if protocol == "v2" else self.cfg["legacyAppVersion"]
        headers = {
            "deviceId": device_id, "Authorization": "", "appVersion": ver,
            "uid": "0", "debug-uid": "3", "ds": self.make_ds(ver),
        }
        form = {"token": tiger_token, "userIdentity": tiger_user_id,
                "appId": self.cfg["appIdUserCenter"]}
        status, payload, raw = self.request_json("POST", self.url(key), form=form, headers=headers)
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data") or {}
        if not data.get("accessToken"):
            raise AdapterError("api_error", "用户中心登录未返回 accessToken")
        return {
            "accessToken": data["accessToken"],
            "refreshToken": data.get("refreshToken") or "",
            "uid": str(data.get("uid") or ""),
            "protocol": protocol,
        }

    # ------------------------------------------------------------ 只读业务
    def get_game_roles(self, access_token: str, uid: str, device_id: str) -> list[dict]:
        status, payload, raw = self.request_json(
            "GET", self.url("getGameRoles"),
            params={"gameId": self.cfg["gameId"]},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data") or {}
        roles = data.get("roles") if isinstance(data, dict) else data
        if not isinstance(roles, list):
            roles = []
        # 只做异环，其它 gameId 一律过滤
        return [r for r in roles if str(r.get("gameId")) == str(self.cfg["gameId"])]

    def get_sign_state(self, access_token: str, uid: str, device_id: str):
        """只读辅助查询。注意：data 布尔值语义未定案，不得作为幂等判据。"""
        status, payload, raw = self.request_json(
            "GET", self.url("getSignState"),
            params={"communityId": self.cfg["communityId"]},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        return (payload or {}).get("data")

    def fetch_role_home(self, access_token: str, uid: str, device_id: str,
                        role_id: str) -> dict:
        status, payload, raw = self.request_json(
            "GET", self.url("roleHome"), params={"roleId": role_id},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, dict):
            raise AdapterError("api_error", "roleHome 未返回 data 对象")
        mapped = self.map_role_home(data)
        # 顺手收集官方角色/弧盘名称（零额外请求）：用于抽卡明细显示中文名
        mapped["itemNames"] = extract_item_names(data.get("characters"))
        return mapped

    def fetch_gacha(self, access_token: str, uid: str, device_id: str,
                    role_id: str) -> dict:
        """只读：官方抽卡聚合统计 + 稀有出货明细。不是逐抽流水。"""
        status, payload, raw = self.request_json(
            "GET", self.url("gacha"), params={"roleId": role_id},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, dict) or not isinstance(data.get("gachaDetails"), list):
            raise AdapterError("api_error", "gacha 未返回 gachaDetails 列表")
        return data

    def fetch_item_names(self, access_token: str, uid: str, device_id: str,
                         role_id: str) -> dict[str, str]:
        """只读：官方角色详情字典，用于把 charid 还原成中文名。

        失败时由上层记录 WARN 并继续（名称只是增强显示，不影响抽数口径）。
        """
        if "characters" not in self.ep:
            return {}
        status, payload, raw = self.request_json(
            "GET", self.url("characters"), params={"roleId": role_id},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        return extract_item_names((payload or {}).get("data"))

    # ------------------------------------------------------------ 字段映射
    def map_role_home(self, data: dict) -> dict:
        """把 roleHome.data 映射成看板指标。

        - 上限一律取接口；缺字段才用前端缺省/推算，并明确标 source；
        - 周本次数为条件性字段：官方下发时作为第四项真实值输出；未下发时 value=None
          且 valueSource='unavailable'，由前端显示「—」，绝不套用 H5 的 3/3 兜底常量；
        - 周本相关提示走 notices（INFO），不告警、不写账号 last_error、不计入提醒。
        """
        metrics: dict[str, Any] = {}
        warnings: list[str] = []
        for spec in self.cfg["metrics"]:
            raw_val = data.get(spec["valueField"])
            value_source = "api"
            if raw_val is None:
                value = spec.get("fallbackValue", 0)
                value_source = spec.get("fallbackSource", "frontend_default")
            else:
                value = _num(raw_val, 0)

            max_value = None
            max_source = spec.get("maxSource", "api")
            if spec.get("maxField"):
                raw_max = data.get(spec["maxField"])
                if raw_max is not None:
                    max_value = _num(raw_max, 0)
                elif "fallbackMax" in spec:
                    max_value = spec["fallbackMax"]
                    max_source = "frontend_default"
                    warnings.append(f"{spec['label']} 上限缺失，使用前端缺省值 {max_value}")
                elif "fallbackByLevel" in spec:
                    rule = spec["fallbackByLevel"]
                    level = _num(data.get(rule["field"]), 0)
                    max_value = rule.get("default", 100)
                    for t in rule["thresholds"]:
                        if level >= t["min"]:
                            max_value = t["max"]
                            break
                    max_source = "estimated"
                    warnings.append(
                        f"{spec['label']} 上限缺失，按 {rule['field']}={level} 推算 {max_value}（推算值）"
                    )
            elif "max" in spec:
                max_value = spec["max"]
            metrics[spec["key"]] = {
                "label": spec["label"],
                "value": value,
                "max": max_value,
                "maxSource": max_source,
                "valueSource": value_source,
            }

        # 结构指纹：官方新增/删除「未知」字段时提示，避免静默错读。
        # 指纹仍基于完整键集（便于观察条件性字段的出现/消失），
        # 但 roleHomeOptionalKeys 中登记的已知条件性字段不算协议改版。
        keys = set(data)
        fingerprint = hashlib.sha256(
            ",".join(sorted(keys)).encode("utf-8")
        ).hexdigest()[:12]
        expected = set(self.cfg.get("roleHomeExpectedKeys", []))
        optional = set(self.cfg.get("roleHomeOptionalKeys", []))
        if expected:
            missing = expected - keys
            added = (keys - expected) - optional
            if missing or added:
                warnings.append(
                    "roleHome 字段集变化（疑似协议改版）："
                    f"新增 {sorted(added)}，缺失 {sorted(missing)}"
                )

        # 提醒性信息与真正异常分开：notices 只记 INFO，不写账号 last_error，
        # 也不在看板上渲染成橙色报错。
        notices: list[str] = []
        from .config import weekly_config  # 避免循环导入
        weekly = weekly_config(self.cfg)
        field = weekly.get("field")
        raw_weekly = data.get(field) if field and field in data else None
        # 字段缺失或值为 null 都视为「没同步到」（存档字段，进游戏并退出后才有），绝不补前端常量 3
        weekly_available = raw_weekly is not None
        weekly_value = _num(raw_weekly, 0) if weekly_available else None
        weekly_visible = bool(weekly.get("enabled", False))
        if weekly_visible:
            metrics["weeklyCopies"] = {
                "label": weekly.get("label", "周本次数"),
                "value": weekly_value,
                "max": weekly.get("denominator", 3),
                "maxSource": weekly.get("denominatorSource", "frontend_constant"),
                "valueSource": "api" if weekly_available else "unavailable",
                "available": weekly_available,
            }
            if not weekly_available:
                notices.append(
                    f"角色数据里没有 {field}（存档未同步），周本次数显示「—」；"
                    "未使用 H5 前端兜底常量 3/3，也不计入提醒"
                )
        elif weekly_available:
            notices.append(
                f"官方已下发 {field}={weekly_value!r}，但设置里关闭了周本次数显示"
            )
        return {
            "metrics": metrics,
            "fingerprint": fingerprint,
            "warnings": warnings,
            "notices": notices,
            "weeklyAvailable": weekly_available,
            "weeklyValue": weekly_value,
            "weeklyVisible": bool(weekly.get("enabled", False)),
            "tycoonLevel": _num(data.get("tycoonLevel"), 0),
            "roleName": data.get("rolename") or "",
        }

    # ------------------------------------------------------------ 写操作（仅签到）
    def signin(self, access_token: str, uid: str, device_id: str) -> dict:
        """提交社区签到。调用方必须已检查写操作开关 + 当日幂等。"""
        status, payload, raw = self.request_json(
            "POST", self.url("signin"),
            form={"communityId": self.cfg["communityId"]},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        if status in self.cfg.get("authExpiredStatus", [401, 402]):
            raise AdapterError("auth_expired", "凭证已失效，需重新短信登录", status=status)
        payload = payload or {}
        msg = str(payload.get("msg") or payload.get("message") or raw or "")
        code = str(payload.get("code"))
        if payload.get("code") == 0 or payload.get("ok") is True:
            # data 可能是 false / 数字 / 字符串（getSignState 就返回过 false），
            # 只有 dict 才能取 exp，否则会把成功响应误判成任务异常。
            data = payload.get("data")
            exp = data.get("exp") if isinstance(data, dict) else None
            return {"result": "success", "code": code, "msg": msg, "exp": exp}
        if any(k in msg for k in self.cfg["alreadySignedKeywords"]):
            return {"result": "already", "code": code, "msg": msg}
        if any(k.lower() in msg.lower() for k in self.cfg["protocolChangedKeywords"]):
            raise AdapterError("protocol_changed", "疑似协议改版：" + msg[:80],
                               status=status, code=code, payload=payload)
        raise AdapterError("api_error", msg[:120] or "签到失败",
                           status=status, code=code, payload=payload)

    # ------------------------------------------------------------ 异环游戏内每日签到
    # 社区签到（上面的 signin）只发社区经验/金币；游戏奖励走下面这组 awapi 接口。
    def _game_id(self) -> str:
        return str(self.cfg.get("gameId") or "1289")

    def fetch_sign_state(self, access_token: str, uid: str, device_id: str,
                         game_id: str | None = None) -> dict:
        """只读：今日是否已签、本月累计天数、可补签次数。"""
        status, payload, raw = self.request_json(
            "GET", self.url("signState"),
            params={"gameId": game_id or self._game_id()},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, dict):
            data = {}
        return {
            "todaySign": bool(data.get("todaySign")),
            "days": int(_num(data.get("days"), 0)),
            "day": int(_num(data.get("day"), 0)),
            "month": int(_num(data.get("month"), 0)),
            "reSignCnt": int(_num(data.get("reSignCnt"), 0)),
        }

    def fetch_sign_rewards(self, access_token: str, uid: str, device_id: str,
                           role_id: str = "", game_id: str | None = None) -> list[dict]:
        """只读：30 天奖励表，rewards[days] 即今日可领奖励（H5 的 0 基算法）。"""
        params: dict = {"gameId": game_id or self._game_id()}
        if role_id:
            params["roleId"] = role_id
        status, payload, raw = self.request_json(
            "GET", self.url("signRewards"), params=params,
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append({
                    "name": str(item.get("name") or ""),
                    "num": int(_num(item.get("num"), 0)),
                    "icon": str(item.get("icon") or ""),
                })
        return out

    def fetch_sign_reward_records(self, access_token: str, uid: str, device_id: str,
                                  game_id: str | None = None) -> list[dict]:
        """只读：最近的奖励发放流水，用于核对奖励是否真的到账。"""
        status, payload, raw = self.request_json(
            "GET", self.url("signRewardRecords"),
            params={"gameId": game_id or self._game_id()},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, list):
            return []
        out = []
        for item in data:
            if isinstance(item, dict):
                out.append({
                    "createTime": int(_num(item.get("createTime"), 0)),
                    "name": str(item.get("name") or ""),
                    "num": int(_num(item.get("num"), 0)),
                    "icon": str(item.get("icon") or ""),
                })
        return out

    def sign_game(self, access_token: str, uid: str, device_id: str,
                  role_id: str, game_id: str | None = None) -> dict:
        """提交游戏内每日签到（真正发游戏奖励的写操作）。"""
        if not role_id:
            raise AdapterError("api_error", "缺少 roleId，无法提交游戏签到")
        status, payload, raw = self.request_json(
            "POST", self.url("signGame"),
            form={"roleId": role_id, "gameId": game_id or self._game_id()},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        if status in self.cfg.get("authExpiredStatus", [401, 402]):
            raise AdapterError("auth_expired", "凭证已失效，需重新短信登录", status=status)
        payload = payload or {}
        msg = str(payload.get("msg") or payload.get("message") or raw or "")
        code = str(payload.get("code"))
        if payload.get("code") == 0 or payload.get("ok") is True:
            data = payload.get("data")
            return {"result": "success", "code": code, "msg": msg,
                    "data": data if isinstance(data, (dict, list)) else None}
        if any(k in msg for k in self.cfg["alreadySignedKeywords"]):
            return {"result": "already", "code": code, "msg": msg, "data": None}
        if any(k.lower() in msg.lower() for k in self.cfg["protocolChangedKeywords"]):
            raise AdapterError("protocol_changed", "疑似协议改版：" + msg[:80],
                               status=status, code=code, payload=payload)
        raise AdapterError("api_error", msg[:120] or "游戏签到失败",
                           status=status, code=code, payload=payload)

    def resign(self, access_token: str, uid: str, device_id: str,
               role_id: str, game_id: str | None = None) -> dict:
        """提交补签（写操作，消耗官方「呗果」积点，每月上限 3 次）。

        官方 H5 说明：完成当日签到后才可补签；补签自动补「当日签到后下一日」的奖励；
        每次消耗 200 呗果；次数每月 1 日 00:00 重置。
        """
        if not role_id:
            raise AdapterError("api_error", "缺少 roleId，无法提交补签")
        status, payload, raw = self.request_json(
            "POST", self.url("resign"),
            form={"roleId": role_id, "gameId": game_id or self._game_id()},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        if status in self.cfg.get("authExpiredStatus", [401, 402]):
            raise AdapterError("auth_expired", "凭证已失效，需重新短信登录", status=status)
        payload = payload or {}
        msg = str(payload.get("msg") or payload.get("message") or raw or "")
        code = str(payload.get("code"))
        if payload.get("code") == 0 or payload.get("ok") is True:
            data = payload.get("data")
            return {"result": "success", "code": code, "msg": msg,
                    "data": data if isinstance(data, (dict, list)) else None}
        if any(k in msg for k in self.cfg["alreadySignedKeywords"]):
            return {"result": "already", "code": code, "msg": msg, "data": None}
        if any(k.lower() in msg.lower() for k in self.cfg["protocolChangedKeywords"]):
            raise AdapterError("protocol_changed", "疑似协议改版：" + msg[:80],
                               status=status, code=code, payload=payload)
        raise AdapterError("api_error", msg[:120] or "补签失败",
                           status=status, code=code, payload=payload)

    def auto_bind_role(self, access_token: str, uid: str, device_id: str,
                       role_id: str, game_id: str | None = None) -> dict:
        """自动绑定游戏角色 = 塔吉多 App 登录后自动执行的那一步。

        2026-11-08 实测：只调 signin/state + sign 时，社区侧 reward_records 有记录，
        但游戏内邮箱始终收不到邮件（用户对照实验：登录过塔吉多 App 的号能收到，
        没登录过的号收不到）。补发一次 POST /usercenter/api/autoBindGameRole
        （form: gameId + roleId）后，奖励立刻进游戏邮箱。

        注意：它**不会**改写 getGameBindRoles 里的 account / updateTime（实测仍为空、
        时间戳不变），所以它的作用是「在游戏服侧建立角色联动并触发待发放投递」，
        不是维护社区绑定表。故不能靠读绑定表判断是否已投递。
        """
        status, payload, raw = self.request_json(
            "POST", self.url("autoBindRole"),
            form={"gameId": game_id or self._game_id(), "roleId": str(role_id)},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        return {"code": str((payload or {}).get("code")), "data": (payload or {}).get("data")}

    def resign_info(self, access_token: str, uid: str, device_id: str,
                    game_id: str | None = None) -> dict:
        """只读：补签次数与积分（本工具不自动补签）。"""
        status, payload, raw = self.request_json(
            "GET", self.url("resignInfo"),
            params={"gameId": game_id or self._game_id()},
            headers=self.auth_headers(access_token, uid, device_id),
        )
        err = self.classify(status, payload, raw)
        if err:
            raise err
        data = (payload or {}).get("data")
        if not isinstance(data, dict):
            data = {}
        return {
            "coin": int(_num(data.get("coin"), 0)),
            "cost": int(_num(data.get("cost"), 0)),
            "resignCnt": int(_num(data.get("resignCnt"), 0)),
            "resignLimit": int(_num(data.get("resignLimit"), 0)),
            "todaySign": bool(data.get("todaySign")),
        }
