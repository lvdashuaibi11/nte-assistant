"""S2 核心逻辑单元测试（纯标准库 unittest，不联网、不签到）。

运行：
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import base64
import datetime
import inspect
import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

TMP = tempfile.mkdtemp(prefix="nte-test-")
os.environ.setdefault("NTE_DATA_DIR", TMP)

from core import config, crypto                     # noqa: E402
from core.adapter import Adapter, AdapterError       # noqa: E402
from core.auth import AuthError, AuthManager, SESSION_TTL  # noqa: E402
from core.db import Store                            # noqa: E402
from core.gacha import extract_item_names, normalize, ts_millis, ts_text, summary_diff  # noqa: E402
from core.logutil import Logger, scrub_text, phone_mask  # noqa: E402
from core.service import Job, Service, WriteDisabled, week_reset_ts  # noqa: E402
from core.vault import Vault, VersionConflict, _fallback_protect, _fallback_unprotect  # noqa: E402

CFG = config.load_adapter()


def sqlite_columns(db_path, table):
    con = sqlite3.connect(db_path)
    try:
        return {r[1] for r in con.execute("PRAGMA table_info(" + table + ")")}
    finally:
        con.close()


def sample_role_home() -> dict:
    """2026-11-07 实测 roleHome.data 的字段集与三项数值。"""
    keys = CFG["roleHomeExpectedKeys"]
    data = {k: None for k in keys}
    data.update({
        "staminaValue": 360, "staminaMaxValue": 360,
        "citystaminaValue": 580, "citystaminaMaxValue": 700,
        "dayvalue": 100, "tycoonLevel": 17, "rolename": "测试角色",
    })
    return data


class BaseCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="nte-case-", dir=TMP))
        self.store = Store(self.dir / "a.db")
        self.vault = Vault(self.dir / "vault.json")
        self.logger = Logger(self.dir / "a.db", self.dir / "run.log")
        self.adapter = Adapter(CFG, self.logger)
        self.service = Service(self.store, self.vault, self.logger, self.adapter)

    def make_account(self, *, write=False, uid="u1"):
        ref = f"cred:{uid}"
        self.vault.put(ref, json.dumps({
            "credentialType": "accessToken", "accessToken": "TOKEN-VALUE",
            "refreshToken": "", "uid": uid, "deviceId": "dev1",
            "protocol": "v2", "obtainedAt": int(time.time()),
        }))
        aid = self.store.create_account(
            remark="测试号", phone_mask="139****0000", role_id="100000000001",
            role_name="测试角色", server_id="14001", device_id="dev1",
            credential_ref=ref, credential_type="accessToken", uid=uid,
            adapter_version="1.2.8", status="ok", enabled=1,
            obtained_at=int(time.time()), token_version=self.vault.version(ref),
        )
        if write:
            self.store.set_setting("writeActionsEnabled", True)
            self.store.set_setting("minIntervalSec", 0)
            self.store.set_setting("maxIntervalSec", 0)
        return aid


class TestCrypto(unittest.TestCase):
    def test_fips197_vector(self):
        key = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
        block = bytes.fromhex("3243f6a8885a308d313198a2e0370734")
        out = crypto._encrypt_block(block, crypto._expand_key(key))
        self.assertEqual(out.hex(), "3925841d02dc09fbdc118597196a0b32")

    def test_aes_b64_deterministic(self):
        a = crypto.aes_ecb_b64("13800138000", CFG["secret"])
        self.assertEqual(a, crypto.aes_ecb_b64("13800138000", CFG["secret"]))
        self.assertTrue(base64.b64decode(a))


class TestVault(BaseCase):
    def test_roundtrip_and_cas(self):
        v1 = self.vault.put("cred:x", "secret-1")
        self.assertEqual(self.vault.get("cred:x"), ("secret-1", v1))
        v2 = self.vault.put("cred:x", "secret-2", expected_version=v1)
        self.assertEqual(self.vault.get("cred:x"), ("secret-2", v2))
        with self.assertRaises(VersionConflict):
            self.vault.put("cred:x", "stale", expected_version=v1)
        self.assertEqual(self.vault.get("cred:x")[0], "secret-2")

    def test_fallback_tamper_detected(self):
        blob = _fallback_protect(b"payload")
        self.assertEqual(_fallback_unprotect(blob), b"payload")
        broken = bytearray(blob)
        broken[-1] ^= 0x01
        with self.assertRaises(Exception):
            _fallback_unprotect(bytes(broken))

    def test_vault_file_has_no_plaintext(self):
        self.vault.put("cred:x", "SUPER-SECRET-TOKEN")
        self.assertNotIn("SUPER-SECRET-TOKEN", self.vault.path.read_text(encoding="utf-8"))


class TestMapping(BaseCase):
    def test_real_values_and_weekly_missing_shows_dash(self):
        result = self.adapter.map_role_home(sample_role_home())
        m = result["metrics"]
        self.assertEqual(set(m), {"stamina", "cityStamina", "activity", "weeklyCopies"})
        self.assertEqual((m["stamina"]["value"], m["stamina"]["max"]), (360, 360))
        self.assertEqual((m["cityStamina"]["value"], m["cityStamina"]["max"]), (580, 700))
        self.assertEqual((m["activity"]["value"], m["activity"]["max"]), (100, 100))
        self.assertEqual(m["activity"]["maxSource"], "frontend_constant")
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["weeklyVisible"])
        self.assertFalse(result["weeklyAvailable"])
        w = m["weeklyCopies"]
        # 没同步到该字段时绝不伪造 3/3：值必须是 None，由前端渲染成「—」
        self.assertIsNone(w["value"])
        self.assertEqual(w["valueSource"], "unavailable")
        self.assertFalse(w["available"])
        self.assertEqual(w["max"], 3)
        self.assertEqual(w["maxSource"], "frontend_constant")
        self.assertNotIn("3/3", json.dumps(m, ensure_ascii=False))
        self.assertTrue(any("roleHome 里没有" in n or "没有 weekcopiesremainCnt" in n
                            for n in result["notices"]), result["notices"])

    def test_fallbacks_are_marked(self):
        data = sample_role_home()
        data.pop("staminaMaxValue")
        data.pop("citystaminaMaxValue")
        data.pop("dayvalue")
        result = self.adapter.map_role_home(data)
        m = result["metrics"]
        self.assertEqual(m["stamina"]["max"], 240)
        self.assertEqual(m["stamina"]["maxSource"], "frontend_default")
        self.assertEqual(m["cityStamina"]["max"], 500)  # tycoonLevel=17 → EN() 500
        self.assertEqual(m["cityStamina"]["maxSource"], "estimated")
        self.assertEqual(m["activity"]["value"], 0)
        self.assertTrue(any("推算" in w for w in result["warnings"]))

    def test_weekly_field_returned_shows_real_value(self):
        data = sample_role_home()
        data["weekcopiesremainCnt"] = 2
        result = self.adapter.map_role_home(data)
        self.assertTrue(result["weeklyAvailable"])
        self.assertTrue(result["weeklyVisible"])
        self.assertEqual(result["weeklyValue"], 2)
        w = result["metrics"]["weeklyCopies"]
        self.assertEqual((w["value"], w["max"]), (2, 3))
        self.assertEqual(w["valueSource"], "api")
        self.assertTrue(w["available"])
        self.assertEqual(len(result["metrics"]), 4)
        # 已知条件性字段不算协议改版，不得污染账号 last_error / 看板报错区
        self.assertFalse(any("字段集变化" in x for x in result["warnings"]))

    def test_weekly_zero_is_real_value_not_default_three(self):
        """用户自己的号本周周本已打完 → 剩余 0 次，不能显示成 3/3。"""
        data = sample_role_home()
        data["weekcopiesremainCnt"] = 0
        result = self.adapter.map_role_home(data)
        self.assertEqual(result["metrics"]["weeklyCopies"]["value"], 0)
        self.assertEqual(result["metrics"]["weeklyCopies"]["valueSource"], "api")

    def test_weekly_null_is_unavailable(self):
        data = sample_role_home()
        data["weekcopiesremainCnt"] = None
        result = self.adapter.map_role_home(data)
        self.assertFalse(result["weeklyAvailable"])
        self.assertIsNone(result["metrics"]["weeklyCopies"]["value"])
        self.assertEqual(result["metrics"]["weeklyCopies"]["valueSource"], "unavailable")

    def test_weekly_never_fabricates_three(self):
        result = self.adapter.map_role_home(sample_role_home())
        self.assertNotEqual(result["metrics"]["weeklyCopies"]["value"], 3)
        self.assertNotEqual(result["weeklyValue"], 3)

    def test_unknown_field_still_warns(self):
        data = sample_role_home()
        data["mysteryField"] = 1
        result = self.adapter.map_role_home(data)
        self.assertTrue(any("疑似协议改版" in w for w in result["warnings"]))

    def test_structure_change_warning(self):
        data = sample_role_home()
        data.pop("worldlevel")
        result = self.adapter.map_role_home(data)
        self.assertTrue(any("疑似协议改版" in w for w in result["warnings"]))


class TestErrors(unittest.TestCase):
    def test_402_is_auth_expired(self):
        err = Adapter(CFG).classify(402, None)
        self.assertEqual(err.kind, "auth_expired")

    def test_invalid_request_is_protocol_changed(self):
        err = Adapter(CFG).classify(200, {"code": 22, "msg": "invalid request", "ok": False})
        self.assertEqual(err.kind, "protocol_changed")

    def test_already_signed_by_msg_not_code(self):
        ad = Adapter(CFG)
        ad.http = lambda *a, **k: (200, '{"code":1,"msg":"今天已签到","ok":false}')
        self.assertEqual(ad.signin("t", "u", "d")["result"], "already")

    def test_signin_success(self):
        ad = Adapter(CFG)
        ad.http = lambda *a, **k: (200, '{"code":0,"data":{"exp":5,"goldCoin":2},"msg":"ok"}')
        r = ad.signin("t", "u", "d")
        self.assertEqual(r["result"], "success")
        self.assertEqual(r["exp"], 5)

    def test_signin_success_with_non_dict_data(self):
        """官方响应 data 可能是 false / 数字 / 字符串 / null，不能因此崩掉成功结果。"""
        ad = Adapter(CFG)
        for raw in ('false', '0', '"done"', 'null', '[]'):
            with self.subTest(data=raw):
                ad.http = lambda *a, _r=raw, **k: (
                    200, '{"code":0,"data":' + _r + ',"msg":"ok"}')
                r = ad.signin("t", "u", "d")
                self.assertEqual(r["result"], "success")
                self.assertIsNone(r["exp"])


class TestGameSignin(unittest.TestCase):
    """异环游戏内每日签到（awapi）：状态/奖励解析与写请求参数。"""

    def setUp(self):
        self.ad = Adapter(CFG)

    def test_state_parses(self):
        self.ad.http = lambda *a, **k: (
            200, '{"code":0,"data":{"day":8,"days":4,"month":11,'
                 '"reSignCnt":2,"todaySign":true},"msg":"ok"}')
        self.assertEqual(self.ad.fetch_sign_state("t", "u", "d"),
                         {"todaySign": True, "days": 4, "day": 8,
                          "month": 11, "reSignCnt": 2})

    def test_rewards_parse(self):
        self.ad.http = lambda *a, **k: (
            200, '{"code":0,"data":[{"name":"甲硬币","num":"10000","icon":"x"}]}')
        self.assertEqual(self.ad.fetch_sign_rewards("t", "u", "d", "r"),
                         [{"name": "甲硬币", "num": 10000, "icon": "x"}])

    def test_reward_records_parse(self):
        self.ad.http = lambda *a, **k: (
            200, '{"code":0,"data":[{"createTime":1793985749347,'
                 '"name":"扩容核心","num":2,"icon":"y"}]}')
        self.assertEqual(self.ad.fetch_sign_reward_records("t", "u", "d"),
                         [{"createTime": 1793985749347, "name": "扩容核心",
                           "num": 2, "icon": "y"}])

    def test_sign_game_uses_form_and_game_id(self):
        seen = {}

        def fake_http(method, url, **kw):
            seen.update({"method": method, "url": url, "form": kw.get("form")})
            return 200, '{"code":0,"data":null,"msg":"ok"}'

        self.ad.http = fake_http
        r = self.ad.sign_game("t", "u", "d", "role-1")
        self.assertEqual(r["result"], "success")
        self.assertEqual(seen["method"], "POST")
        self.assertIn("/apihub/awapi/sign", seen["url"])
        self.assertEqual(seen["form"], {"roleId": "role-1", "gameId": "1289"})

    def test_sign_game_already_by_msg(self):
        self.ad.http = lambda *a, **k: (
            200, '{"code":1,"msg":"今天已签到","ok":false}')
        self.assertEqual(self.ad.sign_game("t", "u", "d", "role")["result"], "already")

    def test_resign_posts_form_and_game_id(self):
        seen = {}

        def fake_http(method, url, **kw):
            seen.update({"method": method, "url": url, "form": kw.get("form")})
            return 200, '{"code":0,"data":null,"msg":"ok"}'

        self.ad.http = fake_http
        r = self.ad.resign("t", "u", "d", "role-1")
        self.assertEqual(r["result"], "success")
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/apihub/awapi/resign"))
        self.assertEqual(seen["form"], {"roleId": "role-1", "gameId": "1289"})

    def test_resign_error_is_classified(self):
        self.ad.http = lambda *a, **k: (200, '{"code":500,"msg":"呗果不足"}')
        with self.assertRaises(AdapterError) as ctx:
            self.ad.resign("t", "u", "d", "role")
        self.assertEqual(ctx.exception.kind, "api_error")
        self.assertIn("呗果", ctx.exception.message)

    def test_auto_bind_role_posts_game_and_role(self):
        """autoBindGameRole 是「奖励进邮箱」的关键一步：form 必须带 gameId + roleId。"""
        seen = {}

        def fake_http(method, url, **kw):
            seen.update({"method": method, "url": url, "form": kw.get("form")})
            return 200, '{"code":0,"data":null,"msg":"ok"}'

        self.ad.http = fake_http
        r = self.ad.auto_bind_role("t", "u", "d", "role-9")
        self.assertEqual(seen["method"], "POST")
        self.assertTrue(seen["url"].endswith("/usercenter/api/autoBindGameRole"))
        self.assertEqual(seen["form"], {"gameId": "1289", "roleId": "role-9"})
        self.assertEqual(r["code"], "0")

    def test_auto_bind_role_error_is_classified(self):
        self.ad.http = lambda *a, **k: (200, '{"code":151,"msg":"该角色已绑定"}')
        with self.assertRaises(AdapterError) as ctx:
            self.ad.auto_bind_role("t", "u", "d", "role")
        self.assertEqual(ctx.exception.kind, "api_error")


class TestSigninfoAndResign(BaseCase):
    """签到详情（呗果 / 补签次数 / 本月奖励表）与补签的前置条件。"""

    def stub(self, *, today=True, days=2, coin=200, cost=200, cnt=0, limit=3,
             rewards=None):
        class Stub:
            cfg = self.adapter.cfg

            def __init__(self):
                self.bind_calls = []

            def fetch_sign_state(self, *a, **k):
                return {"todaySign": today, "days": days, "day": 8, "month": 11,
                        "reSignCnt": 0}

            def auto_bind_role(self, *a, **k):
                self.bind_calls.append((a, k))
                return {"code": "0", "data": None}

            def sign_game(self, *a, **k):
                return {"result": "success", "code": "0", "msg": "ok"}

            def resign_info(self, *a, **k):
                return {"coin": coin, "cost": cost, "resignCnt": cnt,
                        "resignLimit": limit, "todaySign": today}

            def fetch_sign_rewards(self, *a, **k):
                return rewards if rewards is not None else [
                    {"name": "甲硬币", "num": 10000, "icon": ""},
                    {"name": "环石", "num": 20, "icon": ""},
                    {"name": "井屑达人", "num": 3, "icon": ""},
                ]

            def fetch_sign_reward_records(self, *a, **k):
                return [{"createTime": 1794127785028, "name": "资深猎人攻略",
                         "num": 3, "icon": ""}]

            def resign(self, *a, **k):
                return {"result": "success", "code": "0", "msg": "ok", "data": None}

        return Stub()

    def test_collect_marks_signed_today_and_premium(self):
        aid = self.make_account()
        self.service.adapter = self.stub(today=True, days=2)
        info = self.service._collect_signinfo(
            self.store.get_account(aid), "t", "u", "d", "role", "1289")
        self.assertTrue(info["rewards"][0]["signed"])
        self.assertTrue(info["rewards"][1]["signed"])
        self.assertTrue(info["rewards"][1]["today"])     # 今日已签 -> days-1
        self.assertFalse(info["rewards"][2]["signed"])
        self.assertTrue(info["rewards"][1]["premium"])   # 环石加粗
        self.assertFalse(info["rewards"][0]["premium"])
        self.assertTrue(info["canResign"])
        self.assertEqual(info["resignLeft"], 3)
        self.assertEqual(info["records"][0]["at"], 1794127785)

    def test_today_index_when_not_signed(self):
        aid = self.make_account()
        self.service.adapter = self.stub(today=False, days=2)
        info = self.service._collect_signinfo(
            self.store.get_account(aid), "t", "u", "d", "role", "1289")
        self.assertTrue(info["rewards"][2]["today"])     # 今日可签 = rewards[days]
        self.assertFalse(info["rewards"][2]["signed"])
        self.assertFalse(info["canResign"])              # 未签不能补签

    def test_signin_success_triggers_delivery(self):
        """签到成功后必须自动触发 auto_bind_role，否则奖励进不了游戏邮箱。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=False, days=2)
        self.service.adapter = stub
        job = Job("signin", aid)
        self.service._do_signin(job, self.store.get_account(aid))
        self.assertEqual(job.status, "done")
        self.assertTrue(stub.bind_calls, "签到成功后未触发 auto_bind_role")
        args = stub.bind_calls[0][0]
        self.assertEqual(args[3], "100000000001")        # roleId 透传正确

    def test_resign_success_triggers_delivery(self):
        """补签和签到同一个坑：只 resign 不调 autoBindGameRole，奖励进不了游戏邮箱。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=True, days=2)
        self.service.adapter = stub
        job = Job("resign", aid)
        self.service._do_resign(job, self.store.get_account(aid))
        self.assertEqual(job.status, "done")
        self.assertTrue(stub.bind_calls, "补签成功后未触发 auto_bind_role")
        self.assertEqual(stub.bind_calls[0][0][3], "100000000001")
        self.assertIn("投递", job.message)

    def test_sign_already_still_triggers_delivery(self):
        """state 快照说未签、sign_game 却返回「今天已签到」（别人刚签过）时，
        同样必须触发投递，否则奖励只留在社区侧。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=False, days=2)
        stub.sign_game = lambda *a, **k: {"result": "already", "code": "0",
                                          "msg": "今天已签到", "data": None}
        self.service.adapter = stub
        job = Job("signin", aid)
        self.service._do_signin(job, self.store.get_account(aid))
        self.assertTrue(stub.bind_calls, "already 分支未触发 auto_bind_role")

    def test_resign_warns_when_delivery_fails(self):
        """投递失败不能谎报成功，必须提示改用「补投递」。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=True, days=2)

        def boom(*a, **k):
            raise AdapterError("network", "连接超时")
        stub.auto_bind_role = boom
        self.service.adapter = stub
        job = Job("resign", aid)
        self.service._do_resign(job, self.store.get_account(aid))
        self.assertEqual(job.status, "done")             # 补签本身是成功的
        self.assertIn("补投递", job.message)
        self.assertNotIn("已触发投递", job.message)

    def test_last_signin_success_ignores_today_state(self):
        """今日未签也要能查到历史成功记录，否则补投递会被永久拦住。"""
        aid = self.make_account(write=True)
        self.assertIsNone(self.store.last_signin_success(aid, "1289"))
        self.store.record_signin(aid, "2026-11-07", "1289", "success",
                                 mode="game", game_id="1289")
        self.store.record_signin(aid, "2026-11-08", "1289", "failed",
                                 mode="game", game_id="1289")
        row = self.store.last_signin_success(aid, "1289")
        self.assertIsNotNone(row)
        self.assertEqual(row["date"], "2026-11-07")

    def test_already_signed_also_triggers_delivery(self):
        """今日已签（奖励可能还没到邮箱）时也要补一次投递。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=True, days=2)
        self.service.adapter = stub
        job = Job("signin", aid)
        self.service._do_signin(job, self.store.get_account(aid))
        self.assertEqual(job.status, "done")
        self.assertTrue(stub.bind_calls, "已签分支未补投递")

    def test_auto_delivery_retries_before_giving_up(self):
        """投递遇到网络抖动要原地重试，不能一次失败就让人手动点补投递。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=False, days=2)
        calls = {"n": 0}

        def flaky(*a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AdapterError("network", "连接超时")
            return {"code": "0", "data": None}

        stub.auto_bind_role = flaky
        self.service.adapter = stub
        ok = self.service._ensure_delivery(
            self.store.get_account(aid), "t", "u", "d", "100000000001", "1289")
        self.assertTrue(ok)
        self.assertEqual(calls["n"], 2)

    def test_signin_plans_delayed_redelivery(self):
        """签到成功后必须排一次延迟自动补投递，用户不必每天手动点按钮。"""
        aid = self.make_account(write=True)
        self.service.adapter = self.stub(today=False, days=2)
        self.service._do_signin(Job("signin", aid), self.store.get_account(aid))
        self.assertEqual(len(self.service._timers), 1)
        due, job = self.service._timers[0]
        self.assertEqual(job.kind, "deliver")
        self.assertTrue(job.auto, "延迟补投递应标记为后台自动任务")
        self.assertGreater(due, time.time() + 5)

    def test_drain_timers_enqueues_due_jobs(self):
        """到点的延迟任务要真正进队列，且只入队一次。"""
        aid = self.make_account(write=True)
        job = Job("deliver", aid, auto=True)
        self.service._submit_later(job, 0.0)
        self.assertEqual(self.service._drain_timers(), 1)
        self.assertEqual(self.service._drain_timers(), 0)
        self.assertGreaterEqual(job.job_id, 1)

    def test_delayed_redelivery_actually_delivers(self):
        """延迟补投递必须真的跑到 _do_deliver 并再调一次 autoBind（不只是入队）。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=False, days=2)
        self.service.adapter = stub
        self.service._do_signin(Job("signin", aid), self.store.get_account(aid))
        immediate = len(stub.bind_calls)
        # 把 30 秒的延迟任务改成「已到点」，模拟时间流逝
        self.service._timers = [(time.time() - 1, j) for _, j in self.service._timers]
        self.service._drain_timers()
        queued = []
        while not self.service._q.empty():
            queued.append(self.service._q.get_nowait())
        deliver = [j for j in queued if j.kind == "deliver"]
        self.assertEqual(len(deliver), 1, "延迟补投递没有进队列")
        self.service._handle(deliver[0])
        self.assertEqual(deliver[0].status, "done")
        self.assertEqual(len(stub.bind_calls), immediate + 1, "延迟补投递没有再触发一次 autoBind")
        self.assertIn("后台自动补投递", deliver[0].message)

    def test_auto_redelivery_has_distinct_message(self):
        """后台自动补投递的文案要和手动点击区分，避免以为是用户自己点的。"""
        aid = self.make_account(write=True)
        stub = self.stub(today=True)
        self.service.adapter = stub
        job = Job("deliver", aid, auto=True)
        self.service._do_deliver(job, self.store.get_account(aid))
        self.assertEqual(job.status, "done")
        self.assertIn("后台自动补投递", job.message)

    def test_deliver_blocked_when_not_signed(self):
        aid = self.make_account(write=True)
        stub = self.stub(today=False)
        self.service.adapter = stub
        job = Job("deliver", aid)
        self.service._do_deliver(job, self.store.get_account(aid))
        self.assertEqual(job.status, "blocked")
        self.assertFalse(stub.bind_calls)

    def test_cannot_resign_when_no_resign_left(self):
        aid = self.make_account(write=True)
        self.service.adapter = self.stub(cnt=3, limit=3)
        job = Job("resign", aid)
        self.service._do_resign(job, self.store.get_account(aid))
        self.assertEqual(job.status, "blocked")
        self.assertIn("次数已用完", job.message)

    def test_cannot_resign_when_coin_short(self):
        aid = self.make_account(write=True)
        self.service.adapter = self.stub(coin=100, cost=200)
        job = Job("resign", aid)
        self.service._do_resign(job, self.store.get_account(aid))
        self.assertEqual(job.status, "blocked")
        self.assertIn("呗果不足", job.message)
        # 前置条件不满足不是账号异常，不能污染 last_error
        self.assertEqual(self.store.get_account(aid)["last_error"], "")

    def test_resign_requires_write_switch(self):
        aid = self.make_account()
        with self.assertRaises(WriteDisabled):
            self.service.resign_one(aid)

    def test_signinfo_cache_roundtrip(self):
        aid = self.make_account()
        self.store.set_signin_info(aid, {"coin": 210, "days": 5})
        got = self.store.get_signin_info(aid)
        self.assertEqual(got[aid]["data"]["coin"], 210)
        self.assertEqual(got[aid]["data"]["days"], 5)
        self.assertGreater(got[aid]["ts"], 0)


class TestIdempotency(BaseCase):
    def test_signin_reward_is_persisted(self):
        aid = self.make_account()
        reward = {"name": "资深猎人攻略", "num": 3, "icon": "x", "source": "record"}
        self.store.record_signin(aid, "2026-11-08", "1289", "success",
                                 mode="game", game_id="1289", days=2, reward=reward)
        row = self.store.signin_result_today(aid, "2026-11-08", "1289")
        self.assertEqual(row["mode"], "game")
        self.assertEqual(row["days"], 2)
        self.assertEqual(json.loads(row["reward_json"])["name"], "资深猎人攻略")
        self.assertEqual(json.loads(row["reward_json"])["num"], 3)

    def test_success_cannot_be_overwritten(self):
        aid = self.make_account()
        self.assertTrue(self.store.record_signin(aid, "2026-11-07", "1", "success"))
        self.assertFalse(self.store.record_signin(aid, "2026-11-07", "1", "failed"))
        self.assertTrue(self.store.is_signed_today(aid, "2026-11-07", "1"))
        row = self.store.signin_result_today(aid, "2026-11-07", "1")
        self.assertEqual(row["result"], "success")

    def test_failed_can_be_upgraded_to_success(self):
        aid = self.make_account()
        self.store.record_signin(aid, "2026-11-07", "1", "failed")
        self.assertFalse(self.store.is_signed_today(aid, "2026-11-07", "1"))
        self.store.record_signin(aid, "2026-11-07", "1", "success")
        self.assertTrue(self.store.is_signed_today(aid, "2026-11-07", "1"))

    def test_queue_recover(self):
        aid = self.make_account()
        self.store.enqueue_signin("2026-11-07", [aid, aid])
        self.assertEqual(len(self.store.pending_signin("2026-11-07")), 1)
        self.store.set_queue_status(1, "running")
        self.assertEqual(self.store.recover_queue(), 1)
        self.assertEqual(self.store.pending_signin("2026-11-07")[0]["status"], "pending")


class TestService(BaseCase):
    def test_write_disabled_by_default(self):
        aid = self.make_account()
        with self.assertRaises(WriteDisabled):
            self.service.signin_one(aid)

    def test_signin_key_is_game_id(self):
        self.assertEqual(self.service._signin_mode(), "game")
        self.assertEqual(self.service._signin_key(), "1289")

    def test_signin_one_accepts_paced_from_api(self):
        """/api/signin 会传 paced=False；签名不匹配会在入队前抛 TypeError，
        导致签到请求根本发不出去（v0.7.3 修复的真实故障）。"""
        aid = self.make_account(write=True)
        self.assertIn("paced", inspect.signature(Service.signin_one).parameters)
        manual = self.service.signin_one(aid, paced=False)
        self.assertEqual(manual.kind, "signin")
        self.assertFalse(manual.paced)
        self.assertTrue(self.service.signin_one(aid).paced)

    def test_manual_signin_really_submits_and_records(self):
        """手动点击链路：signin_one → _handle → 游戏内签到 → 幂等落库 + 奖励。"""
        aid = self.make_account(write=True)
        calls = {"n": 0}

        def fake_state(*a, **k):
            return {"todaySign": False, "days": 1, "day": 8, "month": 11, "reSignCnt": 0}

        def fake_sign(*a, **k):
            calls["n"] += 1
            return {"result": "success", "code": "0", "msg": "ok", "data": None}

        def fake_records(*a, **k):
            return [{"createTime": int(time.time() * 1000), "name": "资深猎人攻略",
                     "num": 3, "icon": ""}]

        with mock.patch.object(self.adapter, "fetch_sign_state", side_effect=fake_state), \
             mock.patch.object(self.adapter, "sign_game", side_effect=fake_sign), \
             mock.patch.object(self.adapter, "fetch_sign_reward_records",
                               side_effect=fake_records):
            job = self.service.signin_one(aid, paced=False)
            self.service._handle(job)
        date = time.strftime("%Y-%m-%d")
        key = self.service._signin_key()
        self.assertEqual(job.status, "done")
        self.assertEqual(calls["n"], 1)
        self.assertIn("资深猎人攻略 ×3", job.message)
        self.assertTrue(self.store.is_signed_today(aid, date, key))
        row = self.store.signin_result_today(aid, date, key)
        self.assertEqual(row["result"], "success")
        self.assertEqual(row["mode"], "game")
        self.assertEqual(row["days"], 2)
        self.assertIn("资深猎人攻略", row["reward_json"])

    def test_signin_all_keeps_serial_pacing(self):
        aid = self.make_account(write=True)
        result = self.service.signin_all()
        self.assertEqual(result["queued"], 1)
        with self.service._jobs_guard:
            jobs = [j for j in self.service._jobs.values() if j.kind == "signin"]
        self.assertTrue(jobs)
        self.assertTrue(all(j.paced for j in jobs))

    def test_account_mutex(self):
        aid = self.make_account()
        lock = self.service._account_lock(aid)
        self.assertTrue(lock.acquire(blocking=False))
        job = Job("refresh", aid)
        self.service._handle(job)
        self.assertEqual(job.status, "skipped")
        self.assertIn("互斥", job.message)
        lock.release()

    def test_refresh_and_signin_idempotent_flow(self):
        aid = self.make_account(write=True)
        mapped = self.adapter.map_role_home(sample_role_home())
        sign_calls = {"n": 0}

        def fake_signin(*a, **k):
            sign_calls["n"] += 1
            return {"result": "success", "code": "0", "msg": "ok", "data": None}

        def fake_state(*a, **k):
            return {"todaySign": False, "days": 4, "day": 8, "month": 11, "reSignCnt": 0}

        with mock.patch.object(self.adapter, "fetch_role_home", return_value=mapped), \
             mock.patch.object(self.adapter, "fetch_sign_state", side_effect=fake_state), \
             mock.patch.object(self.adapter, "sign_game", side_effect=fake_signin), \
             mock.patch.object(self.adapter, "fetch_sign_reward_records", return_value=[]), \
             mock.patch.object(self.adapter, "fetch_sign_rewards", return_value=[]):
            refresh = Job("refresh", aid)
            self.service._handle(refresh)
            self.assertEqual(refresh.status, "done")
            snap = self.store.latest_snapshot(aid)
            self.assertEqual((snap["stamina_value"], snap["stamina_max"]), (360, 360))
            self.assertEqual((snap["city_value"], snap["city_max"]), (580, 700))
            self.assertEqual((snap["activity_value"], snap["activity_max"]), (100, 100))
            self.assertIsNone(snap["weekly_value"])
            self.assertEqual(snap["weekly_value_source"], "unavailable")
            self.assertEqual(snap["weekly_available"], 0)
            self.assertEqual(snap["weekly_max"], 3)

            first = Job("signin", aid)
            self.service._handle(first)
            self.assertEqual(first.status, "done")
            self.assertEqual(sign_calls["n"], 1)
            self.assertTrue(self.store.is_signed_today(
                aid, time.strftime("%Y-%m-%d"), self.service._signin_key()))

            second = Job("signin", aid)
            self.service._handle(second)
            self.assertEqual(second.status, "skipped")
            self.assertEqual(sign_calls["n"], 1)  # 幂等：第二次根本不发请求

    def test_auth_expired_marks_reauth(self):
        aid = self.make_account(write=True)
        err = AdapterError("auth_expired", "凭证已失效")
        with mock.patch.object(self.adapter, "fetch_role_home", side_effect=err):
            job = Job("refresh", aid)
            self.service._handle(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.store.get_account(aid)["status"], "reauth")

    def test_weekly_visible_in_metrics_but_not_in_reminders(self):
        self.make_account()
        state = self.service.state()
        self.assertTrue(state["weekly"]["visible"])
        self.assertNotIn("weekly", state["summary"])
        self.assertEqual([m["key"] for m in state["metrics"]],
                         ["stamina", "cityStamina", "activity", "weeklyCopies"])
        self.assertIn("本周剩余", state["metrics"][-1]["hint"])

    def test_weekly_can_be_hidden_from_settings(self):
        self.make_account()
        self.store.set_setting("weeklyCopiesVisible", False)
        state = self.service.state()
        self.assertFalse(state["weekly"]["visible"])
        self.assertEqual(len(state["metrics"]), 3)
        self.assertNotIn("weeklyCopies", [m["key"] for m in state["metrics"]])

    def test_weekly_missing_hint_explains_conditional_field(self):
        """字段缺失时前端要有说明：官方只在打完后下发，别让人以为工具坏了。"""
        self.make_account()
        weekly = self.service.state()["weekly"]
        self.assertIn("打完", weekly["missingHint"])
        self.assertEqual(weekly["missingBadge"], "未同步")

    @staticmethod
    def weekly_metric(value, available):
        return {"weeklyCopies": {
            "value": value, "max": 3, "maxSource": "frontend_constant",
            "valueSource": "api" if available else "unavailable", "available": available}}

    def test_weekly_missing_after_done_means_pending_sync(self):
        """曾经下发过的号，字段这一次没带出来 —— 只能提示「待同步」，不许替官方推断次数。"""
        aid = self.make_account()
        self.store.save_snapshot(aid, self.weekly_metric(0, True), "fp1")   # 上周曾下发 0
        self.store.save_snapshot(aid, self.weekly_metric(None, False), "fp2")  # 这次整键消失
        entry = next(a for a in self.service.state()["accounts"] if a["id"] == aid)
        self.assertEqual(entry["snapshot"]["weekly_state"], "pending_sync")
        # 未下发就必须仍然是「没有值」，不能凭空造一个 3
        self.assertIsNone(entry["snapshot"]["weekly_value"])

    def test_weekly_missing_for_unknown_account_has_no_state(self):
        """从没收到过该字段的号（账号 1 那种）连「待同步」都不该显示，只留「—」。"""
        aid = self.make_account()
        self.store.save_snapshot(aid, self.weekly_metric(None, False), "fp1")
        snap = next(a for a in self.service.state()["accounts"] if a["id"] == aid)["snapshot"]
        self.assertNotIn("weekly_state", snap)

    def test_adapter_hot_reload(self):
        self.assertEqual(config.load_adapter("1.2.8")["_version"], "1.2.8")
        self.assertEqual(config.load_adapter("1.2.8"), CFG)  # 缓存命中


class TestLogScrub(BaseCase):
    def test_phone_token_and_captcha_masked(self):
        text = "手机号 13800138000 验证码 654321 accessToken=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmn"
        out = scrub_text(text)
        self.assertNotIn("13800138000", out)
        self.assertNotIn("654321", out)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", out)
        self.assertEqual(phone_mask("13800138000"), "138****8000")
        self.logger.info("test", text)
        logged = (self.dir / "run.log").read_text(encoding="utf-8")
        self.assertNotIn("13800138000", logged)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ", logged)


class TestWeeklyPersistence(BaseCase):
    def test_refresh_persists_real_zero(self):
        aid = self.make_account()
        data = sample_role_home()
        data["weekcopiesremainCnt"] = 0
        mapped = self.adapter.map_role_home(data)
        with mock.patch.object(self.adapter, "fetch_role_home", return_value=mapped):
            job = Job("refresh", aid)
            self.service._handle(job)
        self.assertEqual(job.status, "done")
        snap = self.store.latest_snapshot(aid)
        self.assertEqual(snap["weekly_value"], 0)
        self.assertEqual(snap["weekly_max"], 3)
        self.assertEqual(snap["weekly_value_source"], "api")
        self.assertEqual(snap["weekly_available"], 1)
        self.assertEqual(self.store.get_account(aid)["last_error"], "")
        self.assertIn("4 项", job.message)

    def test_legacy_db_gets_weekly_columns(self):
        """旧数据目录的 daily_snapshot 没有周本列，Store.init 必须自动补列。"""
        d = Path(tempfile.mkdtemp(prefix="nte-legacy-", dir=TMP))
        legacy = d / "legacy.db"
        con = sqlite3.connect(legacy)
        con.execute("""CREATE TABLE daily_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, ts INTEGER NOT NULL,
            stamina_value REAL, stamina_max REAL, stamina_max_source TEXT,
            city_value REAL, city_max REAL, city_max_source TEXT,
            activity_value REAL, activity_max REAL, activity_max_source TEXT,
            fingerprint TEXT NOT NULL DEFAULT '', ok INTEGER NOT NULL DEFAULT 1)""")
        con.commit()
        con.close()
        s = Store(legacy)
        cols = sqlite_columns(legacy, "daily_snapshot")
        for name in ("weekly_value", "weekly_max", "weekly_max_source",
                     "weekly_value_source", "weekly_available"):
            self.assertIn(name, cols)
        s.save_snapshot(1, self.adapter.map_role_home(sample_role_home())["metrics"], "fp")
        self.assertIsNotNone(s.latest_snapshot(1))


def sample_gacha() -> dict:
    """两池小样本：time 恒定（模拟官方缺陷），timeStamp 才是有区分度的出货时间。"""
    return {
        "rolename": "测试角色", "roleid": "999", "lev": 50,
        "luckTitle": "测试评价", "luckType": 6,
        "gachaDetails": [
            {"tab": "限定卡池", "drawCount": 150, "rareCount": 3,
             "average": "50.0", "m": 90, "playerOver": "50%",
             "details": [
                 {"charid": "1004", "rareCount": 80, "luckyType": 0,
                  "time": "2026-11-07", "timeStamp": 1794000000000},
                 {"charid": "1057", "rareCount": 40, "luckyType": 4,
                  "time": "2026-11-07", "timeStamp": 1793000000000},
                 {"charid": "1004", "rareCount": 30, "luckyType": 1,
                  "time": "2026-11-07", "timeStamp": 1792000000000},
             ]},
            {"tab": "弧盘池", "drawCount": 60, "rareCount": 2,
             "average": "30.0", "m": 60, "playerOver": "80%",
             "details": [
                 {"charid": "fork_a", "rareCount": 50, "luckyType": 3,
                  "time": "2026-11-07", "timeStamp": 1793500000000},
                 {"charid": "fork_b", "rareCount": 10, "luckyType": 0,
                  "time": "2026-11-07", "timeStamp": 1792500000000},
             ]},
        ],
    }


class TestGachaNormalize(unittest.TestCase):
    def setUp(self):
        self.names = {
            "1004": {"name": "安魂曲", "source": "api"},
            "fork_a": {"name": "弧盘甲", "source": "user"},
        }
        self.data = normalize(sample_gacha(), self.names, account_id=7)

    def test_totals_and_pool_closure(self):
        self.assertEqual((self.data["totalDraw"], self.data["totalRare"],
                          self.data["totalDetail"]), (210, 5, 5))
        for pool in self.data["pools"]:
            self.assertTrue(pool["drawConsistent"])
            self.assertEqual(pool["coverage"], 1.0)
            self.assertEqual(pool["drawSum"], pool["drawCount"])

    def test_detail_draws_and_cumulative_prefix_sum(self):
        """明细 rareCount = 本次出货花的抽数；累计 = 按出货时间的前缀和。"""
        p = self.data["pools"][0]
        self.assertEqual([d["draws"] for d in p["details"]], [80, 40, 30])
        self.assertEqual([d["cumulative"] for d in p["details"]], [150, 70, 30])
        self.assertEqual(self.data["pools"][1]["details"][0]["cumulative"], 60)
        self.assertEqual(p["details"][0]["pityPct"], 88.9)  # 80/90
        self.assertTrue(p["details"][0]["nearPity"])

    def test_item_rollup_is_character_level_draw_spend(self):
        """用户要的「各个卡池角色获取后花的抽数」：同一角色多次出货要合并。"""
        items = {x["itemId"]: x for x in self.data["pools"][0]["items"]}
        self.assertEqual(items["1004"]["times"], 2)
        self.assertEqual(items["1004"]["totalDraws"], 110)
        self.assertEqual(items["1004"]["avgDraws"], 55.0)
        self.assertEqual((items["1004"]["maxDraws"], items["1004"]["minDraws"]), (80, 30))
        self.assertEqual(items["1004"]["name"], "安魂曲")
        self.assertNotIn("fork_a", items)  # 弧盘只出现在弧盘池，不串池
        self.assertEqual(self.data["pools"][1]["items"][0]["name"], "弧盘甲")
        self.assertEqual(self.data["pools"][1]["items"][0]["nameSource"], "user")
        # 官方字典未覆盖的物品：显示 ID，绝不编名字
        self.assertEqual(items["1057"]["name"], "")
        self.assertEqual(items["1057"]["nameSource"], "none")

    def test_timestamp_not_broken_time_field(self):
        p = self.data["pools"][0]
        self.assertNotEqual(p["details"][0]["time"], p["details"][2]["time"])
        self.assertEqual(ts_millis(1794000000000), 1794000000000)
        self.assertEqual(ts_millis(1794000000), 1794000000000)  # 兼容秒级
        self.assertEqual(ts_text(0), "时间未知")

    def test_partial_coverage_is_flagged_not_fabricated(self):
        """官方滚动清理导致明细少于累计时，必须标记口径残缺，不伪装成完整总账。"""
        raw = sample_gacha()
        raw["gachaDetails"][0]["drawCount"] = 200
        p = normalize(raw, {}, account_id=7)["pools"][0]
        self.assertFalse(p["drawConsistent"])
        self.assertEqual(p["coverage"], 0.75)
        self.assertEqual(p["drawSum"], 150)
        self.assertEqual(p["details"][0]["cumulative"], 150)  # 只覆盖现存明细

    def test_extract_item_names_only_roles_and_forks(self):
        data = [
            {"id": "1004", "name": "安魂曲", "elementType": "X"},
            {"id": "fork_a", "name": "弧盘甲"},
            {"id": "city_ability_x", "name": "不该收的技能"},
            {"id": "9999", "name": "无角色特征，不该收"},
        ]
        self.assertEqual(extract_item_names(data),
                         {"1004": "安魂曲", "fork_a": "弧盘甲"})

    def test_summary_diff(self):
        self.assertFalse(summary_diff({"totalDraw": 10}, None)["hasPrevious"])
        d = summary_diff({"totalDraw": 12, "totalRare": 3},
                         {"total_draw": 10, "total_rare": 2, "ts": 123})
        self.assertEqual((d["drawDelta"], d["rareDelta"], d["previousAt"]), (2, 1, 123))


class TestGachaStoreAndService(BaseCase):
    def _fetch(self, job):
        with mock.patch.object(self.adapter, "fetch_item_names",
                               return_value={"1004": "安魂曲", "fork_a": "弧盘甲"}), \
             mock.patch.object(self.adapter, "fetch_gacha", return_value=sample_gacha()):
            self.service._handle(job)

    def test_gacha_is_readonly_even_when_writes_disabled(self):
        aid = self.make_account()  # writeActionsEnabled 默认 False
        job = self.service.gacha_one(aid, paced=False)
        self.assertEqual(job.kind, "gacha")
        self.assertFalse(job.paced)
        self.assertFalse(self.store.settings()["writeActionsEnabled"])
        self._fetch(job)
        self.assertEqual(job.status, "done")
        self.assertIn("210 抽", job.message)
        self.assertEqual(self.store.get_account(aid)["status"], "ok")

    def test_gacha_all_is_paced_and_serial(self):
        aid = self.make_account()
        self.assertEqual(self.service.gacha_all()["queued"], 1)
        with self.service._jobs_guard:
            jobs = [j for j in self.service._jobs.values() if j.kind == "gacha"]
        self.assertTrue(jobs and all(j.paced for j in jobs))
        self.assertEqual(aid, jobs[0].account_id)

    def test_gacha_failure_does_not_pollute_main_dashboard(self):
        aid = self.make_account()
        self.store.update_account(aid, last_error="")
        with mock.patch.object(self.adapter, "fetch_item_names", return_value={}), \
             mock.patch.object(self.adapter, "fetch_gacha",
                               side_effect=AdapterError("api_error", "模拟失败")):
            job = Job("gacha", aid)
            self.service._handle(job)
        self.assertEqual(job.status, "failed")
        self.assertEqual(self.store.get_account(aid)["last_error"], "")
        self.assertEqual(self.store.get_account(aid)["status"], "ok")

    def test_names_alias_priority_and_clear_restores_official(self):
        self.store.upsert_item_names({"1004": "安魂曲"})
        self.assertEqual(self.store.get_item_names()["1004"]["source"], "api")
        r = self.service.set_item_name("1004", "我的别名")
        self.assertEqual((r["name"], r["source"]), ("我的别名", "user"))
        self.store.upsert_item_names({"1004": "官方改名"})
        self.assertEqual(self.store.get_item_names()["1004"]["name"], "我的别名")
        r = self.service.set_item_name("1004", "")
        self.assertEqual((r["name"], r["source"]), ("官方改名", "api"))
        self.assertEqual(self.service.set_item_name("9999", "")["source"], "none")

    def test_snapshot_history_version_and_payload_alias(self):
        aid = self.make_account()
        job = Job("gacha", aid)
        self._fetch(job)
        first = self.store.gacha_version()
        self.store.upsert_item_names({"1004": "安魂曲"})
        job2 = Job("gacha", aid)
        self._fetch(job2)
        self.assertGreater(self.store.gacha_version(), first)
        self.assertEqual(len(self.store.gacha_history(aid)), 2)
        p = self.service.gacha_payload()
        self.assertEqual(p["version"], self.store.gacha_version())
        self.assertIn("accounts", p)
        acct = next(a for a in p["accounts"] if a["accountId"] == aid)
        self.assertTrue(acct["hasData"])
        self.assertTrue(acct["diff"]["hasPrevious"])
        self.assertEqual(acct["diff"]["drawDelta"], 0)
        named = [x for pool in acct["data"]["pools"] for x in pool["items"]
                 if x["itemId"] == "1004"]
        self.assertTrue(named and all(x["name"] == "安魂曲" for x in named))
        # 别名即时生效，且不改动任何抽数
        self.service.set_item_name("1004", "本地别名")
        p2 = self.service.gacha_payload()
        acct2 = next(a for a in p2["accounts"] if a["accountId"] == aid)
        named2 = [x for pool in acct2["data"]["pools"] for x in pool["items"]
                  if x["itemId"] == "1004"]
        self.assertTrue(all(x["name"] == "本地别名" and x["nameSource"] == "user"
                            for x in named2))
        self.assertEqual(acct2["data"]["totalDraw"], acct["data"]["totalDraw"])

    def test_state_has_lightweight_gacha_version(self):
        self.assertIn("gacha", self.service.state())
        self.assertIsInstance(self.service.state()["gacha"]["version"], int)

    def test_legacy_db_gets_gacha_payload_column(self):
        """旧数据目录若已有 gacha_snapshot 但缺 payload_json，必须自动补列。"""
        d = Path(tempfile.mkdtemp(prefix="nte-legacy-gacha-", dir=TMP))
        legacy = d / "legacy.db"
        con = sqlite3.connect(legacy)
        con.execute("""CREATE TABLE gacha_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, ts INTEGER NOT NULL,
            date TEXT NOT NULL, total_draw INTEGER, total_rare INTEGER,
            role_name TEXT, level INTEGER, luck_title TEXT,
            pools_json TEXT DEFAULT '[]', ok INTEGER DEFAULT 1)""")
        con.commit(); con.close()
        Store(legacy)
        self.assertIn("payload_json", sqlite_columns(legacy, "gacha_snapshot"))


class TestWeeklyFull(BaseCase):
    """都市活力：周一 05:00 归满，但服务端要等玩家花掉一点才回填（v1.1.4）。

    实测 2026-11-09：接口整周返回 0，进游戏花掉 54 后立刻变成 646。看板若不处理
    就会把「满」显示成「空」，这是比不显示更坏的错误。
    """

    def metrics(self, value, max_=700):
        return {"cityStamina": {"label": "都市活力", "value": value, "max": max_,
                                "maxSource": "api", "valueSource": "api"}}

    def test_reset_ts_is_this_week_monday_5am(self):
        now = datetime.datetime(2026, 11, 9, 10, 0).timestamp()   # 周一 10:00
        self.assertEqual(datetime.datetime.fromtimestamp(week_reset_ts(now)),
                         datetime.datetime(2026, 11, 9, 5, 0))

    def test_reset_ts_before_5am_counts_last_week(self):
        now = datetime.datetime(2026, 11, 9, 3, 0).timestamp()    # 周一 03:00，还没到刷新点
        self.assertEqual(datetime.datetime.fromtimestamp(week_reset_ts(now)),
                         datetime.datetime(2026, 11, 2, 5, 0))

    def test_zero_after_reset_shows_full(self):
        aid = self.make_account()
        m = self.metrics(0)
        self.service._apply_weekly_full(aid, m)
        city = m["cityStamina"]
        self.assertEqual(city["value"], 700)
        self.assertTrue(city["assumedFull"])
        self.assertEqual(city["valueSource"], "assumed_full")

    def test_real_zero_after_spending_is_kept(self):
        """本周读到过真实值（646）之后再出现 0，就是真的花光了，不能改成满。"""
        aid = self.make_account()
        self.store.save_snapshot(aid, self.metrics(646), "fp")
        m = self.metrics(0)
        self.service._apply_weekly_full(aid, m)
        self.assertEqual(m["cityStamina"]["value"], 0)
        self.assertNotIn("assumedFull", m["cityStamina"])

    def test_nonzero_value_untouched(self):
        aid = self.make_account()
        m = self.metrics(646)
        self.service._apply_weekly_full(aid, m)
        self.assertEqual(m["cityStamina"]["value"], 646)
        self.assertNotIn("assumedFull", m["cityStamina"])

    def test_assumed_flag_persisted_and_excluded_from_history(self):
        """估算值要落库（前端才能标角标），但不能被当成玩家真实余量。"""
        aid = self.make_account()
        m = self.metrics(0)
        self.service._apply_weekly_full(aid, m)
        self.store.save_snapshot(aid, m, "fp")
        snap = self.store.latest_snapshot(aid)
        self.assertEqual(snap["city_value"], 700)
        self.assertEqual(snap["city_assumed"], 1)
        self.assertIsNone(self.store.max_real_city_value_since(aid, 0))

    def old_snapshot(self, aid, value):
        """造一条「重置点之前」的快照，模拟上周的残留值。"""
        with self.store._con() as con:
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() - 3600), float(value), 700.0))

    def test_leftover_echo_on_reset_shows_full(self):
        """上周没花完（残留 256）：跨点后接口原样回显 256，实际已归满，必须按满显示。"""
        aid = self.make_account()
        self.old_snapshot(aid, 256)
        m = self.metrics(256)
        self.service._apply_weekly_full(aid, m)
        city = m["cityStamina"]
        self.assertEqual(city["value"], 700)
        self.assertEqual(city["assumedReason"], "echo")

    def test_changed_value_after_reset_is_trusted(self):
        """数值变了就说明服务端真的重算过（比如已花掉一些），这时照抄。"""
        aid = self.make_account()
        self.old_snapshot(aid, 256)
        m = self.metrics(300)
        self.service._apply_weekly_full(aid, m)
        self.assertEqual(m["cityStamina"]["value"], 300)
        self.assertNotIn("assumedFull", m["cityStamina"])

    def test_echo_detected_when_last_snapshot_was_assumed(self):
        """上周最后一条是「按满估算」时，回显的残留值仍要能认出来。

        估算行不算真实读数，2 天窗口内于是取不到值，必须退回「重置点前最后一条真实
        快照」，否则会把回显当成本周新值照抄。
        """
        aid = self.make_account()
        with self.store._con() as con:
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() - 3 * 86400), 256.0, 700.0))
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,1)",
                (aid, int(week_reset_ts() - 3600), 700.0, 700.0))
        # 2 天窗口内没有真实读数（最后一条是估算行）→ 走「重置点前最后一条真实快照」兜底
        self.assertEqual(self.store.city_values_before(aid, week_reset_ts()), set())
        self.assertEqual(
            float(self.store.last_city_before(aid, week_reset_ts())["city_value"]), 256.0)
        m = self.metrics(256)
        self.service._apply_weekly_full(aid, m)
        self.assertEqual(m["cityStamina"]["value"], 700)
        self.assertEqual(m["cityStamina"]["assumedReason"], "echo")

    def test_same_habitual_spend_not_mistaken_for_echo(self):
        """每周一都花同样多（上周读到 646、本周又读到 646）不能被当成回显改回满。"""
        aid = self.make_account()
        with self.store._con() as con:
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() - 5 * 86400), 646.0, 700.0))
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() - 86400), 60.0, 700.0))
        m = self.metrics(646)
        self.service._apply_weekly_full(aid, m)
        self.assertEqual(m["cityStamina"]["value"], 646)
        self.assertNotIn("assumedFull", m["cityStamina"])

    def test_stale_snapshot_displayed_full_in_state(self):
        """周一 05:00 之后看板还捧着周日快照时，直接按满展示，不等刷新、也不改库。"""
        aid = self.make_account()
        with self.store._con() as con:
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() - 7200), 256.0, 700.0))
        account = next(a for a in self.service.state()["accounts"] if a["id"] == aid)
        self.assertEqual(account["snapshot"]["city_value"], 700)
        self.assertEqual(account["snapshot"]["city_assumed"], 1)
        # 展示层不落库：库里还是当时抓到的 256
        with self.store._con() as con:
            row = con.execute(
                "SELECT city_value, city_assumed FROM daily_snapshot WHERE account_id=?",
                (aid,)).fetchone()
        self.assertEqual((row["city_value"], row["city_assumed"]), (256.0, 0))

    def test_fresh_snapshot_not_overridden_in_state(self):
        """本周重置点之后抓的快照（哪怕只有 300）说明服务端已重算，展示层不许改。"""
        aid = self.make_account()
        with self.store._con() as con:
            con.execute(
                "INSERT INTO daily_snapshot(account_id, ts, city_value, city_max, city_assumed) "
                "VALUES(?,?,?,?,0)",
                (aid, int(week_reset_ts() + 60), 300.0, 700.0))
        account = next(a for a in self.service.state()["accounts"] if a["id"] == aid)
        self.assertEqual(account["snapshot"]["city_value"], 300)
        self.assertEqual(account["snapshot"]["city_assumed"], 0)

    def test_echo_keeps_assuming_until_real_read(self):
        """一直回显就一直按满显示，直到本周读到过真值为止。"""
        aid = self.make_account()
        self.old_snapshot(aid, 256)
        first = self.metrics(256)
        self.service._apply_weekly_full(aid, first)
        self.store.save_snapshot(aid, first, "fp")       # 估算行（city_assumed=1）
        second = self.metrics(256)
        self.service._apply_weekly_full(aid, second)
        self.assertEqual(second["cityStamina"]["value"], 700)
        # 玩家进游戏花掉一些后接口给真值 -> 以真值为准
        third = self.metrics(612)
        self.service._apply_weekly_full(aid, third)
        self.assertEqual(third["cityStamina"]["value"], 612)

    def test_week_reset_forces_one_refresh(self):
        """跨过周一 05:00 后要立刻补刷过期快照，且同一周只做一次。"""
        self.make_account()
        self.assertEqual(self.service.refresh_if_week_reset(), 1)
        self.assertEqual(self.service.refresh_if_week_reset(), 0)
        kinds = []
        while not self.service._q.empty():
            kinds.append(self.service._q.get_nowait().kind)
        self.assertIn("refresh", kinds)

    def test_week_reset_skips_fresh_snapshot(self):
        """本周已经刷新过（快照晚于重置点）就不要额外打请求。"""
        aid = self.make_account()
        self.store.save_snapshot(aid, self.metrics(646), "fp")
        self.assertEqual(self.service.refresh_if_week_reset(), 0)

    def test_legacy_db_gets_city_assumed_column(self):
        """旧数据目录缺 city_assumed 列时必须自动补齐，否则刷新直接报错。"""
        d = Path(tempfile.mkdtemp(prefix="nte-legacy-city-", dir=TMP))
        legacy = d / "legacy.db"
        con = sqlite3.connect(legacy)
        con.execute("""CREATE TABLE daily_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL,
            ts INTEGER NOT NULL, stamina_value REAL, city_value REAL, city_max REAL,
            fingerprint TEXT DEFAULT '', ok INTEGER DEFAULT 1)""")
        con.commit(); con.close()
        Store(legacy)
        self.assertIn("city_assumed", sqlite_columns(legacy, "daily_snapshot"))


class TestAuthSessionResume(BaseCase):
    """授权会话必须能在「误点弹窗外 / 刷新页面」之后续上。

    旧行为：会话只存在浏览器变量里，关掉弹窗就丢了 sessionId，而服务端仍占着
    这个会话，start() 直接抛「已有一个授权会话在进行中」，用户只能干等 10 分钟。
    """

    def setUp(self):
        super().setUp()
        self.sent = []

        class StubAdapter:
            def send_captcha(inner, phone, device_id):   # noqa: N805
                self.sent.append((phone, device_id))

        self.auth = AuthManager(self.store, self.vault, StubAdapter(), self.logger)

    def test_same_phone_resumes_without_resending(self):
        first = self.auth.start("13800000000")
        self.assertFalse(first["resumed"])
        again = self.auth.start("13800000000")
        self.assertTrue(again["resumed"])
        self.assertEqual(again["sessionId"], first["sessionId"])
        self.assertEqual(len(self.sent), 1)          # 没有重复发短信
        self.assertGreater(again["expiresIn"], 0)

    def test_pending_exposes_live_session(self):
        sid = self.auth.start("13800000000")["sessionId"]
        p = self.auth.pending()
        self.assertEqual(p["sessionId"], sid)
        self.assertEqual(p["phoneMask"], "138****0000")
        self.assertGreater(p["expiresIn"], 0)

    def test_pending_is_none_without_session_and_after_expiry(self):
        self.assertIsNone(self.auth.pending())
        self.auth.start("13800000000")
        next(iter(self.auth._sessions.values()))["createdAt"] -= SESSION_TTL + 1
        self.assertIsNone(self.auth.pending())

    def test_force_resend_replaces_session(self):
        first = self.auth.start("13800000000")
        self.auth._last_sent["13800000000"] = 0       # 跳过 60 秒冷却，只测会话替换
        second = self.auth.start("13800000000", force=True)
        self.assertFalse(second["resumed"])
        self.assertNotEqual(second["sessionId"], first["sessionId"])
        self.assertEqual(len(self.sent), 2)
        self.assertNotIn(first["sessionId"], self.auth._sessions)

    def test_force_resend_still_respects_cooldown(self):
        self.auth.start("13800000000")
        with self.assertRaises(AuthError):
            self.auth.start("13800000000", force=True)
        self.assertEqual(len(self.sent), 1)

    def test_other_phone_is_not_blocked(self):
        """换号授权不该被上一个号残留的会话挡住。"""
        self.auth.start("13800000000")
        other = self.auth.start("13900000000")
        self.assertFalse(other["resumed"])
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(self.auth.pending()["phoneMask"], "139****0000")

    def test_lost_session_id_gives_actionable_error(self):
        """真的丢了会话时，提示必须是「重新发送验证码」而不是一句失败的英文。"""
        self.auth.start("13800000000")
        with self.assertRaises(AuthError) as ctx:
            self.auth.complete("bogus-sid", "123456", skip_check=True)
        self.assertIn("重新发送", str(ctx.exception))


if __name__ == "__main__":
    unittest.main(verbosity=2)
