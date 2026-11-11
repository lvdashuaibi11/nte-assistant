"""服务层：串行队列、账号互斥锁、限速、失败隔离、断点续签、幂等。

对应开发计划 4.3 / 7.2 / 7.3：
- 所有网络任务进单 worker 串行执行，账号无上限也不会打满请求；
- 每账号一把锁，并发触发同一账号的刷新/签到直接跳过（不产生凭证作废）；
- 签到以 (账号, 日期, communityId) 唯一键幂等，当日已成功根本不发请求；
- 队列持久化，进程中断重启后 running → pending 继续跑；
- 单账号异常只影响该账号，队列继续。
"""
from __future__ import annotations

import json
import datetime
import queue as pyqueue
import random
import threading
import time
from dataclasses import dataclass, field

from . import config
from .adapter import Adapter, AdapterError
from .auth import AuthManager, AuthError
from .gacha import name_of, normalize, summary_diff
from .logutil import phone_mask


class WriteDisabled(RuntimeError):
    """写操作总开关关闭时拒绝签到。"""


@dataclass
class Job:
    kind: str                  # refresh | signin | gacha（抽卡只读，但仍走同一串行队列限速）
    account_id: int
    job_id: int = 0
    paced: bool = True
    queue_id: int | None = None
    status: str = "pending"    # pending | running | done | skipped | failed | blocked
    message: str = ""
    created_at: float = field(default_factory=time.time)
    auto: bool = False         # 后台自动发起的任务（如签到后的延迟补投递），文案与手动区分

    def public(self) -> dict:
        return {k: getattr(self, k) for k in
                ("job_id", "kind", "account_id", "status", "message", "created_at")}


# 签到成功后隔多久再自动补一次投递（秒）。
# 原因：sign 只写社区侧记录，autoBindGameRole 要把它推进游戏邮箱；这一秒内提交的
# sign 可能还没在游戏服侧落成「待发放」，紧接着同秒投递会空跑。延迟一次即覆盖该窗口。
DELIVER_RETRY_DELAY = 30.0


# 指标 key → 快照表列前缀（与 Store.save_snapshot 的 prefixes 保持一致）
_METRIC_PREFIX = {
    "stamina": "stamina", "cityStamina": "city",
    "activity": "activity", "weeklyCopies": "weekly",
}


def week_reset_ts(now: float | None = None, weekday: int = 0, hour: int = 5) -> float:
    """最近一次「周重置」时刻：默认周一 05:00（本地时区）。

    都市活力是周刷新资源：周一 05:00 归满。但服务端不在刷新点主动回填，
    要等玩家在游戏里花掉第一点才重算，所以刷新点之后接口会持续返回 0。
    """
    t = datetime.datetime.fromtimestamp(now if now is not None else time.time())
    monday = t.date() - datetime.timedelta(days=t.weekday())
    reset = datetime.datetime.combine(
        monday + datetime.timedelta(days=max(0, min(6, weekday))),
        datetime.time(max(0, min(23, hour)), 0))
    if reset > t:
        reset -= datetime.timedelta(days=7)
    return reset.timestamp()


class Service:
    def __init__(self, store, vault, logger, adapter: Adapter | None = None):
        self.store = store
        self.vault = vault
        self.log = logger
        settings = store.settings()
        self.adapter = adapter or Adapter(
            config.load_adapter(settings["adapterVersion"]), logger
        )
        self.auth = AuthManager(store, vault, self.adapter, logger)
        self._q: pyqueue.Queue[Job] = pyqueue.Queue()
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()
        self._jobs: dict[int, Job] = {}
        self._jobs_guard = threading.Lock()
        self._seq = 0
        self._running: Job | None = None
        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._work, name="nte-worker", daemon=True)
        self._scheduler = threading.Thread(target=self._schedule, name="nte-scheduler", daemon=True)
        self._timer = threading.Thread(target=self._timer_loop, name="nte-timer", daemon=True)
        self._timers: list[tuple[float, Job]] = []
        self._timers_guard = threading.Lock()
        self._last_auto_refresh = 0.0
        self._last_auto_signin_date = ""
        self._week_reset_seen = 0.0

    # ------------------------------------------------------------ 生命周期
    def start(self) -> None:
        recovered = self.store.recover_queue()
        if recovered:
            self.log.info("queue.recover", f"断点续签：恢复 {recovered} 个中断任务")
        pending = self.store.pending_signin(time.strftime("%Y-%m-%d"))
        if pending and self.store.settings()["writeActionsEnabled"]:
            for item in pending:
                self._submit(Job("signin", item["account_id"], queue_id=item["id"]))
        elif pending:
            self.log.info("queue.hold", f"{len(pending)} 个待签任务保留在队列中，写操作开关关闭，未执行")
        self._worker.start()
        self._scheduler.start()
        self._timer.start()
        self.log.info("timer.start", "延迟任务调度已启动（签到后自动补投递用）")
        self._prime_signinfo()

    def _prime_signinfo(self) -> None:
        """启动时补一次签到详情缓存（只读）：呗果余额 / 补签次数 20 分钟内有效。

        只对启用中、且缓存缺失或过期的账号入队，避免每次重启都打一遍全部账号。
        """
        cached = self.store.get_signin_info()
        now = time.time()
        ids = []
        for a in self.store.list_accounts():
            if not a["enabled"] or a["status"] == "disabled":
                continue
            entry = cached.get(a["id"])
            if not entry or now - entry["ts"] > 20 * 60:
                ids.append(a["id"])
        for aid in ids:
            self._submit(Job("signinfo", aid, paced=True))
        if ids:
            self.log.info("queue.signinfo", f"启动预热签到详情 {len(ids)} 个账号（只读，串行 + 限速）")

    def stop(self) -> None:
        self._stop.set()

    def _account_lock(self, account_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(account_id, threading.Lock())

    def _submit(self, job: Job) -> Job:
        with self._jobs_guard:
            self._seq += 1
            job.job_id = self._seq
            self._jobs[job.job_id] = job
            if len(self._jobs) > 200:
                for old in sorted(self._jobs)[:50]:
                    self._jobs.pop(old, None)
        self._q.put(job)
        return job

    def _submit_later(self, job: Job, delay: float) -> Job:
        """延迟入队：到点后仍然进同一条串行队列，享受账号锁与限速。

        只存在内存里（进程重启即失效），用于「签到后隔 30 秒再自动补一次投递」
        这类一次性的保险动作，丢了也无所谓——真没收到还可以手动点补投递。
        """
        with self._timers_guard:
            self._timers.append((time.time() + max(0.0, delay), job))
        return job

    def _drain_timers(self) -> int:
        """把所有到点的延迟任务提交进队列，返回到点个数（供测试直接调用）。"""
        now = time.time()
        with self._timers_guard:
            due = [j for t, j in self._timers if t <= now]
            if due:
                self._timers = [(t, j) for t, j in self._timers if t > now]
        for job in due:
            self._submit(job)
        return len(due)

    def _timer_loop(self) -> None:
        while not self._stop.wait(2.0):
            try:
                self._drain_timers()
            except Exception as exc:  # 定时线程绝不能死
                self.log.warn("timer.error", f"延迟任务调度异常：{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------ 对外操作
    def refresh_one(self, account_id: int, paced: bool = False) -> Job:
        return self._submit(Job("refresh", account_id, paced=paced))

    def refresh_all(self) -> dict:
        settings = self.store.settings()
        now = time.time()
        stale_after = int(settings["refreshIntervalMin"]) * 60
        batch = int(settings["batchSize"])
        candidates = [
            a for a in self.store.list_accounts()
            if a["enabled"] and a["status"] != "disabled"
            and (not a.get("last_refresh_at") or now - a["last_refresh_at"] >= stale_after)
        ]
        candidates.sort(key=lambda a: a.get("last_refresh_at") or 0)
        for a in candidates[:max(batch, 1)]:
            self._submit(Job("refresh", a["id"]))
        return {"queued": min(len(candidates), max(batch, 1)),
                "eligible": len(candidates), "batchSize": batch}

    def gacha_one(self, account_id: int, paced: bool = False) -> Job:
        """只读抓取抽卡统计。只读 GET 不受写操作开关限制（不会提交任何写请求）。"""
        return self._submit(Job("gacha", account_id, paced=paced))

    def gacha_all(self) -> dict:
        ids = [
            a["id"] for a in self.store.list_accounts()
            if a["enabled"] and a["status"] != "disabled"
        ]
        for aid in ids:
            self._submit(Job("gacha", aid, paced=True))
        self.log.info("queue.gacha",
                      f"抽卡统计只读抓取入队 {len(ids)} 个账号（串行 + 限速）")
        return {"queued": len(ids)}

    def signinfo_one(self, account_id: int, paced: bool = False) -> Job:
        """只读：签到详情（呗果余额 / 补签次数 / 30 天奖励表 / 到账流水）。

        纯只读 GET，不受写操作开关限制；结果缓存进 signin_info 表供看板直接渲染。
        """
        return self._submit(Job("signinfo", account_id, paced=paced))

    def signinfo_all(self) -> dict:
        ids = [
            a["id"] for a in self.store.list_accounts()
            if a["enabled"] and a["status"] != "disabled"
        ]
        for aid in ids:
            self._submit(Job("signinfo", aid, paced=True))
        self.log.info("queue.signinfo", f"签到详情只读查询入队 {len(ids)} 个账号（串行 + 限速）")
        return {"queued": len(ids)}

    def resign_one(self, account_id: int) -> Job:
        """单账号补签（写操作）：消耗官方「呗果」积点，每月上限 3 次。"""
        self._assert_write()
        return self._submit(Job("resign", account_id, paced=False))

    def deliver_one(self, account_id: int) -> Job:
        """单账号补投递（写操作）：已签到但邮箱没收到奖励时，触发一次角色联动。"""
        self._assert_write()
        return self._submit(Job("deliver", account_id, paced=False))

    def signin_one(self, account_id: int, paced: bool = True) -> Job:
        """单账号签到。paced=False 用于 UI 手动点击，跳过队列间的随机限速。

        注意：/api/signin 会传 paced=False，签名必须保留该参数，
        否则会在入队前抛 TypeError，导致请求根本发不出去（v0.7.3 修复）。
        """
        self._assert_write()
        return self._submit(Job("signin", account_id, paced=paced))

    def signin_all(self) -> dict:
        self._assert_write()
        date = time.strftime("%Y-%m-%d")
        key = self._signin_key()
        ids = [
            a["id"] for a in self.store.list_accounts()
            if a["enabled"] and a["status"] != "disabled"
            and not self.store.is_signed_today(a["id"], date, key)
        ]
        self.store.enqueue_signin(date, ids)
        pending = {i["account_id"]: i["id"] for i in self.store.pending_signin(date)}
        for aid in ids:
            self._submit(Job("signin", aid, queue_id=pending.get(aid)))
        self.log.info("queue.signin", f"批量签到入队 {len(ids)} 个账号（串行 + 限速）")
        return {"queued": len(ids), "date": date}

    def _assert_write(self) -> None:
        if not self.store.settings()["writeActionsEnabled"]:
            raise WriteDisabled(
                "写操作总开关未开启：请在「设置」里打开「允许写操作」后再签到。"
                "关闭时本工具不会提交任何签到请求。"
            )

    def update_settings(self, values: dict) -> dict:
        old = self.store.settings()
        new = self.store.set_settings(values)
        if new["adapterVersion"] != old["adapterVersion"]:
            self.adapter = Adapter(config.load_adapter(new["adapterVersion"]), self.log)
            self.auth.adapter = self.adapter
            self.log.info("adapter.reload", f"已热加载适配器 {new['adapterVersion']}")
        if old["automationEnabled"] and not new["automationEnabled"]:
            self.log.warn("automation.off", "自动化已一键停用，队列保留但不再自动执行")
        return new

    def delete_account(self, account_id: int) -> None:
        account = self.store.get_account(account_id)
        if account and account.get("credential_ref"):
            self.vault.delete(account["credential_ref"])
        self.store.delete_account(account_id)
        self.log.info("account.delete", f"已删除账号 {account_id} 及其凭证")

    # ------------------------------------------------------------ worker
    def _work(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._q.get(timeout=0.5)
            except pyqueue.Empty:
                continue
            self._running = job
            try:
                self._handle(job)
            except Exception as exc:  # 失败隔离：任何异常都不终止 worker
                job.status = "failed"
                job.message = f"{type(exc).__name__}: {str(exc)[:160]}"
                self.log.error("job.unhandled", f"账号 {job.account_id} 任务异常：{job.message}")
            finally:
                self._running = None
                self._q.task_done()
            if job.paced:
                settings = self.store.settings()
                lo = float(settings["minIntervalSec"])
                hi = float(settings["maxIntervalSec"])
                self._stop.wait(random.uniform(max(lo, 0), max(hi, lo + 0.1)))
            else:
                self._stop.wait(0.2)

    def _handle(self, job: Job) -> None:
        account = self.store.get_account(job.account_id)
        if not account:
            job.status, job.message = "skipped", "账号不存在"
            return
        if not account["enabled"] or account["status"] == "disabled":
            job.status, job.message = "skipped", "账号已停用"
            return
        lock = self._account_lock(account["id"])
        if not lock.acquire(blocking=False):
            job.status, job.message = "skipped", "该账号有任务正在执行，已跳过（账号互斥）"
            self.log.warn("job.busy", f"账号 {account['id']} 并发任务被互斥锁拦截")
            return
        try:
            if job.kind == "refresh":
                self._do_refresh(job, account)
            elif job.kind == "signin":
                self._do_signin(job, account)
            elif job.kind == "gacha":
                self._do_gacha(job, account)
            elif job.kind == "signinfo":
                self._do_signinfo(job, account)
            elif job.kind == "resign":
                self._do_resign(job, account)
            elif job.kind == "deliver":
                self._do_deliver(job, account)
            else:
                job.status, job.message = "failed", "未知任务类型"
        finally:
            lock.release()

    def _credential(self, account: dict) -> dict:
        try:
            return self.auth.credential(account)
        except AuthError as exc:
            self.store.update_account(account["id"], status="reauth", last_error=str(exc))
            self.store.add_audit(account["id"], "credential_missing", "reauth")
            raise

    def _week_view(self, snap: dict, reset: float) -> None:
        """看板展示层：快照比本周重置点还旧，周刷新资源一律按上限显示（v1.1.6）。

        这是用户要的那条规则本身——「周一 05:00 就是满的，这是事实，不用等接口承认」。
        只要手里这条快照是重置点之前抓的（可能是上周日 23:00），它里面的周刷新数值
        一定已经作废，直接按上限展示，不必等服务端回填、也不必等下一次刷新。
        只在展示层改，**不落库**：历史里保留当时的真实读数。
        接口一旦给回本周真实值（快照 ts >= 重置点），这段就自动不再介入。
        """
        ts = snap.get("ts") or 0
        if ts >= reset:
            return
        for spec in self.adapter.cfg["metrics"]:
            if not spec.get("weeklyReset"):
                continue
            prefix = _METRIC_PREFIX.get(spec["key"])
            if not prefix:
                continue
            cap = snap.get(f"{prefix}_max")
            if not cap or float(cap) <= 0:
                continue
            if float(snap.get(f"{prefix}_value") or 0) >= float(cap):
                continue
            snap[f"{prefix}_value"] = float(cap)
            snap[f"{prefix}_assumed"] = 1

    def _apply_weekly_full(self, account_id: int, metrics: dict) -> None:
        """刷新层：周刷新资源在「重置后、服务端未回算」时按上限显示（v1.1.6）。

        模型很简单，和用户说的一致：**周一 05:00 就是满的**，默认按满显示；接口给出
        本周真实值就用真实值覆盖。

        接口只有两种时机需要拦住，否则会把「满」显示成别的数：
        · 返回 0 —— 服务端还没被玩家花的第一点触发重算，0 不是余量；
        · 返回上周残留值（如 256）—— 上周没花完时服务端原样回显，也不是本周余量。
        两种情况都要求「本周还没读到过任何真实值」；一旦本周出现过真实值，接口给什么
        就是什么（包括真花到 0），绝不再改。
        """
        reset = week_reset_ts()
        for spec in self.adapter.cfg["metrics"]:
            rule = spec.get("weeklyReset")
            if not rule or spec["key"] != "cityStamina":
                continue
            m = metrics.get(spec["key"])
            if not m or not m.get("max"):
                continue
            cap = float(m["max"])
            value = float(m.get("value") or 0)
            if cap <= 0 or value >= cap:
                continue
            # 本周已有真实读数 → 这个值（哪怕是 0、哪怕和上周一样）都是真的，照抄
            if (self.store.max_real_city_value_since(account_id, reset) or 0) > 0:
                continue
            reason = None
            if value == 0:
                reason = "zero"
            else:
                # 回显 = 跨过重置点后这个数「一动没动」。只认同重置点前 2 天内出现过
                # 的真实值（活跃玩家的最后读数必然在这里），避免把「本周照旧花掉同样多
                # 活力」读到的同一个数误判成上周回显。若 2 天内没有真实记录（几天没玩），
                # 退回「重置点前最后一条真实快照」。
                recent = self.store.city_values_before(account_id, reset)
                if not recent:
                    last = self.store.last_city_before(account_id, reset)
                    if last and last.get("city_value") is not None:
                        recent = {float(last["city_value"])}
                if value in recent:
                    reason = "echo"
            if not reason:
                continue
            m["value"] = cap
            m["valueSource"] = "assumed_full"
            m["assumedFull"] = True
            m["assumedReason"] = reason
            detail = ("接口返回 0" if reason == "zero"
                      else f"接口回显上周残留值 {value:g}")
            self.log.info(
                "metric.assumed_full",
                f"账号 {account_id} {m['label']} {detail}（周重置后服务端未回算），"
                f"按上限 {cap:g} 显示")

    def _do_refresh(self, job: Job, account: dict) -> None:
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid, device_id = cred.get("uid") or account["uid"], cred.get("deviceId") or account["device_id"]
        role_id = account.get("role_id") or ""
        job.status = "running"
        try:
            if not role_id:
                roles = self.adapter.get_game_roles(token, uid, device_id)
                if roles:
                    r = roles[0]
                    role_id = str(r.get("roleId") or "")
                    self.store.update_account(
                        account["id"], role_id=role_id,
                        role_name=str(r.get("roleName") or ""),
                        server_id=str(r.get("serverId") or ""),
                    )
            if not role_id:
                raise AdapterError("api_error", "该账号未取到异环角色 roleId，请重新授权")
            result = self.adapter.fetch_role_home(token, uid, device_id, role_id)
            self._apply_weekly_full(account["id"], result["metrics"])
            self.store.save_snapshot(account["id"], result["metrics"], result["fingerprint"])
            # roleHome 自带 characters，顺手维护物品名称字典（零额外请求）
            if result.get("itemNames"):
                self.store.upsert_item_names(result["itemNames"])
            if result.get("roleName"):
                self.store.update_account(account["id"], role_name=result["roleName"])
            self.store.update_account(
                account["id"], status="ok",
                last_error="；".join(result["warnings"])[:200] if result["warnings"] else "",
            )
            job.status = "done"
            job.message = f"{len(result['metrics'])} 项数据已更新（指纹 {result['fingerprint']}）"
            for w in result["warnings"]:
                self.log.warn("adapter.structure", w)
            # 周本相关提示只记 INFO，不进 last_error、不计入提醒
            for n in result.get("notices", []):
                self.log.info("adapter.notice", n)
            self.log.info("refresh.ok", f"账号 {account['id']} 数据刷新成功")
        except AdapterError as exc:
            self._on_adapter_error(job, account, exc)

    def _signin_mode(self) -> str:
        return str(self.adapter.cfg.get("signinMode") or "game")

    def _signin_key(self) -> str:
        """幂等键：游戏签到用 gameId（1289），社区签到用 communityId。"""
        if self._signin_mode() == "game":
            return str(self.adapter.cfg.get("gameId") or "1289")
        return str(self.adapter.cfg["communityId"])

    def _do_signin(self, job: Job, account: dict) -> None:
        date = time.strftime("%Y-%m-%d")
        key = self._signin_key()
        mode = self._signin_mode()
        if not self.store.settings()["writeActionsEnabled"]:
            job.status, job.message = "blocked", "写操作开关关闭，未发送任何请求"
            if job.queue_id:
                self.store.set_queue_status(job.queue_id, "blocked", job.message)
            return
        # 幂等第一道：本地唯一键，当日已成功/已签直接跳过，根本不发请求
        if self.store.is_signed_today(account["id"], date, key):
            job.status, job.message = "skipped", "今日已签到（幂等跳过）"
            if job.queue_id:
                self.store.set_queue_status(job.queue_id, "done", job.message)
            return
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid = cred.get("uid") or account["uid"]
        device_id = cred.get("deviceId") or account["device_id"]
        job.status = "running"
        started = time.time()
        try:
            if mode == "game":
                self._signin_game(job, account, date, key, token, uid, device_id)
            else:
                self._signin_community(job, account, date, key, token, uid, device_id)
        except AdapterError as exc:
            self.store.record_signin(
                account["id"], date, key, "failed",
                code=str(exc.code or ""), msg=exc.message,
                elapsed_ms=int((time.time() - started) * 1000),
                mode=mode, game_id=key if mode == "game" else "",
            )
            self._on_adapter_error(job, account, exc)
            if job.queue_id:
                self.store.set_queue_status(job.queue_id, "failed", exc.message[:120])

    def _signin_community(self, job: Job, account: dict, date: str, community: str,
                          token: str, uid: str, device_id: str) -> None:
        """旧社区版区签到：只发社区经验/金币，不是游戏奖励；仅保留为可选模式。"""
        started = time.time()
        try:
            state = self.adapter.get_sign_state(token, uid, device_id)
            self.log.info("signin.state", f"账号 {account['id']} 社区状态={state}")
        except AdapterError as exc:
            self.log.warn("signin.state", f"社区状态查询失败：{exc.kind}")
        result = self.adapter.signin(token, uid, device_id)
        self.store.record_signin(
            account["id"], date, community, result["result"],
            code=result.get("code", ""), msg=result.get("msg", ""),
            elapsed_ms=int((time.time() - started) * 1000), mode="community",
        )
        self.store.update_account(account["id"], status="ok", last_error="")
        if job.queue_id:
            self.store.set_queue_status(job.queue_id, "done", result["result"])
        job.status = "done"
        job.message = "签到成功" if result["result"] == "success" else "今日已签到（服务端判定）"
        self.log.info("signin.ok", f"账号 {account['id']} {job.message}")

    def _signin_game(self, job: Job, account: dict, date: str, game_id: str,
                     token: str, uid: str, device_id: str) -> None:
        """异环游戏内每日签到：真正发游戏奖励的路径。"""
        role_id = str(account.get("role_id") or "")
        if not role_id:
            roles = self.adapter.get_game_roles(token, uid, device_id)
            if roles:
                first = roles[0]
                role_id = str(first.get("roleId") or "")
                self.store.update_account(account["id"], role_id=role_id,
                                          role_name=str(first.get("roleName") or ""))
        if not role_id:
            raise AdapterError("api_error", "未取到异环 roleId，请重新授权")
        state = self.adapter.fetch_sign_state(token, uid, device_id, game_id)
        self.log.info("signin.state",
                      f"账号 {account['id']} 游戏签到 todaySign={state['todaySign']} "
                      f"累计={state['days']} 天")
        if state["todaySign"]:
            self.store.record_signin(
                account["id"], date, game_id, "already", code="0",
                msg="服务端今日已签", mode="game", game_id=game_id,
                days=state["days"],
            )
            self.store.update_account(account["id"], status="ok", last_error="")
            if job.queue_id:
                self.store.set_queue_status(job.queue_id, "done", "already")
            job.status = "done"
            job.message = f"今日已在游戏内签到（累计 {state['days']} 天）"
            # 今日已签但可能还没投递（奖励没进邮箱），顺手补一次联动
            delivered = self._ensure_delivery(account, token, uid, device_id, role_id, game_id)
            tail = "· 已投递邮箱" if delivered else "· ⚠ 投递未成功，稍后自动重试"
            job.message += f"（{tail}，约 {int(DELIVER_RETRY_DELAY)} 秒后自动再补一次）"
            self._schedule_redelivery(account["id"])
            self.log.info("signin.ok", f"账号 {account['id']} {job.message}")
            return
        started = time.time()
        result = self.adapter.sign_game(token, uid, device_id, role_id, game_id)
        days_after = state["days"] + (1 if result["result"] == "success" else 0)
        reward = None
        delivered = False
        # "already" 也必须投递：上面那份 state 是刚拉到的快照，但如果有人（游戏内 /
        # 塔吉多 H5）恰好在这几秒里签了，sign_game 会返回「今天已签到」。这种情形
        # 同样需要触发角色联动，否则奖励只写社区侧、永远进不了邮箱。
        if result["result"] in ("success", "already"):
            # 关键一步：触发游戏服侧的角色联动，待发放奖励才会真正投进游戏邮箱
            delivered = self._ensure_delivery(account, token, uid, device_id, role_id, game_id)
            # 保险二：隔 30 秒再自动补一次投递（路径与「补投递」按钮完全一致：
            # 先 GET 签到状态、再 autoBind）。这一次能覆盖「同秒投递太早、
            # 服务端待发放记录还没落成」的窗口，避免用户每天手动点补投递。
            self._schedule_redelivery(account["id"])
        if result["result"] == "success":
            # 优先用官方奖励流水核对“真的到账”；查不到再回落到 30 天奖励表
            reward = self._latest_reward(token, uid, device_id, game_id, started)
            if not reward:
                reward = self._scheduled_reward(token, uid, device_id, game_id,
                                                role_id, state["days"])
        self.store.record_signin(
            account["id"], date, game_id, result["result"],
            code=result.get("code", ""), msg=result.get("msg", ""),
            elapsed_ms=int((time.time() - started) * 1000),
            mode="game", game_id=game_id, days=days_after, reward=reward,
        )
        self.store.update_account(account["id"], status="ok", last_error="")
        if job.queue_id:
            self.store.set_queue_status(job.queue_id, "done", result["result"])
        job.status = "done"
        label = "签到成功" if result["result"] == "success" else "今日已签到（服务端判定）"
        seconds = int(DELIVER_RETRY_DELAY)
        if result["result"] in ("success", "already"):
            tail = (f"· 已投递邮箱，约 {seconds} 秒后自动再补一次" if delivered
                    else f"· ⚠ 投递未成功，约 {seconds} 秒后自动重试")
        else:
            tail = ""
        if reward:
            job.message = (f"{label}，获得 {reward['name']} ×{reward['num']}"
                           f"（累计 {days_after} 天）{tail}")
        else:
            job.message = f"{label}（累计 {days_after} 天）{tail}"
        self.log.info("signin.ok", f"账号 {account['id']} {job.message}")
        # 签到后呗果 / 剩余补签次数 / 累计天数都变了，补一次只读详情（串行队列里排在后面）
        self._submit(Job("signinfo", account["id"], paced=True))

    def _ensure_delivery(self, account: dict, token: str, uid: str, device_id: str,
                         role_id: str, game_id: str, attempts: int = 3) -> bool:
        """触发一次「自动绑定角色」，让游戏服把待发放奖励投进游戏邮箱。

        背景（2026-11-08 实测）：只提交 POST /apihub/awapi/sign 时，reward_records
        有记录、todaySign=true，但游戏内邮箱始终是空的；用户对照两个账号发现
        「用塔吉多 App 登录过一次的号能收到，没登录过的收不到」。补调
        POST /usercenter/api/autoBindGameRole（form: gameId + roleId）后奖励立刻到账。
        该接口是塔吉多 App 登录后自动执行的一步，不改社区绑定表，因此不能靠读
        getGameBindRoles 判断是否已投递。

        2026-11-09：用户反馈「每次签到都还得手动补投递」。该接口本身幂等、不消耗
        任何资源，所以这里失败就原地重试几次（网络抖动 / 服务端瞬时 5xx），
        并在日志里记下返回的 code/data，便于对照服务端到底回了什么。
        投递失败只记 warn，绝不影响签到结果本身（签到已经成功）。
        """
        last_exc: AdapterError | None = None
        for i in range(max(1, attempts)):
            try:
                r = self.adapter.auto_bind_role(token, uid, device_id, role_id, game_id)
                self.log.info("signin.deliver",
                              f"账号 {account['id']} 已触发奖励投递"
                              f"（第 {i + 1} 次，code={r.get('code')}，data={r.get('data')}）")
                self.store.add_audit(account["id"], "deliver", "ok")
                return True
            except AdapterError as exc:
                last_exc = exc
                if i + 1 < attempts:
                    self.log.warn("signin.deliver",
                                  f"账号 {account['id']} 投递第 {i + 1} 次失败，"
                                  f"{exc.kind} {exc.message[:60]}，稍后重试")
                    self._stop.wait(0.6 * (i + 1))
        assert last_exc is not None
        self.log.warn("signin.deliver",
                      f"账号 {account['id']} 投递触发失败：{last_exc.kind} {last_exc.message[:60]}")
        self.store.add_audit(account["id"], "deliver", f"failed: {last_exc.message[:80]}")
        return False

    def _schedule_redelivery(self, account_id: int,
                             delay: float = DELIVER_RETRY_DELAY) -> None:
        """排一次延迟自动补投递（后台任务，文案与手动补投递区分）。

        与手动「补投递」走同一条 `_do_deliver` 代码路径：先 GET 签到状态、再
        autoBindGameRole。差别只是时间点——延后到签到成功后 30 秒，避开
        「社区侧刚写入、游戏服待发放还没落成」的窗口。
        """
        self._submit_later(Job("deliver", account_id, paced=True, auto=True), delay)
        self.log.info("deliver.plan",
                      f"账号 {account_id} 已排入自动补投递（{int(delay)} 秒后）")

    def _latest_reward(self, token: str, uid: str, device_id: str,
                       game_id: str, since: float) -> dict | None:
        """签到后立即查奖励流水，取本次新增的一条；失败不阻断签到结果。"""
        try:
            records = self.adapter.fetch_sign_reward_records(token, uid, device_id, game_id)
        except AdapterError as exc:
            self.log.warn("signin.reward", f"奖励流水查询失败：{exc.kind}")
            return None
        fresh = [r for r in records
                 if r.get("createTime") and r["createTime"] / 1000 >= since - 180]
        if not fresh:
            return None
        r = max(fresh, key=lambda x: x["createTime"])
        return {"name": r["name"], "num": r["num"], "icon": r["icon"], "source": "record"}

    def _scheduled_reward(self, token: str, uid: str, device_id: str, game_id: str,
                          role_id: str, days: int) -> dict | None:
        """流水没查到时，按 H5 的 0 基规则取 rewards[days] 作为应得奖励。"""
        try:
            rewards = self.adapter.fetch_sign_rewards(token, uid, device_id, role_id, game_id)
        except AdapterError as exc:
            self.log.warn("signin.reward", f"奖励表查询失败：{exc.kind}")
            return None
        if 0 <= days < len(rewards):
            r = rewards[days]
            return {"name": r["name"], "num": r["num"], "icon": r["icon"], "source": "schedule"}
        return None

    # ------------------------------------------------------------ 签到详情 / 补签
    def _collect_signinfo(self, account: dict, token: str, uid: str, device_id: str,
                          role_id: str, game_id: str) -> dict:
        """只读汇总签到详情：状态、呗果、补签次数、30 天奖励表、到账流水。

        四个 GET 全部只读；任一子查询失败只丢该块，不整条失败（缺什么显示什么）。
        """
        state = self.adapter.fetch_sign_state(token, uid, device_id, game_id)
        info = self.adapter.resign_info(token, uid, device_id, game_id)
        rewards, records = [], []
        try:
            rewards = self.adapter.fetch_sign_rewards(token, uid, device_id, role_id, game_id)
        except AdapterError as exc:
            self.log.warn("signinfo.rewards", f"账号 {account['id']} 奖励表查询失败：{exc.kind}")
        try:
            records = self.adapter.fetch_sign_reward_records(token, uid, device_id, game_id)
        except AdapterError as exc:
            self.log.warn("signinfo.records", f"账号 {account['id']} 奖励流水查询失败：{exc.kind}")

        days, today = state["days"], state["todaySign"]
        # H5 口径：index < days 为已签；今天的格子 = days-1（今日已签）或 days（今日可签）
        today_index = days - 1 if (today and days > 0) else days
        grid = []
        for i, r in enumerate(rewards):
            name = r["name"] or ""
            grid.append({
                "index": i + 1,
                "name": name,
                "num": r["num"],
                "icon": r["icon"],
                "signed": i < days,
                "today": i == today_index,
                "premium": "环石" in name,
            })
        left = max(0, info["resignLimit"] - info["resignCnt"]) if info["resignLimit"] else 0
        return {
            "gameId": game_id,
            "roleId": role_id,
            "todaySign": today,
            "days": days,
            "day": state["day"],
            "month": state["month"],
            "coin": info["coin"],
            "cost": info["cost"],
            "resignCnt": info["resignCnt"],
            "resignLimit": info["resignLimit"],
            "resignLeft": left,
            "canResign": bool(today and left > 0 and info["coin"] >= info["cost"]),
            "rewards": grid,
            "records": [{"at": int(r["createTime"] / 1000) if r["createTime"] else 0,
                         "name": r["name"], "num": r["num"], "icon": r["icon"]}
                        for r in records],
            "fetchedAt": int(time.time()),
        }

    def _do_signinfo(self, job: Job, account: dict) -> None:
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid = cred.get("uid") or account["uid"]
        device_id = cred.get("deviceId") or account["device_id"]
        game_id = self._signin_key()
        role_id = str(account.get("role_id") or "")
        if not role_id:
            roles = self.adapter.get_game_roles(token, uid, device_id)
            if roles:
                role_id = str(roles[0].get("roleId") or "")
                self.store.update_account(account["id"], role_id=role_id,
                                          role_name=str(roles[0].get("roleName") or ""))
        job.status = "running"
        try:
            data = self._collect_signinfo(account, token, uid, device_id, role_id, game_id)
        except AdapterError as exc:
            self._on_adapter_error(job, account, exc)
            return
        self.store.set_signin_info(account["id"], data)
        self.store.update_account(account["id"], status="ok", last_error="")
        job.status = "done"
        job.message = (f"呗果 {data['coin']} · 本月已签 {data['days']} 天 · "
                       f"补签 {data['resignCnt']}/{data['resignLimit']}")
        self.log.info("signinfo.ok", f"账号 {account['id']} {job.message}")

    def _do_deliver(self, job: Job, account: dict) -> None:
        """手动补投递：已签到但游戏邮箱迟迟没收到奖励时，主动触发一次角色联动。"""
        if not self.store.settings()["writeActionsEnabled"]:
            job.status, job.message = "blocked", "写操作开关关闭，未发送任何请求"
            return
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid = cred.get("uid") or account["uid"]
        device_id = cred.get("deviceId") or account["device_id"]
        game_id = self._signin_key()
        role_id = str(account.get("role_id") or "")
        if not role_id:
            try:
                roles = self.adapter.get_game_roles(token, uid, device_id)
            except AdapterError as exc:
                self._on_adapter_error(job, account, exc)
                return
            if roles:
                role_id = str(roles[0].get("roleId") or "")
                self.store.update_account(account["id"], role_id=role_id)
        if not role_id:
            job.status, job.message = "failed", "缺少 roleId，请先刷新该账号数据"
            return
        job.status = "running"
        try:
            state = self.adapter.fetch_sign_state(token, uid, device_id, game_id)
        except AdapterError as exc:
            self._on_adapter_error(job, account, exc)
            return
        if not state["todaySign"]:
            # 今日未签 ≠ 没有待发放奖励：昨天签了却没投递，今天照样需要补投递。
            # 只要历史上有过成功签到就放行（autoBind 幂等、不消耗资源、不会重复发奖）。
            last = self.store.last_signin_success(account["id"], game_id)
            if not last:
                job.status = "blocked"
                job.message = ("自动补投递跳过：无待投递的签到记录" if job.auto
                               else "该账号没有可投递的签到记录（今日未签，历史也无成功记录）")
                return
            self.log.info("deliver.retry",
                          f"账号 {account['id']} 今日未签，但 {last['date']} 有成功记录，仍允许补投递")
        if self._ensure_delivery(account, token, uid, device_id, role_id, game_id):
            job.status = "done"
            job.message = ("后台自动补投递已完成，请完全退出游戏后重进查看邮箱" if job.auto
                           else "已触发投递，请完全退出游戏后重进查看邮箱")
            self.log.info("deliver.ok", f"账号 {account['id']} {job.message}")
        else:
            job.status = "failed"
            job.message = "投递触发失败，详见 logs"

    def _do_resign(self, job: Job, account: dict) -> None:
        """补签：先核对官方前置条件（今日已签 / 次数 / 呗果），再提交写请求。"""
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid = cred.get("uid") or account["uid"]
        device_id = cred.get("deviceId") or account["device_id"]
        game_id = self._signin_key()
        role_id = str(account.get("role_id") or "")
        if not role_id:
            job.status, job.message = "failed", "缺少 roleId，请先刷新该账号数据"
            return
        job.status = "running"
        try:
            state = self.adapter.fetch_sign_state(token, uid, device_id, game_id)
            if not state["todaySign"]:
                raise AdapterError("precondition", "今日还没签到：官方要求先完成当日签到才能补签")
            info = self.adapter.resign_info(token, uid, device_id, game_id)
            left = max(0, info["resignLimit"] - info["resignCnt"]) if info["resignLimit"] else 0
            if info["resignLimit"] and left <= 0:
                raise AdapterError("precondition",
                                   f"本月补签次数已用完（{info['resignCnt']}/{info['resignLimit']}）")
            if info["coin"] < info["cost"]:
                raise AdapterError("precondition",
                                   f"呗果不足：补签需 {info['cost']}，当前 {info['coin']}")
            started = time.time()
            result = self.adapter.resign(token, uid, device_id, role_id, game_id)
        except AdapterError as exc:
            if exc.kind == "precondition":
                # 前置条件不满足属于「不执行」，不是账号异常，不写 last_error
                job.status, job.message = "blocked", exc.message
                self.log.warn("resign.blocked", f"账号 {account['id']} {exc.message}")
                return
            self.store.add_audit(account["id"], "resign", f"failed: {exc.message[:80]}")
            self._on_adapter_error(job, account, exc)
            return
        # 补签发放的是「当日签到后下一日」的奖励，和签到一样走游戏内邮箱；
        # 也就是说 resign 同样只写社区侧记录，必须再触发一次 autoBindGameRole
        # 才会被推进游戏邮箱（与签到同一个坑，v1.1.1 补上）。
        reward = None
        delivered = False
        if result["result"] == "success":
            delivered = self._ensure_delivery(account, token, uid, device_id, role_id, game_id)
            self._schedule_redelivery(account["id"])
            reward = self._latest_reward(token, uid, device_id, game_id, started)
        self.store.add_audit(account["id"], "resign",
                             f"{result['result']}: cost={info['cost']} left={left - 1}")
        # 立刻回填详情，让看板的呗果 / 剩余次数 / 累计天数当场变化
        try:
            data = self._collect_signinfo(account, token, uid, device_id, role_id, game_id)
            self.store.set_signin_info(account["id"], data)
        except AdapterError as exc:
            self.log.warn("resign.refresh", f"补签后详情刷新失败：{exc.kind}")
        job.status = "done" if result["result"] == "success" else "skipped"
        if result["result"] == "success":
            extra = f"，获得 {reward['name']} ×{reward['num']}" if reward else ""
            tail = ("· 已触发投递，完全退出游戏重进后到邮箱领取" if delivered
                    else "· ⚠ 投递触发失败，稍后会自动重试，也可点「补投递」（不必重复补签）")
            job.message = (f"补签成功{extra}（消耗 {info['cost']} 呗果，"
                           f"本月剩余补签 {max(0, left - 1)} 次）{tail}")
        else:
            job.message = "补签未成功：服务端判定今日无需补签"
        self.log.info("resign.ok" if result["result"] == "success" else "resign.skip",
                      f"账号 {account['id']} {job.message}")

    def _do_gacha(self, job: Job, account: dict) -> None:
        cred = self._credential(account)
        token = cred.get("accessToken") or cred.get("refreshToken")
        uid, device_id = cred.get("uid") or account["uid"], cred.get("deviceId") or account["device_id"]
        role_id = account.get("role_id") or ""
        job.status = "running"
        try:
            if not role_id:
                roles = self.adapter.get_game_roles(token, uid, device_id)
                if roles:
                    r = roles[0]
                    role_id = str(r.get("roleId") or "")
                    self.store.update_account(account["id"], role_id=role_id,
                                              role_name=str(r.get("roleName") or ""))
            if not role_id:
                raise AdapterError("api_error", "该账号未取到异环角色 roleId，请重新授权")
            # 名称字典只是增强显示，失败不阻断抽数抓取
            try:
                from_api = self.adapter.fetch_item_names(token, uid, device_id, role_id)
                added = self.store.upsert_item_names(from_api)
                if added:
                    self.log.info("gacha.names", f"账号 {account['id']} 新增 {added} 个官方物品名称")
            except AdapterError as exc:
                self.log.warn("gacha.names",
                              f"账号 {account['id']} 名称字典获取失败（不影响抽数）：{exc.kind}")
            data = self.adapter.fetch_gacha(token, uid, device_id, role_id)
            normalized = normalize(data, self.store.get_item_names(),
                                   account_id=account["id"])
            self.store.save_gacha_snapshot(account["id"], normalized)
            if normalized.get("roleName"):
                self.store.update_account(account["id"], role_name=normalized["roleName"])
            job.status = "done"
            job.message = (f"{normalized['totalDraw']} 抽 / {normalized['totalRare']} 金，"
                           f"{len(normalized['pools'])} 池 / {normalized['totalDetail']} 条出货明细")
            self.log.info("gacha.ok", f"账号 {account['id']} 抽卡统计抓取成功：{job.message}")
        except AdapterError as exc:
            self._on_gacha_error(job, account, exc)

    def _on_gacha_error(self, job: Job, account: dict, exc: AdapterError) -> None:
        """抽卡是可选只读功能：失败不污染四项看板的账号 last_error。"""
        job.status = "failed"
        job.message = f"[{exc.kind}] {exc.message}"
        if exc.kind == "auth_expired":
            self.store.update_account(account["id"], status="reauth",
                                      last_error="凭证失效，需重新短信登录"
                                                 "（在官方塔吉多 App 登录会顶掉这里的凭证，两者不能同时在线）")
            self.store.add_audit(account["id"], "auth_expired", "reauth",
                                 account.get("token_version", 0))
        self.log.error("gacha.failed", f"账号 {account['id']} 抽卡抓取失败：{job.message}")

    def _on_adapter_error(self, job: Job, account: dict, exc: AdapterError) -> None:
        job.status = "failed"
        job.message = f"[{exc.kind}] {exc.message}"
        if exc.kind == "auth_expired":
            self.store.update_account(account["id"], status="reauth",
                                      last_error="凭证失效，需重新短信登录"
                                                 "（在官方塔吉多 App 登录会顶掉这里的凭证，两者不能同时在线）")
            self.store.add_audit(account["id"], "auth_expired", "reauth",
                                 account.get("token_version", 0))
        elif exc.kind == "protocol_changed":
            self.store.update_account(account["id"], last_error="疑似协议改版，请更新适配器配置")
            self.store.add_audit(account["id"], "protocol_changed", "warn")
        else:
            self.store.update_account(account["id"], last_error=exc.message[:200])
        self.log.error("job.failed", f"账号 {account['id']} {job.kind} 失败：{job.message}")

    # ------------------------------------------------------------ 定时调度
    def refresh_if_week_reset(self) -> int:
        """跨过周重置点（周一 05:00）后，强制刷一次过期快照。

        都市活力在 05:00 归满，但服务端懒回填；如果看板还捧着周日的快照，会先
        显示「667/700」这种旧值，最长一个自动刷新周期（默认 30 分钟）才纠正。
        这里在跨过重置点后的第一个调度周期立刻补刷，让「5 点满」当场成立。
        """
        reset = week_reset_ts()
        if reset <= self._week_reset_seen:
            return 0
        self._week_reset_seen = reset
        stale = []
        for a in self.store.list_accounts():
            if not a["enabled"] or a["status"] == "disabled":
                continue
            ts = (a.get("snapshot") or {}).get("ts") or 0
            if ts < reset:
                stale.append(a["id"])
        for aid in stale[:max(1, int(self.store.settings()["batchSize"]))]:
            self._submit(Job("refresh", aid))
        if stale:
            self.log.info("weekly.refresh",
                          f"已跨过周重置点，刷新 {len(stale)} 个账号的过期快照")
        return len(stale)

    def _schedule(self) -> None:
        while not self._stop.wait(60):
            s = self.store.settings()
            if not s["automationEnabled"]:
                continue
            now = time.time()
            # 周重置（周一 05:00）：先把跨过重置点的过期快照刷掉，别拿旧值糊弄
            try:
                self.refresh_if_week_reset()
            except Exception as exc:
                self.log.warn("weekly.refresh", f"周重置刷新失败：{type(exc).__name__}")
            # 只读刷新：错峰、分批
            if now - self._last_auto_refresh >= int(s["refreshIntervalMin"]) * 60:
                self._last_auto_refresh = now
                self.refresh_all()
            # 自动签到：必须同时开启写操作开关，且在时间窗内
            date = time.strftime("%Y-%m-%d")
            hhmm = time.strftime("%H:%M")
            if (s["writeActionsEnabled"] and self._last_auto_signin_date != date
                    and str(s["signinWindowStart"]) <= hhmm <= str(s["signinWindowEnd"])):
                try:
                    result = self.signin_all()  # 内部按当日幂等过滤，已签账号不会入队
                    self._last_auto_signin_date = date
                    self.log.info("automation.signin",
                                  f"自动签到入队 {result['queued']} 个账号")
                except WriteDisabled:
                    pass

    # ------------------------------------------------------------ 前端状态
    def state(self) -> dict:
        date = time.strftime("%Y-%m-%d")
        accounts = self.store.list_accounts(date, signin_key=self._signin_key())
        settings = self.store.settings()
        # 签到详情（呗果 / 补签次数 / 本月奖励表）只读缓存，随 state 一起下发，前端不必额外请求
        infos = self.store.get_signin_info()
        reset = week_reset_ts()
        weekly_seen = self.store.weekly_delivered_accounts()
        for a in accounts:
            entry = infos.get(a["id"])
            a["signin_info"] = (entry or {}).get("data") or None
            a["signin_info_at"] = (entry or {}).get("ts") or 0
            if a.get("snapshot"):
                # 快照若是本周重置点之前抓的，周刷新资源直接按满展示（不落库）
                self._week_view(a["snapshot"], reset)
                # 周本次数：官方字段不是每次都在。实测（11-09 11:31 用户截图）官方 App
                # 能显示 2/3，而同刻我们的 roleHome 里没有这个字段、只有一个字符之差都
                # 不像「0 次」——高度疑似「存档字段，退出游戏后才同步」。所以只提示
                # 「待同步」，绝不替官方推断数字，也不说「未打完」。
                snap = a["snapshot"]
                if not snap.get("weekly_available") and a["id"] in weekly_seen:
                    snap["weekly_state"] = "pending_sync"
        summary = self._summary(accounts, settings)
        with self._jobs_guard:
            jobs = [j.public() for j in sorted(
                self._jobs.values(), key=lambda x: x.job_id, reverse=True)[:20]]
        weekly = config.weekly_config(self.adapter.cfg)
        weekly_visible = bool(settings["weeklyCopiesVisible"] and weekly.get("enabled", False))
        metric_specs = [
            {"key": m["key"], "label": m["label"], "hint": ""}
            for m in self.adapter.cfg["metrics"]
        ]
        if weekly_visible:
            metric_specs.append({
                "key": "weeklyCopies",
                "label": weekly.get("label", "周本次数"),
                "hint": weekly.get("semantics", ""),
            })
        return {
            "date": date,
            "accounts": accounts,
            "summary": summary,
            "settings": settings,
            "metrics": metric_specs,
            "weekly": {
                "visible": weekly_visible,
                "decision": weekly.get("decision", "not_configured"),
                "field": weekly.get("field", ""),
                "denominator": weekly.get("denominator", 3),
                "semantics": weekly.get("semantics", ""),
                "missingLabel": weekly.get("missingLabel", "—"),
                "missingBadge": weekly.get("missingBadge", "未同步"),
                "missingHint": weekly.get("missingHint", ""),
                "note": "第四项已启用：官方下发 weekcopiesremainCnt 时显示真实剩余次数 x/3 并标来源；未下发时显示「—」，绝不套用官方 H5 的 3/3 兜底常量。分母 3 是前端常量，0 表示本周已打完。不计入提醒，可在设置里关闭显示。",
            },
            "queue": {
                "size": self._q.qsize(),
                "running": self._running.public() if self._running else None,
                "persisted": self.store.queue_summary(date),
                "jobs": jobs,
            },
            "adapter": {
                "version": self.adapter.cfg["_version"],
                "loadedAt": self.adapter.cfg["_loadedAt"],
                "authProtocol": self.adapter.cfg["authProtocol"],
                "credentialMode": self.adapter.cfg["credentialMode"],
                "available": config.list_adapter_versions(),
            },
            "logs": self.log.recent(60),
            "audits": self.store.audits(20),
            # 轻量版本戳：前端 3 秒轮询只比对 version，变了才拉完整抽卡数据
            "gacha": {"version": self.store.gacha_version()},
        }

    def gacha_payload(self) -> dict:
        """完整抽卡看板数据：最新快照 + 历史 + 名称字典 + 口径说明。"""
        names = self.store.get_item_names()
        latest = {r["account_id"]: r for r in self.store.all_latest_gacha()}
        accounts = []
        for a in self.store.list_accounts():
            snap = latest.get(a["id"])
            history = self.store.gacha_history(a["id"], 8)
            entry = {
                "accountId": a["id"],
                "remark": a["remark"],
                "phoneMask": a["phone_mask"],
                "roleName": a["role_name"] or (snap or {}).get("role_name", ""),
                "enabled": bool(a["enabled"]),
                "hasData": False,
                "fetchedAt": snap["ts"] if snap else 0,
                "history": history,
                "diff": {},
                "data": None,
            }
            if snap:
                try:
                    data = json.loads(snap.get("payload_json") or "{}")
                except (json.JSONDecodeError, TypeError):
                    data = {}
                if data.get("pools") is not None:
                    # 别名是即时生效的：只覆盖显示名，绝不改动任何抽数 / 时间
                    for pool in data.get("pools", []):
                        for row in list(pool.get("items", [])) + list(pool.get("details", [])):
                            n, src = name_of(str(row.get("itemId") or ""), names)
                            row["name"], row["nameSource"] = n, src
                    entry["data"] = data
                    entry["hasData"] = True
                    entry["diff"] = summary_diff(
                        {"totalDraw": snap["total_draw"], "totalRare": snap["total_rare"]},
                        history[1] if len(history) > 1 else None,
                    )
            accounts.append(entry)
        return {
            "version": self.store.gacha_version(),
            "endpoint": self.adapter.ep.get("gacha", ""),
            "names": {k: v["name"] for k, v in names.items()},
            "accounts": accounts,
            "notes": {
                "readonly": "只读 GET：不签到、不刷新凭证、不修改任何官方数据；与四项数据共用同一串行限速队列。",
                "draws": "明细 rareCount = 本次出货距上次出货所用抽数（拿到该角色花了多少抽）；累计抽数 = 按出货时间对抽数做前缀和。",
                "time": "出货时间取官方 timeStamp（毫秒）；time 字段实测恒为抓取当天，未使用。",
                "coverage": "官方记录疑有滚动保留期：若明细抽数之和小于卡池累计抽数，说明早期明细已不可见，累计抽数只覆盖现存明细。",
                "names": "中文名来自官方 characters 字典接口；未覆盖的物品只显示 ID，可点击名称自行设置别名（别名只存本地，优先级高于官方名称）。",
                "luckyType": "luckyType 为官方原始枚举值，含义未定案，原样展示不做解读。",
            },
        }

    def set_item_name(self, item_id: str, name: str) -> dict:
        result = self.store.set_item_name(item_id, name)
        self.log.info("gacha.alias", f"物品 {result['itemId']} 别名已更新（{result['source']}）")
        return result

    def _summary(self, accounts: list[dict], settings: dict) -> dict:
        today_ok = sum(1 for a in accounts if (a.get("signin_today") or {}).get("result")
                       in ("success", "already"))
        stamina_full = 0
        stale = 0
        now = time.time()
        for a in accounts:
            s = a.get("snapshot")
            if s and s.get("stamina_max") and s.get("stamina_value") is not None \
                    and s["stamina_value"] >= s["stamina_max"]:
                stamina_full += 1
            if not a.get("last_refresh_at") or \
                    now - a["last_refresh_at"] > int(settings["refreshIntervalMin"]) * 60:
                stale += 1
        return {
            "total": len(accounts),
            "enabled": sum(1 for a in accounts if a["enabled"]),
            "reauth": sum(1 for a in accounts if a["status"] == "reauth"),
            "error": sum(1 for a in accounts if a["status"] == "error"),
            "signedToday": today_ok,
            "unsignedToday": len(accounts) - today_ok,
            "staminaFull": stamina_full,
            "staleData": stale,
            # 约定：周本次数只作展示，不参与任何提醒计数（即使已启用）
        }
