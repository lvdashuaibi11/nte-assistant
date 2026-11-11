"""《异环》第三方助手 · 本地 Web 服务入口（S2）。

零 pip 依赖：只用 Python 标准库（http.server / sqlite3 / urllib / ctypes）。
仅绑定 127.0.0.1，校验 Host，任何写操作默认关闭。

启动：
    python app.py
    # 或双击 启动.bat
环境变量：
    NTE_PORT        端口，默认 8765
    NTE_DATA_DIR    数据目录（测试用），默认 ./data
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from core import config
from core.adapter import AdapterError
from core.auth import AuthError
from core.db import Store
from core.logutil import Logger, scrub_text
from core.service import Service, WriteDisabled
from core.vault import Vault

ROOT = Path(__file__).resolve().parent
PORT = int(os.environ.get("NTE_PORT", "8765"))
STATIC = {"index.html": "text/html; charset=utf-8",
          "app.js": "application/javascript; charset=utf-8",
          "style.css": "text/css; charset=utf-8"}

store = Store()
logger = Logger()
vault = Vault()
service = Service(store, vault, logger)


class ApiError(Exception):
    def __init__(self, status: int, kind: str, message: str):
        super().__init__(message)
        self.status, self.kind, self.message = status, kind, message


class Handler(BaseHTTPRequestHandler):
    server_version = "NTEAssistant/0.7"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt, *args):
        # 不把请求行写进日志（可能带 query 参数），只保留方法
        pass

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(",")[0].strip()
        return host in {f"127.0.0.1:{PORT}", f"localhost:{PORT}",
                        "127.0.0.1", "localhost"}

    def _send(self, status: int, payload, content_type: str = "application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else json.dumps(
            payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > 65536:
            raise ApiError(413, "too_large", "请求体过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ApiError(400, "bad_json", "请求不是合法 JSON") from exc

    def _ok(self, data=None):
        self._send(200, {"ok": True, "data": data or {}})

    # ------------------------------------------------------------ GET
    def do_GET(self):
        if not self._host_ok():
            self._send(403, {"ok": False, "error": {"kind": "host", "message": "仅允许本机访问"}})
            return
        path = self.path.split("?")[0]
        try:
            if path == "/api/state":
                self._ok(service.state())
                return
            if path == "/api/health":
                self._ok({"port": PORT, "adapter": service.adapter.cfg["_version"]})
                return
            if path == "/api/gacha":
                self._ok(service.gacha_payload())
                return
            if path == "/" or path == "/index.html":
                self._send(200, (config.WEB_DIR / "index.html").read_bytes(),
                           STATIC["index.html"])
                return
            name = path.lstrip("/")
            if name in STATIC and name != "index.html":
                self._send(200, (config.WEB_DIR / name).read_bytes(), STATIC[name])
                return
            self._send(404, {"ok": False, "error": {"kind": "not_found", "message": "不存在"}})
        except Exception as exc:
            self._error(exc)

    # ------------------------------------------------------------ POST
    def do_POST(self):
        if not self._host_ok():
            self._send(403, {"ok": False, "error": {"kind": "host", "message": "仅允许本机访问"}})
            return
        path = self.path.split("?")[0]
        try:
            body = self._body()
            result = self._route(path, body)
            self._ok(result)
        except ApiError as exc:
            self._send(exc.status, {"ok": False,
                                    "error": {"kind": exc.kind, "message": exc.message}})
        except WriteDisabled as exc:
            logger.warn("api.blocked", str(exc))
            self._send(403, {"ok": False,
                             "error": {"kind": "write_disabled", "message": str(exc)}})
        except AuthError as exc:
            logger.warn("api.auth", str(exc))
            self._send(400, {"ok": False,
                             "error": {"kind": "auth_error", "message": str(exc)}})
        except AdapterError as exc:
            logger.warn("api.adapter", f"{exc.kind}: {exc.message}")
            self._send(502, {"ok": False, "error": exc.as_dict()})
        except Exception as exc:
            self._error(exc)

    def _route(self, path: str, b: dict):
        if path == "/api/auth/start":
            # force=True 表示「我要重发一条新验证码」，否则同号未过期就直接续用旧会话
            return service.auth.start(str(b.get("phone") or ""),
                                      force=bool(b.get("force")))
        if path == "/api/auth/pending":
            return {"pending": service.auth.pending()}
        if path == "/api/auth/complete":
            return service.auth.complete(
                str(b.get("sessionId") or ""), str(b.get("code") or ""),
                skip_check=bool(b.get("skipCheck")),
                remark=str(b.get("remark") or "")[:30],
            )
        if path == "/api/auth/import":
            default = ROOT.parent / "nte-capture" / "TOKEN.txt"
            target = str(b.get("path") or default)
            return service.auth.import_token_file(target)
        if path == "/api/refresh":
            return {"job": service.refresh_one(int(b.get("accountId")), paced=False).public()}
        if path == "/api/refresh-all":
            return service.refresh_all()
        if path == "/api/signin":
            return {"job": service.signin_one(int(b.get("accountId")), paced=False).public()}
        if path == "/api/signin-all":
            return service.signin_all()
        if path == "/api/signinfo":
            return {"job": service.signinfo_one(int(b.get("accountId"))).public()}
        if path == "/api/signinfo-all":
            return service.signinfo_all()
        if path == "/api/resign":
            return {"job": service.resign_one(int(b.get("accountId"))).public()}
        if path == "/api/deliver":
            return {"job": service.deliver_one(int(b.get("accountId"))).public()}
        if path == "/api/gacha/refresh":
            return {"job": service.gacha_one(int(b.get("accountId")), paced=False).public()}
        if path == "/api/gacha/refresh-all":
            return service.gacha_all()
        if path == "/api/gacha/alias":
            return service.set_item_name(str(b.get("itemId") or ""), str(b.get("name") or ""))
        if path == "/api/settings":
            return service.update_settings(dict(b.get("values") or {}))
        if path == "/api/account/update":
            aid = int(b.get("accountId"))
            fields = {k: v for k, v in (b.get("fields") or {}).items()
                      if k in {"remark", "group_tag", "enabled"}}
            if "enabled" in fields:
                fields["enabled"] = 1 if fields["enabled"] else 0
            store.update_account(aid, **fields)
            return {"updated": True}
        if path == "/api/account/delete":
            service.delete_account(int(b.get("accountId")))
            return {"deleted": True}
        raise ApiError(404, "not_found", "未知接口")

    def _error(self, exc: Exception):
        msg = scrub_text(f"{type(exc).__name__}: {exc}")
        logger.error("api.exception", msg)
        self._send(500, {"ok": False,
                         "error": {"kind": "internal", "message": msg}})


def main() -> None:
    config.ensure_data_dir()
    service.start()
    logger.info("app.start",
                f"本地服务启动 127.0.0.1:{PORT}，适配器 {service.adapter.cfg['_version']}，"
                f"写操作={'已开启' if store.settings()['writeActionsEnabled'] else '关闭'}")
    print("=" * 62)
    print("  《异环》第三方助手 · 本地 Web 版 (S2)")
    print(f"  浏览器打开: http://127.0.0.1:{PORT}/")
    print(f"  数据目录:   {config.DATA_DIR}")
    print("  安全默认:   写操作 / 自动化 均为关闭；今日已签不会重复提交")
    print("  按 Ctrl+C 退出")
    print("=" * 62)

    def open_browser():
        time.sleep(1.0)
        try:
            webbrowser.open(f"http://127.0.0.1:{PORT}/")
        except Exception:
            pass

    if os.environ.get("NTE_NO_BROWSER") != "1":
        threading.Thread(target=open_browser, daemon=True).start()
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n正在退出…")
    finally:
        service.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
