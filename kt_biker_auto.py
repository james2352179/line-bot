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


# ── 商品表現報表推播 ───────────────────────────────────────────────────────────

def _short_name(raw: str, maxlen: int = 14) -> str:
    n = str(raw)
    if "】" in n:
        n = n.split("】", 1)[1].strip()
    n = n.split("〔")[0].split(" ")[0].strip()
    return n[:maxlen] if len(n) > maxlen else n


def _product_perf_rule_based(period, kpis, stars, low, cart, rep) -> str:
    total   = kpis.get("total_revenue", 0)
    count   = int(kpis.get("product_count", 0))
    avg_cvr = kpis.get("avg_cvr", 0)

    to80 = "—"
    top1_name, top1_rev = "（無資料）", 0
    if stars is not None and not stars.empty:
        if "累積佔比" in stars.columns:
            to80 = int((stars["累積佔比"] <= 80).sum())
        top1_name = _short_name(str(stars.iloc[0].get("商品名稱", "")))
        top1_rev  = float(stars.iloc[0].get("銷售額", 0))

    worst_cart_name, worst_cart_rate = "（無）", 0.0
    if cart is not None and not cart.empty:
        worst_cart_name = _short_name(str(cart.iloc[0].get("商品名稱", "")))
        worst_cart_rate = float(cart.iloc[0].get("購→訂率", 0))

    worst_low_name, worst_low_cvr, worst_low_exp = "（無）", 0.0, 0
    if low is not None and not low.empty:
        worst_low_name = _short_name(str(low.iloc[0].get("商品名稱", "")))
        worst_low_cvr  = float(low.iloc[0].get("轉換率_num", 0))
        worst_low_exp  = int(float(low.iloc[0].get("曝光", 0)))

    top_rep_name, top_rep_rate = "（無）", 0.0
    if rep is not None and not rep.empty:
        top_rep_name = _short_name(str(rep.iloc[0].get("商品名稱", "")))
        top_rep_rate = float(rep.iloc[0].get("回購率_num", 0))

    return (
        f"🌟 本期概況\n"
        f"• 月銷售 ${total/10000:.1f}萬 / {count} 件商品 / 轉換率 {avg_cvr:.1f}%\n"
        f"• 前 {to80} 件明星商品貢獻 80% 銷售集中度\n\n"
        f"✅ 優勢\n"
        f"• {top1_name}：${top1_rev/10000:.1f}萬（月銷售冠軍）\n"
        f"• {top_rep_name}：回購率 {top_rep_rate:.0f}%（忠誠客核心）\n\n"
        f"⚠️ 警示\n"
        f"• {worst_cart_name}：購→訂率僅 {worst_cart_rate:.1f}%，棄單嚴重\n"
        f"• {worst_low_name}：曝光 {worst_low_exp//10000:.1f}萬 → 轉換 {worst_low_cvr:.1f}%\n\n"
        f"🎯 行動\n"
        f"1. 立即：{worst_cart_name} 排查競品定價 / 補充客評\n"
        f"2. 本週：{worst_low_name} 主圖 A/B 測試或調整關鍵字\n"
        f"3. 長期：高回購耗材導入購物車提醒推播"
    )


_CN_MONTHS = {
    '一': 1, '二': 2, '三': 3, '四': 4, '五': 5, '六': 6,
    '七': 7, '八': 8, '九': 9, '十': 10, '十一': 11, '十二': 12,
}

def _match_period(query: str, periods: list) -> str | None:
    """從 periods 清單中找最接近 query 的期間標籤（格式 YYYY年MM月）。"""
    import re as _re
    if not query:
        return None
    # 完全比對
    if query in periods:
        return query
    # 解析 year
    year_num = None
    m = _re.search(r'(\d{4})', query)
    if m:
        year_num = int(m.group(1))
    # 解析 month（中文優先）
    month_num = None
    for cn, num in sorted(_CN_MONTHS.items(), key=lambda x: -len(x[0])):
        if f"{cn}月" in query:
            month_num = num
            break
    if month_num is None:
        m = _re.search(r'(\d{1,2})月', query)
        if m:
            month_num = int(m.group(1))
    if month_num is None:
        return None
    month_str = f"{month_num:02d}月"
    for p in periods:
        if month_str in p:
            if year_num is None or f"{year_num}年" in p:
                return p
    return None


def push_product_perf_report(target: str = "both", period: str = ""):
    """推播商品表現分析報表（讀 product_perf_history.json，支援指定月份）"""
    shopee_str = str(SHOPEE_TOOL_DIR)
    if shopee_str not in sys.path:
        sys.path.insert(0, shopee_str)

    from core.storage.product_perf_history import ProductPerfHistory
    from core.reporter import product_performance_exporter as pex
    from core.reporter import product_performance_charts as pcharts
    from core.reporter.netlify_uploader import NetlifyUploader

    settings_path = SHOPEE_TOOL_DIR / "config" / "settings.json"
    if not settings_path.exists():
        push("⚠️ 商品表現報表：找不到工具設定檔。")
        return
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    netlify_token = settings.get("netlify_token", "").strip()
    if not netlify_token:
        push("⚠️ 商品表現報表：尚未設定 Netlify Token，請先在工具中設定。")
        return

    data_dir = SHOPEE_TOOL_DIR / "data"
    history  = ProductPerfHistory(data_dir)
    periods  = history.get_periods()
    if not periods:
        push("⚠️ 商品表現報表：尚無歷史記錄，請先在工具中分析一期資料。")
        return

    if period:
        matched = _match_period(period, periods)
        if not matched:
            avail = "、".join(periods[:5])
            push(f"⚠️ 商品表現報表：找不到「{period}」的紀錄。\n可用期間：{avail}")
            return
        latest_period = matched
        log.info(f"指定期間：{period} → 比對到 {latest_period}")
    else:
        latest_period = periods[0]

    rec = history.load(latest_period)
    kpis         = rec.get("kpis", {})
    stars        = rec.get("stars")
    low_cvr      = rec.get("low_cvr")
    cart_abandon = rec.get("cart_abandon")
    repurchase   = rec.get("repurchase")

    # 生成圖表
    chart_list = []
    if stars is not None and not stars.empty:
        b64_bar = pcharts.top_revenue_bar(stars)
        if b64_bar:
            chart_list.append(b64_bar)
        b64_pie = pcharts.revenue_concentration_pie(stars, total_revenue=kpis.get("total_revenue", 0))
        if b64_pie:
            chart_list.append(b64_pie)

    # AI 分析：有 API Key 就呼叫，否則規則分析
    ai_text = _product_perf_rule_based(latest_period, kpis, stars, low_cvr, cart_abandon, repurchase)
    api_key = settings.get("api_key", "").strip()
    if api_key:
        try:
            import anthropic
            ctx_lines = [f"【期間】{latest_period}", ""]
            total = kpis.get("total_revenue", 0)
            ctx_lines += [
                f"- 總銷售額：${total:,.0f} TWD",
                f"- 商品數：{int(kpis.get('product_count',0))} 件",
                f"- 平均轉換率：{kpis.get('avg_cvr',0):.1f}%",
                f"- 平均回購率：{kpis.get('avg_repurchase',0):.1f}%",
            ]
            if stars is not None and not stars.empty and "累積佔比" in stars.columns:
                ctx_lines.append(f"- 80/20集中度：前 {int((stars['累積佔比']<=80).sum())} 件商品貢獻80%銷售")
            prompt = "\n".join(ctx_lines) + """

你是蝦皮商品數據分析師。根據上述數據，用繁體中文輸出極簡決策摘要。
【輸出規則】每bullet不超過20字，含具體數字；總字數180字以內；嚴格按格式：

🌟 本期概況
• [月銷售額 $XXX萬 / X件商品 / 轉換率X%]
• [前X件明星商品貢獻80%銷售]

✅ 優勢
• [最強商品名：金額]
• [最高回購商品名：回購率X%]

⚠️ 警示
• [最嚴重棄單商品名：購→訂X%]
• [最嚴重低轉換商品名：曝光X萬 → 轉換X%]

🎯 行動（3條，每條15字內）
1. 立即：[具體動作+商品名]
2. 本週：[具體動作]
3. 長期：[具體動作]"""
            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=settings.get("model", "claude-sonnet-4-6"),
                max_tokens=600,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = msg.content[0].text
            log.info("商品表現 AI 分析完成")
        except Exception as e:
            log.warning(f"商品表現 AI 分析失敗，改用規則分析: {e}")

    html = pex.generate(
        latest_period, kpis, stars, low_cvr, cart_abandon, repurchase,
        ai_text=ai_text, charts_b64=chart_list,
    )

    log.info(f"上傳商品表現報表：{latest_period}")
    url = NetlifyUploader(netlify_token).upload(html)

    share_path = data_dir / "product_perf_latest_share.json"
    share_path.write_text(
        json.dumps({"url": url, "period": latest_period,
                    "saved_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    message = f"📦 商品表現報表｜{latest_period}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"商品表現報表已推播: {latest_period} {url}（→員工群）")
    else:
        log.info(f"商品表現報表完成: {latest_period} {url}（僅回傳 CC）")
    return message


# ── 入口 ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    if mode == "shopee":
        push_shopee_report()
    elif mode == "competitor":
        push_competitor_report()
    elif mode == "product_perf":
        push_product_perf_report()
    else:
        print("用法: python kt_biker_auto.py [shopee|competitor|product_perf]")
        sys.exit(1)
