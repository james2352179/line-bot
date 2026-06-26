#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
路線淹水雷達 — 核心查詢模組
資料源：民生公共物聯網 SensorThings API（水利署＋縣市政府合建淹水感測器）
  base: https://sta.colife.org.tw/STA_WaterResource_v2/v1.0

用法（CLI 測試）：
  ./venv/bin/python3.12 flood_api.py 玉井 南化 左鎮
"""
from __future__ import annotations
import sys
import datetime as dt
from typing import List, Dict, Any

import requests

BASE = "https://sta.colife.org.tw/STA_WaterResource_v2/v1.0"
TZ8 = dt.timezone(dt.timedelta(hours=8))  # 台灣時間，Railway/UTC 環境也安全

# 淹水深度紅綠燈分級（以「開車」安全為準，單位 cm）
#   0       綠  正常
#   1–15    黃  注意（路面積水，慢行）
#   16–30   橘  警戒（轎車涉水風險，建議改道）
#   >30     紅  危險（勿行，車輛易拋錨／受困）
LEVELS = [
    (30, "red", "🔴 危險", "勿行，車輛易拋錨受困"),
    (15, "orange", "🟠 警戒", "轎車涉水風險，建議改道"),
    (0,  "yellow", "🟡 注意", "路面積水，減速慢行"),
    (-1, "green", "🟢 正常", "目前無積水"),
]
# 觀測值超過這個分鐘數視為「可能過時／測站未回報」
STALE_MIN = 30


def classify(depth_cm: float) -> Dict[str, str]:
    for thr, color, label, advice in LEVELS:
        if depth_cm > thr:
            return {"color": color, "label": label, "advice": advice}
    return {"color": "green", "label": "🟢 正常", "advice": "目前無積水"}


def _parse_time(iso: str) -> dt.datetime | None:
    if not iso:
        return None
    try:
        s = iso.replace("Z", "+00:00")
        return dt.datetime.fromisoformat(s).astimezone(TZ8)
    except Exception:
        return None


def query(townships: List[str], timeout: int = 25) -> Dict[str, Any]:
    """
    依鄉鎮（站名子字串）查淹水感測器即時值。
    回傳 {"ok":bool, "stations":[...], "error":str, "queried_at":datetime}
    每站：name, depth_cm(float|None), time(datetime|None), stale(bool),
          cctv(str|None), lat, lon, level(dict)
    """
    towns = [t.strip() for t in townships if t.strip()]
    if not towns:
        return {"ok": False, "error": "未輸入鄉鎮", "stations": [], "queried_at": dt.datetime.now(TZ8)}

    flt = " or ".join(f"substringof('{t}',properties/stationName)" for t in towns)
    ok, stations, err = _request_stations(flt, timeout)
    if not ok:
        return {"ok": False, "error": err, "stations": [], "queried_at": dt.datetime.now(TZ8)}
    # 覆蓋率檢查：哪些鄉鎮在此資料源「查無淹水感測站」
    # （重要安全提醒：查無站 ≠ 沒淹，可能根本沒裝感測器，需改看 CCTV／封路）
    missing = [t for t in towns if not any(t in s["name"] for s in stations)]
    return {"ok": True, "error": "", "stations": stations,
            "missing": missing, "queried_at": dt.datetime.now(TZ8)}


def _request_stations(odata_filter: str, timeout: int = 25):
    """以 OData $filter 取淹水感測站並解析。回傳 (ok, stations, error)。"""
    params = {
        "$filter": odata_filter,
        "$top": "400",
        "$expand": (
            "Locations($select=location),"
            "Datastreams($select=unitOfMeasurement;"
            "$expand=ObservedProperty($select=name),"
            "Observations($orderby=phenomenonTime desc;$top=1;$select=result,phenomenonTime))"
        ),
    }
    try:
        r = requests.get(f"{BASE}/Things", params=params, timeout=timeout,
                         headers={"User-Agent": "Mozilla/5.0 FloodRadar"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return False, [], f"連線失敗：{e}"

    now = dt.datetime.now(TZ8)
    stations: List[Dict[str, Any]] = []
    for thing in data.get("value", []):
        p = thing.get("properties", {})
        name = p.get("stationName") or thing.get("name", "未命名")

        lat = lon = None
        locs = thing.get("Locations", [])
        if locs:
            coords = (locs[0].get("location", {}) or {}).get("coordinates")
            if coords and len(coords) == 2:
                lon, lat = coords[0], coords[1]

        depth = otime = cctv = None
        for ds in thing.get("Datastreams", []):
            opname = (ds.get("ObservedProperty", {}) or {}).get("name", "")
            obs = ds.get("Observations", [])
            if not obs:
                continue
            o = obs[0]
            if "淹水深度" in opname:
                try:
                    depth = float(o.get("result"))
                except (TypeError, ValueError):
                    depth = None
                otime = _parse_time(o.get("phenomenonTime"))
            elif "影格" in opname or "視訊" in opname or "影像" in opname:
                val = o.get("result")
                if isinstance(val, str) and val.startswith("http"):
                    cctv = val

        if depth is None:  # 排除純水位/河川站
            continue
        stale = bool(otime and (now - otime).total_seconds() > STALE_MIN * 60)
        stations.append({
            "name": name, "depth_cm": depth, "time": otime, "stale": stale,
            "cctv": cctv, "lat": lat, "lon": lon, "level": classify(depth),
        })

    stations.sort(key=lambda s: s["depth_cm"], reverse=True)
    return True, stations, ""


def query_tainan_all(timeout: int = 30) -> Dict[str, Any]:
    """取全台南淹水感測站（給路線沿線比對用）。"""
    ok, stations, err = _request_stations(
        "properties/authority eq '臺南市政府水利局'", timeout)
    return {"ok": ok, "error": err, "stations": stations,
            "queried_at": dt.datetime.now(TZ8)}


def haversine(lat1, lon1, lat2, lon2) -> float:
    """兩點球面距離（公里）。"""
    import math
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def filter_near_route(stations: List[Dict[str, Any]],
                      route_pts: List[tuple], radius_km: float = 0.8):
    """保留距離路線任一點 <= radius_km 的感測站，附上 dist_km。"""
    out = []
    for s in stations:
        if s.get("lat") is None or s.get("lon") is None:
            continue
        dmin = min((haversine(s["lat"], s["lon"], la, lo) for la, lo in route_pts),
                   default=9e9)
        if dmin <= radius_km:
            s = dict(s)
            s["dist_km"] = dmin
            out.append(s)
    out.sort(key=lambda s: (-s["depth_cm"], s["dist_km"]))
    return out


def overall_verdict(stations: List[Dict[str, Any]]) -> Dict[str, str]:
    """整條路線的總結紅綠燈：取最嚴重的一站。"""
    if not stations:
        return {"color": "gray", "label": "查無測站", "advice": "此區無淹水感測器，請改看 CCTV／封路資訊"}
    worst = max(stations, key=lambda s: s["depth_cm"])
    v = dict(worst["level"])
    v["station"] = worst["name"]
    v["depth"] = worst["depth_cm"]
    return v


if __name__ == "__main__":
    towns = sys.argv[1:] or ["玉井", "南化", "左鎮"]
    res = query(towns)
    print(f"\n查詢鄉鎮：{' '.join(towns)}    時間：{res['queried_at']:%Y-%m-%d %H:%M:%S} (台灣)")
    if not res["ok"]:
        print("❌", res["error"]); sys.exit(1)
    sts = res["stations"]
    v = overall_verdict(sts)
    print(f"總結：{v['label']}  ({v.get('station','')} {v.get('depth','')}cm) — {v['advice']}\n")
    if res.get("missing"):
        print(f"⚠️ 查無淹水感測站的鄉鎮：{ '、'.join(res['missing']) }（此區需改看 CCTV／封路，勿當作安全）\n")
    if not sts:
        print("（此區查無淹水感測站）")
    for s in sts:
        t = f"{s['time']:%H:%M}" if s["time"] else "—"
        warn = " ⚠️資料可能過時" if s["stale"] else ""
        cctv = "  📷有即時影像" if s["cctv"] else ""
        print(f"  {s['level']['label']}  {s['depth_cm']:>4.0f}cm  {s['name']}  @{t}{warn}{cctv}")
    print()
