"""脱敏日志：任何手机号、验证码、票据都不得进入日志或接口返回。"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from pathlib import Path

from . import config

SENSITIVE_SUB = (
    "token", "authorization", "captcha", "password", "passwd", "cellphone",
    "secret", "cookie", "session", "credential", "useridentif", "idnumber",
    "realname", "idcard", "encryptedid", "contactinfo", "phone",
)
SENSITIVE_EXACT = {
    "sign", "ds", "signature", "deviceid", "nonce", "appid", "idfa", "mac",
    "username", "nickname", "headimg", "showcellphone", "showemail",
    "setthirdcellphone", "pi", "bindrelation", "usergames", "email",
    "sex", "age", "adult", "isactive", "securescore", "source",
}

TEXT_PATTERNS = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "1**********"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "******************"),
    (re.compile(r"(?i)\b(accessToken|refreshToken|authorization|token|captcha|sign|ds)\b\s*[:=]\s*[\"']?[A-Za-z0-9_\-.=/+]{6,}"), r"\1=***"),
    (re.compile(r"(?i)(验证码|短信码|phoneCode|captchaCode)\s*[:=是为]?\s*[\"']?\d{4,8}"), r"\1=***"),
    (re.compile(r"(?<![\w])[A-Za-z0-9_\-]{40,}={0,2}(?![\w])"), "***LONG_TOKEN***"),
)


def scrub_text(text: str) -> str:
    if not text:
        return ""
    out = str(text)
    for pattern, repl in TEXT_PATTERNS:
        out = pattern.sub(repl, out)
    return out[:800]


def scrub_obj(obj, depth: int = 0):
    """按键名打码；空值/null 保留原样（避免把「无 refreshToken」看成有票据）。"""
    if depth > 10:
        return "..."
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in SENSITIVE_EXACT or any(s in kl for s in SENSITIVE_SUB):
                out[str(k)] = "***" if v not in (None, "") else v
            else:
                out[str(k)] = scrub_obj(v, depth + 1)
        return out
    if isinstance(obj, list):
        items = [scrub_obj(x, depth + 1) for x in obj[:8]]
        if len(obj) > 8:
            items.append(f"...({len(obj) - 8} more)")
        return items
    if isinstance(obj, str):
        return scrub_text(obj)
    return obj


def phone_mask(phone: str) -> str:
    p = re.sub(r"\D", "", phone or "")
    if len(p) < 7:
        return "***"
    return f"{p[:3]}****{p[-4:]}"


class Logger:
    """写 SQLite run_log + 本地文本文件，两份都脱敏。"""

    def __init__(self, db_path: Path | None = None, log_path: Path | None = None):
        self.db_path = db_path or config.DB_PATH
        self.log_path = log_path or config.LOG_PATH
        self._lock = threading.Lock()
        config.ensure_data_dir()
        con = sqlite3.connect(self.db_path)
        try:
            con.execute(
                """CREATE TABLE IF NOT EXISTS run_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    action TEXT NOT NULL,
                    message TEXT NOT NULL
                )"""
            )
            con.commit()
        finally:
            con.close()

    def log(self, level: str, action: str, message) -> None:
        if not isinstance(message, str):
            message = json.dumps(scrub_obj(message), ensure_ascii=False)
        msg = scrub_text(message)
        ts = int(time.time())
        with self._lock:
            con = None
            try:
                con = sqlite3.connect(self.db_path, timeout=10)
                con.execute(
                    "INSERT INTO run_log(ts, level, action, message) VALUES(?,?,?,?)",
                    (ts, level, action, msg),
                )
                con.execute(
                    "DELETE FROM run_log WHERE id NOT IN "
                    "(SELECT id FROM run_log ORDER BY id DESC LIMIT 500)"
                )
                con.commit()
            except sqlite3.Error:
                pass
            finally:
                if con is not None:
                    con.close()
            try:
                with self.log_path.open("a", encoding="utf-8") as f:
                    f.write(f"{ts} [{level}] {action}: {msg}\n")
            except OSError:
                pass

    def info(self, action: str, message) -> None:
        self.log("INFO", action, message)

    def warn(self, action: str, message) -> None:
        self.log("WARN", action, message)

    def error(self, action: str, message) -> None:
        self.log("ERROR", action, message)

    def recent(self, limit: int = 100) -> list[dict]:
        con = sqlite3.connect(self.db_path, timeout=10)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute(
                "SELECT ts, level, action, message FROM run_log ORDER BY id DESC LIMIT ?",
                (min(max(limit, 1), 500),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            con.close()
