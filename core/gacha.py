"""抽卡统计规范化：官方 gacha 响应 → 看板数据（纯函数、零依赖）。

口径（2026-11-07 双账号实测，见 nte-capture/抽卡统计_抓取报告.md）：
- 卡池层 drawCount = 该池累计抽数；实测恰好等于该池所有明细 rareCount 之和。
- 明细层 rareCount = **本次出货距上次出货所用抽数**，即「拿到这个角色花了多少抽」。
  与卡池层同名字段语义完全不同，不要混用。
- 累计抽数 cumulative = 按 timeStamp 升序对 rareCount 做前缀和，
  表示「该池到这次出货为止一共花了多少抽」。
- 官方明细 time 字段恒为抓取当天（疑似数据生成日），**不可当出货日期**；
  出货时间一律用 timeStamp（毫秒）。
- charid 只有物品 ID；中文名来自官方 characters 字典接口或用户自定义别名，
  不猜、不编造，缺名字就显示 ID 并标「官方未下发名称」。
- 官方抽卡记录疑有约 180 天滚动期（外部资料，未实测）：若早期明细被清理，
  明细之和会小于 drawCount。此时 coverage < 1，累计抽数只覆盖现存明细，
  界面必须明确提示，绝不把「可见明细之和」伪装成「开服至今累计」。
"""
from __future__ import annotations

import datetime
import time

_ROLE_MARKERS = {"elementType", "quality", "alev", "awakenLev", "citySkills"}


def as_int(value, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_float(value, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ts_millis(value) -> int:
    """官方 timeStamp 为毫秒；兼容秒级输入，非法值返回 0。"""
    n = as_int(value, 0)
    if n <= 0:
        return 0
    return n * 1000 if n < 10 ** 12 else n


def ts_text(ms: int) -> str:
    if not ms:
        return "时间未知"
    try:
        return datetime.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d %H:%M")
    except (OSError, OverflowError, ValueError):
        return "时间未知"


def extract_item_names(data) -> dict[str, str]:
    """从官方 /apihub/awapi/yh/characters（或 roleHome.characters）递归提取 id→name。

    只收两类 ID：纯数字（角色，且必须带角色特征键，避免误收技能/装备 ID）、
    fork_ 开头（弧盘）。返回的名称一律来自官方响应，不做任何推测。
    """
    out: dict[str, str] = {}

    def walk(node) -> None:
        if isinstance(node, dict):
            i, n = node.get("id"), node.get("name")
            if (isinstance(i, str) and isinstance(n, str) and n.strip()
                    and (i.startswith("fork_") or (i.isdigit() and _ROLE_MARKERS & set(node)))):
                out.setdefault(i, n.strip())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data)
    return out


def name_of(item_id: str, names: dict) -> tuple[str, str]:
    entry = (names or {}).get(item_id)
    if isinstance(entry, dict) and entry.get("name"):
        return str(entry["name"]), str(entry.get("source") or "api")
    if isinstance(entry, str) and entry:
        return entry, "api"
    return "", "none"


def normalize_pool(pool: dict, names: dict) -> dict:
    """把单个卡池（gachaDetails 元素）映射成看板结构。"""
    tab = str(pool.get("tab") or "未知卡池")
    draw_count = as_int(pool.get("drawCount"))
    rare_count = as_int(pool.get("rareCount"))
    pity_max = as_int(pool.get("m")) or 0
    average = as_float(pool.get("average"), 0.0) or (
        round(draw_count / rare_count, 1) if rare_count else 0.0
    )

    raw_details = [d for d in (pool.get("details") or []) if isinstance(d, dict)]
    # 先按 timeStamp 升序（相同时间戳保持官方原始顺序）算前缀和，再倒序输出
    ordered = sorted(enumerate(raw_details), key=lambda pair: (ts_millis(pair[1].get("timeStamp")) or 10 ** 18, pair[0]))
    running = 0
    details: list[dict] = []
    for _idx, d in ordered:
        draws = as_int(d.get("rareCount"))
        running += draws
        ms = ts_millis(d.get("timeStamp"))
        item_id = str(d.get("charid") or "")
        name, name_source = name_of(item_id, names)
        details.append({
            "itemId": item_id,
            "name": name,
            "nameSource": name_source,
            "draws": draws,                 # 获取该角色本次花了多少抽
            "cumulative": running,          # 到这次出货为止该池累计花了多少抽
            "ts": ms,
            "time": ts_text(ms),
            "luckyType": d.get("luckyType"),
            "pityMax": pity_max,
            "pityPct": round(min(100.0, draws / pity_max * 100), 1) if pity_max else None,
            "nearPity": bool(pity_max and draws >= pity_max * 0.8),
        })
    details.reverse()  # 最新出货在前

    # 按物品汇总：同一角色/弧盘多次出货合计花了多少抽
    grouped: dict[str, dict] = {}
    for d in details:
        g = grouped.setdefault(d["itemId"], {
            "itemId": d["itemId"], "name": d["name"], "nameSource": d["nameSource"],
            "times": 0, "totalDraws": 0, "maxDraws": 0, "minDraws": None,
            "lastTs": 0, "lastTime": "", "firstTs": 0, "firstTime": "",
        })
        g["times"] += 1
        g["totalDraws"] += d["draws"]
        g["maxDraws"] = max(g["maxDraws"], d["draws"])
        g["minDraws"] = d["draws"] if g["minDraws"] is None else min(g["minDraws"], d["draws"])
        if d["ts"] >= g["lastTs"]:
            g["lastTs"], g["lastTime"] = d["ts"], d["time"]
        if not g["firstTs"] or (d["ts"] and d["ts"] <= g["firstTs"]):
            g["firstTs"], g["firstTime"] = d["ts"], d["time"]
    items = sorted(grouped.values(), key=lambda x: (-x["totalDraws"], -x["times"], x["itemId"]))
    for g in items:
        g["avgDraws"] = round(g["totalDraws"] / g["times"], 1) if g["times"] else 0.0
        g["minDraws"] = g["minDraws"] if g["minDraws"] is not None else 0

    draw_sum = sum(d["draws"] for d in details)
    return {
        "tab": tab,
        "drawCount": draw_count,
        "rareCount": rare_count,
        "average": average,
        "pityMax": pity_max,
        "playerOver": str(pool.get("playerOver") or ""),
        "detailCount": len(details),
        "drawSum": draw_sum,
        "drawConsistent": draw_sum == draw_count,
        "coverage": round(draw_sum / draw_count, 3) if draw_count else 1.0,
        "items": items,
        "details": details,
    }


def normalize(data: dict, names: dict, *, account_id: int,
              fetched_at: int | None = None) -> dict:
    """把一次官方 gacha.data 规范化成看板完整结构。"""
    pools = [normalize_pool(p, names) for p in (data.get("gachaDetails") or [])
             if isinstance(p, dict)]
    return {
        "accountId": account_id,
        "fetchedAt": fetched_at or int(time.time()),
        "roleId": str(data.get("roleid") or ""),
        "roleName": str(data.get("rolename") or ""),
        "level": as_int(data.get("lev")),
        "luckTitle": str(data.get("luckTitle") or ""),
        "luckType": data.get("luckType"),
        "totalDraw": sum(p["drawCount"] for p in pools),
        "totalRare": sum(p["rareCount"] for p in pools),
        "totalDetail": sum(p["detailCount"] for p in pools),
        "pools": pools,
    }


def summary_diff(current: dict, previous: dict | None) -> dict:
    """与上一次快照对比（只比较官方聚合值，明细变化不猜）。"""
    if not previous:
        return {"hasPrevious": False}
    return {
        "hasPrevious": True,
        "drawDelta": current["totalDraw"] - as_int(previous.get("total_draw")),
        "rareDelta": current["totalRare"] - as_int(previous.get("total_rare")),
        "previousAt": as_int(previous.get("ts")),
    }
