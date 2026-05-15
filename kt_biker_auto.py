#!/usr/bin/env python3
"""
KT BIKER 自動化推播腳本
用法：
  python kt_biker_auto.py shopee      # 月2號：推蝦皮最新報表
  python kt_biker_auto.py competitor  # 月1、15號：跑競品分析並推播
"""
import json
import os
import subprocess
import sys
import time
import logging
from datetime import datetime
from pathlib import Path

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(Path(__file__).parent / "kt_biker_auto.log", encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── 設定 ──────────────────────────────────────────────────────────────────────

KT_ACCESS_TOKEN   = os.environ["KT_CHANNEL_ACCESS_TOKEN"]
KT_GROUP_ID       = os.environ["KT_GROUP_ID"]
SHOPEE_TOOL_DIR   = Path("/Users/kuanghao/Downloads/kuanghao-claude/kh_shopee_tool")
COMPETITOR_DIR    = Path("/Users/kuanghao/Downloads/kuanghao-claude/kh_competitor_tool")
COMPETITOR_URL    = "http://127.0.0.1:5173"
COMPETITOR_PID    = "3b1c961a"   # 汽車美容用品業
PYTHON            = "/opt/homebrew/bin/python3.12"


# ── LINE 推播 ─────────────────────────────────────────────────────────────────

def push(message: str):
    resp = requests.post(
        "https://api.line.me/v2/bot/message/push",
        headers={"Authorization": f"Bearer {KT_ACCESS_TOKEN}", "Content-Type": "application/json"},
        json={"to": KT_GROUP_ID, "messages": [{"type": "text", "text": message}]},
        timeout=15,
    )
    if resp.status_code != 200:
        log.error(f"LINE push 失敗: {resp.status_code} {resp.text}")
    else:
        log.info("LINE push 成功")


# ── 蝦皮報表推播（月2號） ──────────────────────────────────────────────────────

def _kpi_compare(current: dict, previous: dict) -> dict:
    """KPIEngine.compare() 的純Python版（無pandas依賴）"""
    if not previous:
        return {}
    metrics = ["total_spend", "total_clicks", "total_orders", "total_revenue",
               "cvr", "cpc", "roas", "ctr"]
    result = {}
    for m in metrics:
        curr = current.get(m, 0)
        prev = previous.get(m, 0)
        result[m] = {
            "current": curr,
            "previous": prev,
            "change_pct": round((curr - prev) / prev * 100, 1) if prev != 0 else None,
        }
    return result


def push_shopee_report(target: str = "both"):
    # 加入蝦皮工具路徑（只加一次）
    shopee_str = str(SHOPEE_TOOL_DIR)
    if shopee_str not in sys.path:
        sys.path.insert(0, shopee_str)

    from core.storage.history_store import HistoryStore
    from core.reporter.html_exporter import HTMLExporter
    from core.reporter.netlify_uploader import NetlifyUploader

    # 讀取 Netlify Token
    settings_path = SHOPEE_TOOL_DIR / "config" / "settings.json"
    if not settings_path.exists():
        push("⚠️ 蝦皮廣告報表：找不到工具設定檔。")
        return
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    netlify_token = settings.get("netlify_token", "").strip()
    if not netlify_token:
        push("⚠️ 蝦皮廣告報表：尚未設定 Netlify Token，請先在工具中設定。")
        return

    # 取得最新一期歷史記錄
    data_dir = SHOPEE_TOOL_DIR / "data"
    history = HistoryStore(str(data_dir))
    periods = history.get_all_periods()
    if not periods:
        push("⚠️ 蝦皮廣告報表：尚無歷史記錄，請先在工具中分析一期資料。")
        return

    latest_period = periods[-1]
    rec = history.get_record(latest_period)
    kpis = rec.get("kpis", {})

    analysis_result = {
        "kpis": kpis,
        "comparison": _kpi_compare(kpis, history.get_previous(latest_period)),
        "anomalies": rec.get("anomalies", []),
        "charts": rec.get("chart_paths", []),
        "ai_analysis": rec.get("ai_analysis", ""),
        "period": latest_period,
        "metadata": {},
    }

    log.info(f"生成蝦皮報表 HTML：{latest_period}")
    html = HTMLExporter().generate(analysis_result)
    url = NetlifyUploader(netlify_token).upload(html)

    # 存檔備用
    share_path = data_dir / "latest_share.json"
    share_path.write_text(
        json.dumps({"url": url, "period": latest_period,
                    "saved_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    message = f"📊 蝦皮廣告報表｜{latest_period}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"蝦皮報表已推播: {latest_period} {url}（→員工群）")
    else:
        log.info(f"蝦皮報表完成: {latest_period} {url}（僅回傳 CC）")
    return message


# ── 競品戰情室推播（月1、15號） ────────────────────────────────────────────────

def _webapp_ready() -> bool:
    try:
        requests.get(f"{COMPETITOR_URL}/api/profiles", timeout=3)
        return True
    except Exception:
        return False


def _find_profile_id(keyword: str) -> str | None:
    """從競品工具 API 搜尋符合關鍵詞的設定檔 ID"""
    try:
        resp = requests.get(f"{COMPETITOR_URL}/api/profiles", timeout=10)
        for p in resp.json():
            if keyword in p.get("name", ""):
                log.info(f"找到設定檔: {p['name']} ({p['id']})")
                return p["id"]
    except Exception as e:
        log.warning(f"設定檔搜尋失敗: {e}")
    return None


def _start_webapp():
    log.info("啟動競品戰情室 webapp…")
    subprocess.Popen(
        [PYTHON, "webapp/app.py"],
        cwd=str(COMPETITOR_DIR),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    for _ in range(30):   # 最多等 60 秒
        time.sleep(2)
        if _webapp_ready():
            log.info("webapp 就緒")
            return True
    return False


def _run_analysis_and_wait():
    """觸發全平台分析，等待 SSE done 事件，最多等 90 分鐘。"""
    log.info("開始全平台分析…")
    with requests.get(f"{COMPETITOR_URL}/api/analysis/all/run", stream=True, timeout=5400) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            try:
                event = json.loads(raw[5:].strip())
            except Exception:
                continue
            kind = event.get("kind", "")
            if kind == "done":
                log.info("分析完成")
                return True
            if kind == "error":
                log.error(f"分析失敗: {event.get('payload')}")
                return False
            if kind not in ("heartbeat", "progress"):
                log.info(f"[{kind}] {str(event.get('payload', ''))[:80]}")
    return False


def push_competitor_report(profile_keyword: str | None = None, target: str = "both"):
    if not _webapp_ready():
        if not _start_webapp():
            push("⚠️ 競品戰情室啟動逾時，請手動執行。")
            log.error("webapp 啟動失敗")
            return None

    # 決定設定檔 ID
    profile_id = COMPETITOR_PID
    profile_name = "汽車美容用品業"
    if profile_keyword:
        found = _find_profile_id(profile_keyword)
        if found:
            profile_id = found
            profile_name = profile_keyword
        else:
            log.warning(f"找不到符合「{profile_keyword}」的設定檔，使用預設")

    resp = requests.post(f"{COMPETITOR_URL}/api/profiles/{profile_id}/activate", timeout=10)
    if not resp.ok:
        push(f"⚠️ 競品分析：無法切換至「{profile_name}」設定檔。")
        log.error(f"activate 失敗: {resp.text}")
        return None
    log.info(f"已切換至 {profile_name}")

    if not _run_analysis_and_wait():
        push("⚠️ 競品分析執行失敗，請手動檢查。")
        return None

    # 發布到 Netlify（最多重試 2 次，失敗平台單獨補發）
    platform_names = {
        "youtube": "YouTube", "tiktok": "TikTok",
        "facebook": "Facebook", "instagram": "Instagram", "threads": "Threads",
    }
    urls: dict = {}

    for attempt in range(2):
        resp = requests.post(f"{COMPETITOR_URL}/api/analysis/all/publish_all", timeout=300)
        data = resp.json()
        if not data.get("ok"):
            log.warning(f"publish_all 第{attempt+1}次失敗: {data.get('error')}")
            time.sleep(10)
            continue
        urls = data.get("urls", {})
        break

    # 補發仍失敗的平台（個別重試一次）
    for platform in ("youtube", "tiktok", "facebook", "instagram", "threads"):
        if urls.get(platform):
            continue
        log.info(f"補發 {platform}…")
        time.sleep(5)
        try:
            r = requests.post(f"{COMPETITOR_URL}/api/analysis/{platform}/publish", timeout=120)
            d = r.json()
            if d.get("ok") and d.get("url"):
                urls[platform] = d["url"]
                log.info(f"{platform} 補發成功: {d['url']}")
            else:
                log.warning(f"{platform} 補發失敗: {d}")
        except Exception as e:
            log.warning(f"{platform} 補發例外: {e}")

    lines = [f"📈 競品分析報表｜{profile_name}\n"]
    failed = []
    for platform, url in urls.items():
        if url:
            lines.append(f"▸ {platform_names.get(platform, platform)}\n{url}")
        else:
            failed.append(platform_names.get(platform, platform))
            log.warning(f"平台發布失敗或無資料: {platform}")

    if failed:
        lines.append(f"\n⚠️ 以下平台未能發布：{', '.join(failed)}")
        log.warning(f"未發布平台: {failed}")

    if len(lines) == 1:
        push("⚠️ 競品分析完成，但所有平台均無報告可發布。")
        return None

    message = "\n".join(lines)
    if target != "cc_only":
        push(message)
        log.info(f"競品報表推播完成：{len(lines) - 1} 個平台（→員工群）")
    else:
        log.info(f"競品報表完成：{len(lines) - 1} 個平台（僅回傳 CC）")
    return message


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "shopee":
        push_shopee_report()
    elif mode == "competitor":
        push_competitor_report()
    else:
        print("用法: python kt_biker_auto.py [shopee|competitor]")
        sys.exit(1)
