"""SQLite 存储：账号、快照、签到日志、持久化队列、审计、设置。

设计要点（对应开发计划 4.3 / 4.4）：
- 幂等唯一键 (account_id, date, community_id)，「今日已签」也算成功；
- 队列状态持久化，进程重启可断点续签；
- 账号表只存掩码手机号与 credential_ref，票据不入库；
- 每次调用独立连接，WAL 模式，线程安全。
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from . import config

_ACCOUNT_FIELDS = {
    "remark", "group_tag", "phone_mask", "role_id", "role_name", "server_id",
    "device_id", "credential_ref", "credential_type", "uid", "adapter_version",
    "enabled", "status", "obtained_at", "last_refresh_at", "last_signin_at",
    "last_error", "token_version",
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    remark TEXT NOT NULL DEFAULT '',
    group_tag TEXT NOT NULL DEFAULT '',
    phone_mask TEXT NOT NULL DEFAULT '',
    role_id TEXT NOT NULL DEFAULT '',
    role_name TEXT NOT NULL DEFAULT '',
    server_id TEXT NOT NULL DEFAULT '',
    device_id TEXT NOT NULL DEFAULT '',
    credential_ref TEXT NOT NULL UNIQUE,
    credential_type TEXT NOT NULL DEFAULT 'accessToken',
    token_version INTEGER NOT NULL DEFAULT 0,
    uid TEXT NOT NULL DEFAULT '',
    adapter_version TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL DEFAULT 'ok',
    obtained_at INTEGER,
    last_refresh_at INTEGER,
    last_signin_at INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    stamina_value REAL, stamina_max REAL, stamina_max_source TEXT,
    city_value REAL, city_max REAL, city_max_source TEXT,
    activity_value REAL, activity_max REAL, activity_max_source TEXT,
    weekly_value REAL, weekly_max REAL, weekly_max_source TEXT,
    weekly_value_source TEXT, weekly_available INTEGER NOT NULL DEFAULT 0,
    fingerprint TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_snapshot_account ON daily_snapshot(account_id, id DESC);
CREATE TABLE IF NOT EXISTS signin_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    community_id TEXT NOT NULL,
    result TEXT NOT NULL,
    code TEXT NOT NULL DEFAULT '',
    msg TEXT NOT NULL DEFAULT '',
    attempts INTEGER NOT NULL DEFAULT 1,
    elapsed_ms INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL,
    mode TEXT NOT NULL DEFAULT 'community',
    game_id TEXT NOT NULL DEFAULT '',
    days INTEGER NOT NULL DEFAULT 0,
    reward_json TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, date, community_id)
);
CREATE TABLE IF NOT EXISTS signin_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    date TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    planned_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    UNIQUE(account_id, date)
);
CREATE TABLE IF NOT EXISTS token_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER,
    ts INTEGER NOT NULL,
    event TEXT NOT NULL,
    result TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS run_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    action TEXT NOT NULL,
    message TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gacha_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    date TEXT NOT NULL,
    total_draw INTEGER NOT NULL DEFAULT 0,
    total_rare INTEGER NOT NULL DEFAULT 0,
    role_name TEXT NOT NULL DEFAULT '',
    level INTEGER NOT NULL DEFAULT 0,
    luck_title TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    ok INTEGER NOT NULL DEFAULT 1,
    FOREIGN KEY(account_id) REFERENCES accounts(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_gacha_account ON gacha_snapshot(account_id, id DESC);
CREATE TABLE IF NOT EXISTS item_names (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'api',
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS item_aliases (
    item_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS signin_info (
    account_id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}'
);
"""

# 轻量迁移：CREATE TABLE IF NOT EXISTS 不会给已有表补列，旧数据目录需显式 ALTER。
_SNAPSHOT_COLUMNS = {
    "weekly_value": "REAL",
    "weekly_max": "REAL",
    "weekly_max_source": "TEXT",
    "weekly_value_source": "TEXT",
    "weekly_available": "INTEGER NOT NULL DEFAULT 0",
    # 都市活力「周重置未回填」时按上限显示：1 表示这一条是估算值，不是接口真值。
    # 查询历史时必须排除这类行，否则会把估算出来的满值当成玩家真实余额。
    "city_assumed": "INTEGER NOT NULL DEFAULT 0",
}
_GACHA_COLUMNS = {
    "payload_json": "TEXT NOT NULL DEFAULT '{}'",
}
_SIGNIN_COLUMNS = {
    "mode": "TEXT NOT NULL DEFAULT 'community'",
    "game_id": "TEXT NOT NULL DEFAULT ''",
    "days": "INTEGER NOT NULL DEFAULT 0",
    "reward_json": "TEXT NOT NULL DEFAULT ''",
}


class Store:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or config.DB_PATH
        config.ensure_data_dir()
        self.init()

    @contextmanager
    def _con(self):
        """独立连接 + 事务 + 关闭（sqlite3 的 with 只提交事务，不关闭连接）。"""
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    def init(self) -> None:
        with self._con() as con:
            con.executescript(SCHEMA)
            have = {r["name"] for r in con.execute("PRAGMA table_info(daily_snapshot)")}
            for name, decl in _SNAPSHOT_COLUMNS.items():
                if name not in have:
                    con.execute(f"ALTER TABLE daily_snapshot ADD COLUMN {name} {decl}")
            have_g = {r["name"] for r in con.execute("PRAGMA table_info(gacha_snapshot)")}
            for name, decl in _GACHA_COLUMNS.items():
                if name not in have_g:
                    con.execute(f"ALTER TABLE gacha_snapshot ADD COLUMN {name} {decl}")
            have_s = {r["name"] for r in con.execute("PRAGMA table_info(signin_log)")}
            for name, decl in _SIGNIN_COLUMNS.items():
                if name not in have_s:
                    con.execute(f"ALTER TABLE signin_log ADD COLUMN {name} {decl}")

    # ------------------------------------------------------------ 设置
    def settings(self) -> dict:
        out = dict(config.DEFAULT_SETTINGS)
        with self._con() as con:
            rows = con.execute("SELECT key, value FROM settings").fetchall()
        for r in rows:
            try:
                out[r["key"]] = json.loads(r["value"])
            except json.JSONDecodeError:
                out[r["key"]] = r["value"]
        return out

    def set_setting(self, key: str, value) -> None:
        with self._con() as con:
            con.execute(
                "INSERT INTO settings(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value, ensure_ascii=False)),
            )

    def set_settings(self, values: dict) -> dict:
        allowed = set(config.DEFAULT_SETTINGS)
        for k, v in values.items():
            if k in allowed:
                self.set_setting(k, v)
        return self.settings()

    # ------------------------------------------------------------ 账号
    def create_account(self, **fields) -> int:
        data = {k: v for k, v in fields.items() if k in _ACCOUNT_FIELDS}
        data.setdefault("credential_type", "accessToken")
        data.setdefault("status", "ok")
        data.setdefault("enabled", 1)
        data["created_at"] = int(time.time())
        cols = ", ".join(data)
        marks = ", ".join("?" for _ in data)
        with self._con() as con:
            cur = con.execute(
                f"INSERT INTO accounts({cols}) VALUES({marks})", tuple(data.values())
            )
            return int(cur.lastrowid)

    def update_account(self, account_id: int, **fields) -> None:
        data = {k: v for k, v in fields.items() if k in _ACCOUNT_FIELDS}
        if not data:
            return
        assignments = ", ".join(f"{k}=?" for k in data)
        with self._con() as con:
            con.execute(
                f"UPDATE accounts SET {assignments} WHERE id=?",
                (*data.values(), account_id),
            )

    def sync_token_version(self, account_id: int, expected: int, new_version: int) -> bool:
        """与 vault 的 CAS 配套：旧版本不允许覆盖新版本。"""
        with self._con() as con:
            cur = con.execute(
                "UPDATE accounts SET token_version=? WHERE id=? AND token_version=?",
                (new_version, account_id, expected),
            )
            return cur.rowcount > 0

    def get_account(self, account_id: int) -> dict | None:
        with self._con() as con:
            row = con.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return dict(row) if row else None

    def find_by_uid(self, uid: str) -> dict | None:
        with self._con() as con:
            row = con.execute("SELECT * FROM accounts WHERE uid=?", (str(uid),)).fetchone()
        return dict(row) if row else None

    def delete_account(self, account_id: int) -> None:
        with self._con() as con:
            con.execute("DELETE FROM accounts WHERE id=?", (account_id,))

    def list_accounts(self, today: str | None = None,
                      signin_key: str | None = None) -> list[dict]:
        today = today or time.strftime("%Y-%m-%d")
        with self._con() as con:
            accounts = [dict(r) for r in con.execute("SELECT * FROM accounts ORDER BY id")]
            snaps = {
                r["account_id"]: dict(r) for r in con.execute(
                    "SELECT * FROM daily_snapshot WHERE id IN "
                    "(SELECT MAX(id) FROM daily_snapshot GROUP BY account_id)"
                )
            }
            if signin_key:
                # 只取当前签到模式对应的记录：旧社区记录（community_id=1）不会
                # 被误显示成「今日已签」，切到 game/1289 后当天会重新入队。
                signs = {
                    r["account_id"]: dict(r) for r in con.execute(
                        "SELECT * FROM signin_log WHERE date=? AND community_id=?",
                        (today, signin_key)
                    )
                }
            else:
                signs = {
                    r["account_id"]: dict(r) for r in con.execute(
                        "SELECT * FROM signin_log WHERE date=?", (today,)
                    )
                }
        for a in accounts:
            a.pop("credential_ref", None)  # 引用本身也不外泄
            a["snapshot"] = snaps.get(a["id"])
            a["signin_today"] = signs.get(a["id"])
            a["has_credential"] = bool(a.get("token_version"))
        return accounts

    # ------------------------------------------------------------ 快照
    def save_snapshot(self, account_id: int, metrics: dict, fingerprint: str,
                      ok: bool = True) -> None:
        cols = {
            "account_id": account_id,
            "ts": int(time.time()),
            "fingerprint": fingerprint,
            "ok": 1 if ok else 0,
        }
        prefixes = {
            "stamina": "stamina", "cityStamina": "city",
            "activity": "activity", "weeklyCopies": "weekly",
        }
        for key, prefix in prefixes.items():
            m = metrics.get(key) or {}
            cols[f"{prefix}_value"] = m.get("value")
            cols[f"{prefix}_max"] = m.get("max")
            cols[f"{prefix}_max_source"] = m.get("maxSource")
            if prefix == "city":
                cols["city_assumed"] = 1 if m.get("assumedFull") else 0
            if prefix == "weekly":
                cols["weekly_value_source"] = m.get("valueSource")
                cols["weekly_available"] = 1 if m.get("available") else 0
        names = ", ".join(cols)
        marks = ", ".join("?" for _ in cols)
        with self._con() as con:
            con.execute(f"INSERT INTO daily_snapshot({names}) VALUES({marks})", tuple(cols.values()))
            con.execute("UPDATE accounts SET last_refresh_at=?, last_error='' WHERE id=?",
                        (int(time.time()), account_id))
            # 快照保留 30 天
            con.execute("DELETE FROM daily_snapshot WHERE ts < ?",
                        (int(time.time()) - 30 * 86400,))

    def latest_snapshot(self, account_id: int) -> dict | None:
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM daily_snapshot WHERE account_id=? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def max_real_city_value_since(self, account_id: int, since_ts: float) -> float | None:
        """本周重置后读到过的最大「真实」都市活力（排除按上限估算的那些行）。

        用途：接口返回 0 时区分两种情形——周重置后还没被服务端回填（历史无真值，
        应按满显示）vs 玩家真的花光了（本周期内读到过 >0 的真值，0 就是真的 0）。
        """
        with self._con() as con:
            row = con.execute(
                "SELECT MAX(city_value) AS v FROM daily_snapshot "
                "WHERE account_id=? AND ts>=? AND city_assumed=0",
                (account_id, int(since_ts)),
            ).fetchone()
        if not row or row["v"] is None:
            return None
        return float(row["v"])

    def weekly_delivered_accounts(self) -> set[int]:
        """曾经收到过 weekcopiesremainCnt 的账号集合（= 官方会给这个号下发周本次数）。

        用途：官方只在「本周已打完（剩 0）」时下发该字段，其余时候整键消失。对曾经
        收到过 0 的账号，字段消失就可以确定是「本周还有次数」，可以显示「未打完」，
        比一个「—」有意义。从没收到过的账号（如账号 1）不能这么推断，仍显示「—」。
        """
        with self._con() as con:
            rows = con.execute(
                "SELECT DISTINCT account_id FROM daily_snapshot WHERE weekly_available=1"
            ).fetchall()
        return {int(r["account_id"]) for r in rows}

    def last_city_before(self, account_id: int, before_ts: float) -> dict | None:
        """重置点之前最后一条**真实**（非按上限估算）快照里的都市活力。

        用途：识别「跨过周一 05:00 后数值一字未变」——那是服务端在回显上周的旧值
        （上周没花完就会回显成 256 这种残留值），不是本周真实余量。
        必须排除估算行，否则上周最后一条是「按满估算」的 700，本周回显 256 时
        就对不上了。
        """
        with self._con() as con:
            row = con.execute(
                "SELECT city_value, city_assumed, ts FROM daily_snapshot "
                "WHERE account_id=? AND ts<? AND city_assumed=0 ORDER BY id DESC LIMIT 1",
                (account_id, int(before_ts)),
            ).fetchone()
        return dict(row) if row else None

    def city_values_before(self, account_id: int, before_ts: float,
                           days: float = 2) -> set[float]:
        """重置点之前 `days` 天里读到过的**真实**都市活力值。

        用途：识别「上周残留回显」——服务端在重置点后原样返回上周最后一个数。
        窗口刻意收窄到 2 天：回显的数一定是**跨点前最后那个数**，而活跃玩家 1~2 天内
        必然刷过；窗口一放宽就会误伤——每周一都花同样多的话（比如每次花 54 余 646），
        本周第一次读到的 646 会被当成「上周也见过」，反被改回满。
        """
        with self._con() as con:
            rows = con.execute(
                "SELECT DISTINCT city_value FROM daily_snapshot "
                "WHERE account_id=? AND ts<? AND ts>=? AND city_assumed=0 "
                "AND city_value IS NOT NULL",
                (account_id, int(before_ts), int(before_ts - days * 86400)),
            ).fetchall()
        return {float(r["city_value"]) for r in rows}

    # ------------------------------------------------------------ 签到幂等 / 队列
    def signin_result_today(self, account_id: int, date: str, community_id: str) -> dict | None:
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM signin_log WHERE account_id=? AND date=? AND community_id=?",
                (account_id, date, community_id),
            ).fetchone()
        return dict(row) if row else None

    def is_signed_today(self, account_id: int, date: str, community_id: str) -> bool:
        row = self.signin_result_today(account_id, date, community_id)
        return bool(row and row["result"] in ("success", "already"))

    def last_signin_success(self, account_id: int, community_id: str) -> dict | None:
        """最近一次「成功 / 已签」记录（不限日期）。

        用途：判断账号是否还有历史待发放奖励。例如昨天签了但没投递、今天还没签时，
        手动补投递不应被「今日未签」一刀切拦掉。
        """
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM signin_log WHERE account_id=? AND community_id=? "
                "AND result IN ('success','already') ORDER BY date DESC, id DESC LIMIT 1",
                (account_id, community_id),
            ).fetchone()
        return dict(row) if row else None

    def record_signin(self, account_id: int, date: str, community_id: str,
                      result: str, code: str = "", msg: str = "",
                      attempts: int = 1, elapsed_ms: int = 0,
                      mode: str = "community", game_id: str = "",
                      days: int = 0, reward: dict | None = None) -> bool:
        """幂等落库：成功/已签记录不可被覆盖；失败记录允许被后续成功补写。

        这样「当天先失败再重试成功」不会因唯一键被永久卡在 failed，
        而「当天已成功」也绝不会被第二次提交改写。

        community_id 列同时承载游戏签到的 gameId（值不同，互不冲突），
        mode 用于区分 community / game；reward 存本次实际到账奖励。
        """
        reward_json = json.dumps(reward, ensure_ascii=False) if reward else ""
        with self._con() as con:
            cur = con.execute(
                "INSERT INTO signin_log"
                "(account_id,date,community_id,result,code,msg,attempts,elapsed_ms,"
                "created_at,mode,game_id,days,reward_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(account_id,date,community_id) DO UPDATE SET "
                "result=excluded.result, code=excluded.code, msg=excluded.msg, "
                "attempts=signin_log.attempts+1, elapsed_ms=excluded.elapsed_ms, "
                "created_at=excluded.created_at, mode=excluded.mode, "
                "game_id=excluded.game_id, days=excluded.days, "
                "reward_json=excluded.reward_json "
                "WHERE signin_log.result='failed' AND excluded.result IN ('success','already')",
                (account_id, date, community_id, result, str(code), msg[:200],
                 attempts, elapsed_ms, int(time.time()), mode, game_id, days, reward_json),
            )
            if cur.rowcount:
                con.execute("UPDATE accounts SET last_signin_at=? WHERE id=?",
                            (int(time.time()), account_id))
            return cur.rowcount > 0

    def enqueue_signin(self, date: str, account_ids: list[int]) -> int:
        now = int(time.time())
        count = 0
        with self._con() as con:
            for aid in account_ids:
                cur = con.execute(
                    "INSERT OR IGNORE INTO signin_queue(account_id,date,status,planned_at) "
                    "VALUES(?,?,'pending',?)", (aid, date, now)
                )
                count += cur.rowcount
        return count

    def pending_signin(self, date: str) -> list[dict]:
        with self._con() as con:
            rows = con.execute(
                "SELECT q.*, a.remark, a.enabled, a.status AS account_status "
                "FROM signin_queue q JOIN accounts a ON a.id=q.account_id "
                "WHERE q.date=? AND q.status IN ('pending','running') AND q.attempts < ? "
                "ORDER BY q.id",
                (date, 3),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_queue_status(self, queue_id: int, status: str, error: str = "") -> None:
        with self._con() as con:
            con.execute(
                "UPDATE signin_queue SET status=?, attempts=attempts+1, last_error=? WHERE id=?",
                (status, error[:200], queue_id),
            )

    def recover_queue(self) -> int:
        """进程中断后把 running 重置为 pending，实现断点续签。"""
        with self._con() as con:
            cur = con.execute("UPDATE signin_queue SET status='pending' WHERE status='running'")
            return cur.rowcount

    def queue_summary(self, date: str) -> dict:
        with self._con() as con:
            rows = con.execute(
                "SELECT status, COUNT(*) AS n FROM signin_queue WHERE date=? GROUP BY status",
                (date,),
            ).fetchall()
        return {r["status"]: r["n"] for r in rows}

    # ------------------------------------------------------------ 审计 / 日志
    def add_audit(self, account_id: int | None, event: str, result: str,
                  version: int = 0) -> None:
        with self._con() as con:
            con.execute(
                "INSERT INTO token_audit(account_id,ts,event,result,version) VALUES(?,?,?,?,?)",
                (account_id, int(time.time()), event, result, version),
            )

    def audits(self, limit: int = 100) -> list[dict]:
        with self._con() as con:
            rows = con.execute(
                "SELECT * FROM token_audit ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------ 抽卡统计快照
    def save_gacha_snapshot(self, account_id: int, normalized: dict) -> int:
        ts = int(normalized.get("fetchedAt") or time.time())
        with self._con() as con:
            cur = con.execute(
                "INSERT INTO gacha_snapshot"
                "(account_id,ts,date,total_draw,total_rare,role_name,level,luck_title,payload_json,ok) "
                "VALUES(?,?,?,?,?,?,?,?,?,1)",
                (account_id, ts,
                 time.strftime("%Y-%m-%d", time.localtime(ts)), normalized.get("totalDraw", 0),
                 normalized.get("totalRare", 0), normalized.get("roleName", ""),
                 normalized.get("level", 0), normalized.get("luckTitle", ""),
                 json.dumps(normalized, ensure_ascii=False)),
            )
            # 快照保留 90 天，避免无限增长
            con.execute("DELETE FROM gacha_snapshot WHERE ts < ?",
                        (int(time.time()) - 90 * 86400,))
            return int(cur.lastrowid)

    def latest_gacha(self, account_id: int) -> dict | None:
        with self._con() as con:
            row = con.execute(
                "SELECT * FROM gacha_snapshot WHERE account_id=? ORDER BY id DESC LIMIT 1",
                (account_id,),
            ).fetchone()
        return dict(row) if row else None

    def all_latest_gacha(self) -> list[dict]:
        with self._con() as con:
            rows = con.execute(
                "SELECT * FROM gacha_snapshot WHERE id IN "
                "(SELECT MAX(id) FROM gacha_snapshot GROUP BY account_id)"
            ).fetchall()
        return [dict(r) for r in rows]

    def gacha_history(self, account_id: int, limit: int = 8) -> list[dict]:
        with self._con() as con:
            rows = con.execute(
                "SELECT id,ts,date,total_draw,total_rare,role_name,level,luck_title,ok "
                "FROM gacha_snapshot WHERE account_id=? ORDER BY id DESC LIMIT ?",
                (account_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def gacha_version(self) -> int:
        """轻量版本戳：前端轮询 /api/state 时据此判断是否需要重拉完整抽卡数据。"""
        with self._con() as con:
            row = con.execute("SELECT COALESCE(MAX(id),0) AS v FROM gacha_snapshot").fetchone()
        return int(row["v"])

    # ------------------------------------------------------------ 物品名称字典
    def get_item_names(self) -> dict[str, dict]:
        """官方字典 + 用户别名合并；别名单独存表，优先级更高且不会破坏官方名。"""
        with self._con() as con:
            rows = con.execute("SELECT item_id,name,source,updated_at FROM item_names").fetchall()
            aliases = con.execute("SELECT item_id,name,updated_at FROM item_aliases").fetchall()
        out = {r["item_id"]: {"name": r["name"], "source": r["source"],
                              "updatedAt": r["updated_at"]} for r in rows}
        for r in aliases:
            if r["name"]:
                out[r["item_id"]] = {"name": r["name"], "source": "user",
                                     "updatedAt": r["updated_at"]}
        return out

    def upsert_item_names(self, mapping: dict[str, str], source: str = "api") -> int:
        """官方字典自动补名；只更新未变化时跳过，用户别名永远在另一张表里。"""
        if not mapping:
            return 0
        now = int(time.time())
        count = 0
        with self._con() as con:
            for item_id, name in mapping.items():
                if not item_id or not name:
                    continue
                cur = con.execute(
                    "INSERT INTO item_names(item_id,name,source,updated_at) VALUES(?,?,?,?) "
                    "ON CONFLICT(item_id) DO UPDATE SET "
                    "name=excluded.name, source=excluded.source, updated_at=excluded.updated_at "
                    "WHERE item_names.name != excluded.name",
                    (str(item_id), str(name)[:60], source, now),
                )
                count += cur.rowcount
        return count

    def set_item_name(self, item_id: str, name: str) -> dict:
        """用户自定义别名；传空字符串表示清除别名，立即回落到官方名称或纯 ID。"""
        item_id = str(item_id or "")[:80]
        name = str(name or "").strip()[:40]
        with self._con() as con:
            if name:
                con.execute(
                    "INSERT INTO item_aliases(item_id,name,updated_at) VALUES(?,?,?) "
                    "ON CONFLICT(item_id) DO UPDATE SET "
                    "name=excluded.name, updated_at=excluded.updated_at",
                    (item_id, name, int(time.time())),
                )
            else:
                con.execute("DELETE FROM item_aliases WHERE item_id=?", (item_id,))
        entry = self.get_item_names().get(item_id)
        return {"itemId": item_id, "name": (entry or {}).get("name", ""),
                "source": (entry or {}).get("source", "none")}

    # ------------------------------------------------------------ 签到详情快照
    def set_signin_info(self, account_id: int, payload: dict) -> None:
        """只读缓存：呗果余额、补签次数、30 天奖励表、最近到账流水。

        这些数据只影响展示，不参与任何幂等或写操作判断；官方改口时随时可被覆盖。
        """
        with self._con() as con:
            con.execute(
                "INSERT INTO signin_info(account_id,ts,payload_json) VALUES(?,?,?) "
                "ON CONFLICT(account_id) DO UPDATE SET "
                "ts=excluded.ts, payload_json=excluded.payload_json",
                (int(account_id), int(time.time()),
                 json.dumps(payload, ensure_ascii=False)),
            )

    def get_signin_info(self, account_id: int | None = None) -> dict:
        """返回 {account_id: {ts, data}}；account_id 给定时只取该账号。"""
        with self._con() as con:
            if account_id is None:
                rows = con.execute("SELECT account_id,ts,payload_json FROM signin_info").fetchall()
            else:
                rows = con.execute(
                    "SELECT account_id,ts,payload_json FROM signin_info WHERE account_id=?",
                    (int(account_id),)).fetchall()
        out = {}
        for r in rows:
            try:
                data = json.loads(r["payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                data = {}
            out[int(r["account_id"])] = {"ts": int(r["ts"]), "data": data}
        return out
