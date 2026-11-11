"""配置与版本化适配器加载。

适配器 JSON 是「协议事实」的唯一来源：端点、签名常量、字段映射、错误关键词、
限速参数都从这里读。文件改动后按 mtime 自动热加载，官方改版不需要改 Python 代码。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ADAPTER_DIR = ROOT / "adapters"
WEB_DIR = ROOT / "web"
DEFAULT_ADAPTER_VERSION = "1.2.8"

# 测试 / 多实例时可用环境变量覆盖，避免污染正式数据
DATA_DIR = Path(os.environ.get("NTE_DATA_DIR", str(ROOT / "data"))).resolve()
DB_PATH = DATA_DIR / "assistant.db"
VAULT_PATH = DATA_DIR / "vault.json"
LOG_PATH = DATA_DIR / "run.log"

DEFAULT_SETTINGS = {
    # 安全默认值：写操作与自动化都关闭。用户当日已签到，绝不自动提交写请求。
    "writeActionsEnabled": False,
    "automationEnabled": False,
    "refreshIntervalMin": 20,
    "batchSize": 5,
    "minIntervalSec": 5,
    "maxIntervalSec": 10,
    "signinWindowStart": "08:00",
    "signinWindowEnd": "23:00",
    "adapterVersion": DEFAULT_ADAPTER_VERSION,
    # 2026-11-07 04:26 用户启用第四项；仍不计入任何提醒逻辑，可在设置里随时关掉
    "weeklyCopiesVisible": True,
    "hideHealthyAccounts": False,
}

_lock = threading.RLock()
_cache: dict[str, tuple[float, dict]] = {}


def ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def list_adapter_versions() -> list[str]:
    return sorted(p.stem for p in ADAPTER_DIR.glob("*.json"))


def load_adapter(version: str | None = None) -> dict:
    """按版本加载适配器配置；mtime 变化自动重新读取（热更新）。"""
    version = version or DEFAULT_ADAPTER_VERSION
    path = ADAPTER_DIR / f"{version}.json"
    if not path.exists():
        raise FileNotFoundError(f"适配器配置不存在: {path}")
    mtime = path.stat().st_mtime
    with _lock:
        cached = _cache.get(version)
        if cached and cached[0] == mtime:
            return cached[1]
        with path.open("r", encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["_version"] = version
        cfg["_loadedAt"] = int(time.time())
        _cache[version] = (mtime, cfg)
        return cfg


def metric_config(cfg: dict, key: str) -> dict | None:
    for m in cfg.get("metrics", []):
        if m.get("key") == key:
            return m
    return None


def weekly_config(cfg: dict) -> dict:
    """周本次数配置。兼容旧版适配器的 hiddenMetrics 键名（B 决策时期）。"""
    items = cfg.get("optionalMetrics") or cfg.get("hiddenMetrics") or []
    for m in items:
        if m.get("key") == "weeklyCopies":
            return m
    return {"enabled": False, "decision": "not_configured"}
