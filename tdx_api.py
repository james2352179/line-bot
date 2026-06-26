#!/usr/bin/env python3.12
# -*- coding: utf-8 -*-
"""
TDX 即時路況事件（封路/管制/坍方）查詢模組
資料源：TDX 運輸資料流通服務 Road/Traffic/Live/News/Highway（國道+省道，全國）
事件無座標，靠「路名比對」對到路線經過的道路（台20線/國道8號…）。
NewsCategory：1=封閉/管制、99=天氣警示、4=施工。
"""
from __future__ import annotations
import re
import time
import requests

TOKEN_URL = "https://tdx.transportdata.tw/auth/realms/TDXConnect/protocol/openid-connect/token"
NEWS_HIGHWAY = "https://tdx.transportdata.tw/api/basic/v2/Road/Traffic/Live/News/Highway?$format=JSON"

_token = {"val": None, "exp": 0}
_news = {"data": None, "exp": 0}

# 封路/管制關鍵字（這些一律當高優先示警）
_CLOSE_KW = ["封閉", "封橋", "封路", "中斷", "坍方", "土石", "不開放通行",
             "雙向封閉", "預警性封閉", "交通管制", "道路封", "便道", "搶修"]
_WEATHER_KW = ["大雨", "豪雨", "降雨", "濃霧", "積水", "請降低車速", "小心駕駛"]

# 路線道路擷取（國道N號 / 台N線 / 台N乙線 / 縣道N / 市道N / 快速道路）
_ROAD_RE = re.compile(r'國道\d+號|[台臺]\d+[甲乙丙]?線|縣道\d+|市道\d+')
# 縣市擷取（用來判斷事件是否在路線所經縣市，過濾同路名但遠在他縣的事件）
_COUNTY_RE = re.compile(
    r'(台北|臺北|新北|桃園|台中|臺中|台南|臺南|高雄|基隆|新竹|苗栗|彰化|'
    r'南投|雲林|嘉義|屏東|宜蘭|花蓮|台東|臺東|澎湖|金門|連江)[市縣]')


def _norm(s: str) -> str:
    return (s or "").replace("臺", "台")


def extract_route_roads(instructions) -> list:
    """從 Directions 步驟指示文字擷取編號道路（去重）。"""
    roads = set()
    for ins in instructions:
        for m in _ROAD_RE.findall(_norm(ins)):
            roads.add(m)
    return sorted(roads)


def get_token(cid: str, csecret: str) -> str:
    if _token["val"] and time.time() < _token["exp"]:
        return _token["val"]
    r = requests.post(TOKEN_URL, timeout=20,
                      headers={"content-type": "application/x-www-form-urlencoded"},
                      data={"grant_type": "client_credentials",
                            "client_id": cid, "client_secret": csecret})
    r.raise_for_status()
    j = r.json()
    _token["val"] = j["access_token"]
    _token["exp"] = time.time() + j.get("expires_in", 86400) - 120
    return _token["val"]


def _fetch_highway_news(token: str) -> list:
    if _news["data"] is not None and time.time() < _news["exp"]:
        return _news["data"]
    r = requests.get(NEWS_HIGHWAY, timeout=30,
                     headers={"authorization": "Bearer " + token})
    r.raise_for_status()
    data = r.json().get("Newses", [])
    _news["data"] = data
    _news["exp"] = time.time() + 60  # 與來源更新頻率一致，避免限流
    return data


def route_alerts(cid: str, csecret: str, route_roads: list,
                 region_hints=None, allowed_counties=None) -> dict:
    """
    回傳沿線（依路名比對）的封路/天氣事件。
    allowed_counties：路線所經縣市（如 ['台南']）；事件標明他縣且非路線縣市→off_route。
    {"ok":bool, "closed":[on-route], "closed_far":[他縣], "weather":[...], "error":str}
    每筆：{title, category, time, road, priority, off_route}
    """
    region_hints = region_hints or []
    allowed = set(_norm(c) for c in (allowed_counties or []))
    if not cid or not csecret:
        return {"ok": False, "error": "未設定 TDX 金鑰", "closed": [], "closed_far": [], "weather": []}
    if not route_roads:
        return {"ok": True, "error": "", "closed": [], "closed_far": [], "weather": []}
    try:
        token = get_token(cid, csecret)
        news = _fetch_highway_news(token)
    except Exception as e:
        return {"ok": False, "error": f"TDX 連線失敗：{str(e)[:60]}",
                "closed": [], "closed_far": [], "weather": []}

    roads_norm = [_norm(r) for r in route_roads]
    hints_norm = [_norm(h) for h in region_hints]
    closed, closed_far, weather = [], [], []
    for n in news:
        text = _norm(n.get("Title", "") + n.get("Description", ""))
        road = next((r for r in roads_norm if r in text), None)
        if not road:
            continue
        cat = n.get("NewsCategory")
        counties = set(_COUNTY_RE.findall(text))
        priority = any(h in text for h in hints_norm)
        # 事件標了縣市、且都不在路線縣市、又無地名命中 → 視為他縣（可能不在路線）
        off_route = bool(allowed) and bool(counties) and not (counties & allowed) and not priority
        item = {
            "title": n.get("Title", "").strip(), "category": cat,
            "time": n.get("PublishTime", ""), "road": road,
            "priority": priority, "off_route": off_route,
        }
        if cat == 1 or any(k in text for k in _CLOSE_KW):
            (closed_far if off_route else closed).append(item)
        elif (cat == 99 or any(k in text for k in _WEATHER_KW)) and not off_route:
            weather.append(item)
    closed.sort(key=lambda x: (not x["priority"],))
    weather.sort(key=lambda x: (not x["priority"],))
    return {"ok": True, "error": "", "closed": closed,
            "closed_far": closed_far, "weather": weather}


if __name__ == "__main__":
    import sys
    cid, csecret = sys.argv[1], sys.argv[2]
    roads = sys.argv[3:] or ["台20線", "國道8號", "國道1號"]
    res = route_alerts(cid, csecret, roads,
                       region_hints=["台南", "玉井", "南化", "左鎮"],
                       allowed_counties=["台南"])
    print("ok:", res["ok"], res.get("error", ""))
    print(f"\n🚧 沿線封閉/管制：{len(res['closed'])} 筆")
    for c in res["closed"]:
        star = "⭐" if c["priority"] else "  "
        print(f"  {star}[類{c['category']}] {c['title'][:70]}")
    print(f"\n🟠 他縣同路名(可能不在路線)：{len(res['closed_far'])} 筆")
    for c in res["closed_far"]:
        print(f"    [類{c['category']}] {c['title'][:70]}")
    print(f"\n🌧 天氣警示：{len(res['weather'])} 筆")
    for w in res["weather"][:5]:
        print(f"    {w['title'][:70]}")
