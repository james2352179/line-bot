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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ── 設定 ──────────────────────────────────────────────────────────────────────

KT_ACCESS_TOKEN   = os.environ["KT_CHANNEL_ACCESS_TOKEN"]
KT_GROUP_ID       = os.environ["KT_GROUP_ID"]
SHOPEE_TOOL_DIR   = Path("/Users/kuanghao/Downloads/kuanghao-claude/kh_shopee_tool")
COMPETITOR_DIR    = Path("/Users/kuanghao/Downloads/kuanghao-claude/kh_competitor_tool")
COMPETITOR_URL    = "http://127.0.0.1:5173"
COMPETITOR_PID    = "3b1c961a"   # 汽車美容用品業
# 競品戰情室 webapp 的依賴（flask/playwright/pywebview）裝在系統 python3，
# 與手動雙擊 launch.command 用的直譯器一致；勿改回 homebrew python3.12（缺 flask 會啟動逾時）
PYTHON            = "/usr/bin/python3"


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
    share_path = SHOPEE_TOOL_DIR / "data" / "latest_share.json"
    if not share_path.exists():
        push("⚠️ 蝦皮廣告報表：尚無報表，請先在工具中分析並上傳一期資料。")
        return
    share = json.loads(share_path.read_text(encoding="utf-8"))
    url = share["url"]
    period = share.get("period", "")
    message = f"📊 蝦皮廣告報表｜{period}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"蝦皮報表已推播: {period} {url}（→員工群）")
    else:
        log.info(f"蝦皮報表完成: {period} {url}（僅回傳 CC）")
    return message


# ── 短影音報表推播（月5號） ──────────────────────────────────────────────────────

def push_short_video_report(target: str = "both"):
    share_path = SHOPEE_TOOL_DIR / "data" / "short_video_latest_share.json"
    if not share_path.exists():
        push("⚠️ 短影音報表：尚無報表，請先在工具中分析並上傳一期資料。")
        return
    share = json.loads(share_path.read_text(encoding="utf-8"))
    url = share["url"]
    period = share.get("period", "")
    message = f"🎬 短影音數據報表｜{period}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"短影音報表已推播: {period} {url}（→員工群）")
    else:
        log.info(f"短影音報表完成: {period} {url}（僅回傳 CC）")
    return message


def _push_short_video_report_full(target: str = "both"):
    """完整重新產生短影音報表（AI 分析 + 上傳 CF Pages）。工具端分享時使用。"""
    import pandas as pd

    shopee_str = str(SHOPEE_TOOL_DIR)
    if shopee_str not in sys.path:
        sys.path.insert(0, shopee_str)

    from core.reporter.short_video_exporter import generate
    from core.reporter.cf_pages_uploader import PagesUploader

    settings_path = SHOPEE_TOOL_DIR / "config" / "settings.json"
    if not settings_path.exists():
        push("⚠️ 短影音報表：找不到工具設定檔。")
        return
    settings = json.loads(settings_path.read_text(encoding="utf-8"))

    cf_token = settings.get("cf_api_token", "").strip()
    if not cf_token:
        push("⚠️ 短影音報表：尚未設定 Cloudflare API Token。")
        return

    file_paths = [p for p in settings.get("short_video_files", []) if Path(p).exists()]
    if not file_paths:
        push("⚠️ 短影音報表：尚無已載入的 CSV，請先在工具中匯入資料。")
        return

    _NUMERIC = [
        "總觀眾數", "總觀看次數", "加入購物車總次數",
        "買家數(可出貨訂單)", "平均客單價(可出貨訂單)", "千次觀看交易額(可出貨訂單)",
        "有效觀看次數(觀看蝦皮短影音 3 秒以上)",
    ]

    def _load(path):
        raw = pd.read_csv(path, encoding="utf-8-sig", header=None, dtype=str)
        cols = raw.iloc[1].tolist()
        df = raw.iloc[2:].copy()
        df.columns = cols
        df = df[df["數據時段"].str.match(r"^\d{4}-\d{2}-\d{2}$", na=False)].copy()
        df["日期"] = pd.to_datetime(df["數據時段"], errors="coerce")
        df = df.dropna(subset=["日期"])
        for col in _NUMERIC:
            if col in df.columns:
                df[col] = pd.to_numeric(
                    df[col].str.replace(",", "").str.replace("$", "").str.replace("%", ""),
                    errors="coerce",
                )
        return df

    dfs = []
    for p in file_paths:
        try:
            dfs.append(_load(p))
        except Exception as e:
            log.warning(f"短影音 CSV 載入失敗 {p}: {e}")

    if not dfs:
        push("⚠️ 短影音報表：所有 CSV 載入失敗。")
        return

    merged = (pd.concat(dfs, ignore_index=True)
              .drop_duplicates(subset=["日期"])
              .sort_values("日期")
              .reset_index(drop=True))

    # 取最新月份
    latest_month = merged["日期"].dt.strftime("%Y-%m").max()
    df = merged[merged["日期"].dt.strftime("%Y-%m") == latest_month].copy()

    # 計算派生欄位
    views   = df["總觀看次數"].fillna(0)
    viewers = df["總觀眾數"].fillna(0)
    cart    = df["加入購物車總次數"].fillna(0)
    buyers  = df["買家數(可出貨訂單)"].fillna(0)
    df["估計收入"] = (buyers * df["平均客單價(可出貨訂單)"].fillna(0)).round(0)
    df["加購率%"]  = (cart / views * 100).where(views > 0).round(2)
    df["成交率%"]  = (buyers / viewers * 100).where(viewers > 0).round(2)

    rpv_series = df["千次觀看交易額(可出貨訂單)"].dropna()
    kpis = {
        "total_revenue": df["估計收入"].sum(),
        "avg_rpv":       rpv_series.mean() if not rpv_series.empty else 0,
        "cart_rate":     cart.sum() / views.sum() * 100 if views.sum() > 0 else 0,
        "cvr":           buyers.sum() / viewers.sum() * 100 if viewers.sum() > 0 else 0,
        "days":          len(df),
        "total_cart":    cart.sum(),
        "total_buyers":  buyers.sum(),
    }

    # ── AI 分析 ───────────────────────────────────────────────────────────────────
    ai_text = ""
    api_key = settings.get("api_key", "").strip()
    if api_key:
        try:
            import anthropic

            def _n(v):
                return 0 if (v is None or (isinstance(v, float) and pd.isna(v))) else v

            promo_days = {18, 25}
            month_num  = int(latest_month.split("-")[1]) if "-" in latest_month else 0
            rows_txt = []
            for _, r in df.iterrows():
                d   = r["日期"]
                is_promo = (d.day in promo_days) or (d.month == d.day)
                tag = "★促銷" if is_promo else ""
                rows_txt.append(
                    f"{str(d)[:10]}  觀看:{int(_n(r.get('總觀看次數',0))):>6}  "
                    f"加購:{int(_n(r.get('加入購物車總次數',0))):>4}  "
                    f"加購率:{_n(r.get('加購率%',0)):.2f}%  "
                    f"買家:{int(_n(r.get('買家數(可出貨訂單)',0))):>3}  "
                    f"客單:${_n(r.get('平均客單價(可出貨訂單)',0)):.0f}  "
                    f"千次交易額:${_n(r.get('千次觀看交易額(可出貨訂單)',0)):.0f}  {tag}"
                )

            system_ctx = (
                "你是熟悉蝦皮平台生態的電商數據分析師。\n\n"
                "【蝦皮平台促銷日曆——分析前必須優先考量】\n"
                "以下日期出現波動屬正常預期，請歸類為「促銷日效應」：\n"
                "・月份疊字節：1/1、2/2、3/3、4/4、5/5、6/6、7/7、8/8、9/9、10/10、11/11（雙11）、12/12（雙12）\n"
                "・每月固定促銷：每月 18 日、25 日\n"
                "・促銷後一天通常有報復性低谷，屬正常，不需標記\n\n"
                "分析原則：\n"
                "1. 促銷日 → 評估「是否達到促銷應有水準」（與同月非促銷日均值比較）\n"
                "2. 非促銷日異常 → 才算真正值得追查的訊號\n"
                "3. 輸出繁體中文，簡潔有力，整體不超過 380 字"
            )
            prompt = (
                f"以下是 {latest_month} 短影音帶貨每日數據（★ 為促銷日）：\n\n"
                + "\n".join(rows_txt)
                + "\n\n請分析以下三個維度，每個維度 2-3 句，最後給行動建議 3 條（具體可操作）：\n"
                "1. **促銷日表現評估**：本月促銷日實際表現如何？哪個超預期、哪個低於預期？\n"
                "2. **非促銷日的真實異常**：排除促銷日後，哪幾天有明顯的客單價或加購率變化？\n"
                "3. **本月流量品質判斷**：整體觀看數與千次交易額的關係說明了什麼？\n\n"
                "⚡ 行動建議（3 條，格式：**標題**：內容）"
            )

            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=settings.get("model", "claude-haiku-4-5-20251001"),
                max_tokens=650,
                system=system_ctx,
                messages=[{"role": "user", "content": prompt}],
            )
            ai_text = msg.content[0].text
            log.info("AI 分析完成")
        except Exception as e:
            log.warning(f"AI 分析失敗（繼續生成報表）：{e}")

    log.info(f"生成短影音報表 HTML：{latest_month}")
    html = generate(latest_month, df, kpis, ai_text=ai_text, fig=None)
    url  = PagesUploader(cf_token).upload(html, site_key="short_video")

    share_path = SHOPEE_TOOL_DIR / "data" / "short_video_latest_share.json"
    share_path.write_text(
        json.dumps({"url": url, "period": latest_month,
                    "saved_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    message = f"🎬 短影音數據報表｜{latest_month}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"短影音報表已推播: {latest_month} {url}（→員工群）")
    else:
        log.info(f"短影音報表完成: {latest_month} {url}（僅回傳 CC）")
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
    # 不要把輸出丟 DEVNULL：失敗時（如 ModuleNotFoundError）才查得到原因
    startup_log = COMPETITOR_DIR / "data" / "webapp_startup.log"
    startup_log.parent.mkdir(parents=True, exist_ok=True)
    logf = open(startup_log, "w")
    proc = subprocess.Popen(
        [PYTHON, "webapp/app.py"],
        cwd=str(COMPETITOR_DIR),
        stdout=logf,
        stderr=subprocess.STDOUT,
    )
    for _ in range(30):   # 最多等 60 秒
        time.sleep(2)
        if _webapp_ready():
            log.info("webapp 就緒")
            return True
        if proc.poll() is not None:   # process 提早死亡，不用空等 60 秒
            logf.flush()
            tail = startup_log.read_text(errors="replace")[-500:]
            log.error(f"webapp 啟動即崩潰 (exit={proc.returncode})，log 末段：\n{tail}")
            return False
    log.error(f"webapp 啟動逾時（60 秒未就緒），詳見 {startup_log}")
    return False


def _chromium_exists() -> bool:
    """用 webapp 同一個直譯器(PYTHON)檢查 Playwright Chromium executable 是否存在。"""
    check = (
        "from playwright.sync_api import sync_playwright; from pathlib import Path; "
        "p=sync_playwright().start(); ok=Path(p.chromium.executable_path).exists(); "
        "p.stop(); print('OK' if ok else 'MISSING')"
    )
    try:
        r = subprocess.run([PYTHON, "-c", check], cwd=str(COMPETITOR_DIR),
                           capture_output=True, text=True, timeout=30)
        return r.stdout.strip().endswith("OK")
    except Exception as e:
        log.warning(f"chromium 檢查失敗: {e}")
        return False


def _ensure_chromium_ready(timeout: int = 180) -> bool:
    """跑分析前確認 Playwright Chromium 已就緒，根治冷啟動 race。
    webapp(app.py)冷啟動會背景下載 chromium，若沒等它裝完就跑分析，
    IG/FB/Threads 會 'BrowserType.launch: Executable doesn't exist' 全部失敗。
    正常情況 chromium 已在 → 第一次輪詢即過、零延遲；只有首次冷啟動會等。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _chromium_exists():
            return True
        log.info("等待 Playwright Chromium 背景下載完成…")
        time.sleep(5)
    # 等背景下載逾時仍沒有 → 主動補裝一次當 fallback
    log.warning("Chromium 背景下載逾時，主動補裝…")
    try:
        subprocess.run([PYTHON, "-m", "playwright", "install", "chromium"],
                       cwd=str(COMPETITOR_DIR), capture_output=True, timeout=300)
    except Exception as e:
        log.error(f"chromium 補裝失敗: {e}")
    ok = _chromium_exists()
    if not ok:
        log.error("⚠️ Chromium 仍未就緒，IG/FB/Threads 可能失敗")
    return ok


def _run_analysis_and_wait():
    """觸發全平台分析，等待 SSE done 事件，最多等 90 分鐘。
    若分析已在進行中（409），改為輪詢 /status 等待結束。
    """
    log.info("開始全平台分析…")
    r = requests.get(f"{COMPETITOR_URL}/api/analysis/all/run", stream=True, timeout=5400)

    if r.status_code == 409:
        log.info("分析已在進行中，等待現有分析完成…")
        deadline = time.time() + 5400
        while time.time() < deadline:
            try:
                st = requests.get(f"{COMPETITOR_URL}/api/analysis/all/status", timeout=10).json()
                if not st.get("running"):
                    log.info("分析完成（等待現有執行結束）")
                    return True
            except Exception:
                pass
            time.sleep(30)
        log.error("等待現有分析超時（90 分鐘）")
        return False

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
        # 冷啟動後 chromium 可能還在背景下載，跑分析前先擋一道，避免 IG/FB/Threads 全掛
        _ensure_chromium_ready()

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

    # 發布到 Cloudflare Pages（最多重試 2 次，失敗平台單獨補發）
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


# ── 競品戰情室進階指令 ────────────────────────────────────────────────────────

PLATFORM_NAMES = {
    "youtube": "YouTube", "tiktok": "TikTok",
    "facebook": "Facebook", "instagram": "Instagram", "threads": "Threads",
}


def query_competitor_status() -> str:
    """查詢競品戰情室目前是否有分析在執行"""
    if not _webapp_ready():
        return "競品戰情室尚未啟動，無法查詢狀態。"
    try:
        st = requests.get(f"{COMPETITOR_URL}/api/analysis/all/status", timeout=10).json()
        if st.get("running"):
            return "⏳ 目前有競品分析正在執行中，請稍候。"
        return "✅ 目前沒有分析在執行，可以下達新的分析指令。"
    except Exception as e:
        return f"⚠️ 無法取得分析狀態：{e}"


def _run_platform_and_wait(platform: str) -> bool:
    """觸發單一平台分析並等待完成（SSE + 409 fallback）"""
    log.info(f"開始 {platform} 平台分析…")
    r = requests.get(f"{COMPETITOR_URL}/api/analysis/{platform}/run", stream=True, timeout=5400)
    if r.status_code == 409:
        log.info(f"{platform} 分析已在進行中，輪詢等待…")
        deadline = time.time() + 5400
        while time.time() < deadline:
            try:
                st = requests.get(f"{COMPETITOR_URL}/api/analysis/{platform}/status", timeout=10).json()
                if not st.get("running"):
                    return True
            except Exception:
                pass
            time.sleep(30)
        return False
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
            log.info(f"{platform} 分析完成")
            return True
        if kind == "error":
            log.error(f"{platform} 分析失敗: {event.get('payload')}")
            return False
    return False


def _publish_platform(platform: str) -> str | None:
    """發布單一平台已分析的報告到 CF Pages，回傳 URL；失敗回傳 None"""
    try:
        r = requests.post(f"{COMPETITOR_URL}/api/analysis/{platform}/publish", timeout=120)
        d = r.json()
        if d.get("ok") and d.get("url"):
            return d["url"]
        log.warning(f"{platform} 發布失敗: {d}")
    except Exception as e:
        log.error(f"{platform} 發布例外: {e}")
    return None


def run_single_platform_report(platform: str, target: str = "both") -> str | None:
    """跑單一平台競品分析並推播報告"""
    if platform not in PLATFORM_NAMES:
        return f"⚠️ 不支援的平台：{platform}"
    if not _webapp_ready():
        if not _start_webapp():
            return "⚠️ 競品戰情室啟動逾時，請手動執行。"
    label = PLATFORM_NAMES[platform]
    if not _run_platform_and_wait(platform):
        msg = f"⚠️ {label} 競品分析執行失敗，請手動檢查。"
        if target != "cc_only":
            push(msg)
        return msg
    url = _publish_platform(platform)
    if not url:
        msg = f"⚠️ {label} 報告發布失敗，請手動檢查。"
        if target != "cc_only":
            push(msg)
        return msg
    message = f"📈 {label} 競品分析報表\n{url}"
    if target != "cc_only":
        push(message)
    log.info(f"{label} 單平台分析完成: {url}（target={target}）")
    return message


def get_latest_platform_report(platform: str, target: str = "cc_only") -> str | None:
    """重新發布上次已分析的平台報告（直接讀本地 HTML，不需要啟動 webapp）"""
    if platform not in PLATFORM_NAMES:
        return f"⚠️ 不支援的平台：{platform}"

    # 各平台報告目錄與 CF Pages site_key（與 webapp app.py 保持一致）
    REPORT_DIRS = {
        "youtube":   COMPETITOR_DIR / "reports",
        "tiktok":    COMPETITOR_DIR / "tiktok_reports",
        "facebook":  COMPETITOR_DIR / "fb_reports",
        "instagram": COMPETITOR_DIR / "ig_reports",
        "threads":   COMPETITOR_DIR / "threads_reports",
    }
    SITE_KEYS = {
        "youtube":   "competitor-yt",
        "tiktok":    "competitor-tt",
        "facebook":  "competitor-fb",
        "instagram": "competitor-ig",
        "threads":   "competitor-th",
    }

    label    = PLATFORM_NAMES[platform]
    rep_dir  = REPORT_DIRS[platform]
    site_key = SITE_KEYS[platform]

    # 找最新 HTML 報告（按 mtime 排序）
    from pathlib import Path as _Path
    reports = sorted(_Path(rep_dir).glob("*.html"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not reports:
        msg = f"⚠️ {label} 本地尚無報告，請先執行分析。"
        if target != "cc_only":
            push(msg)
        return msg

    # 讀取 CF API Token（從蝦皮工具 settings.json 共用）
    shopee_settings = SHOPEE_TOOL_DIR / "config" / "settings.json"
    cf_token = ""
    if shopee_settings.exists():
        cf_token = json.loads(shopee_settings.read_text(encoding="utf-8")).get("cf_api_token", "").strip()
    if not cf_token:
        msg = f"⚠️ 尚未設定 Cloudflare API Token，請先在蝦皮工具設定頁填入。"
        if target != "cc_only":
            push(msg)
        return msg

    # 直接上傳到 CF Pages，完全不需要啟動 webapp
    shopee_str = str(SHOPEE_TOOL_DIR)
    if shopee_str not in sys.path:
        sys.path.insert(0, shopee_str)
    from core.reporter.cf_pages_uploader import PagesUploader

    html = reports[0].read_text(encoding="utf-8")
    url  = PagesUploader(cf_token).upload(html, site_key=site_key)

    message = f"📈 {label} 最新競品報告（上次分析結果）\n{url}"
    if target != "cc_only":
        push(message)
    log.info(f"{label} 最新報告推播: {url}（target={target}）")
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
    """推播商品表現報表：直接推最後一次上傳的 CF Pages URL，不重新產生。"""
    share_path = SHOPEE_TOOL_DIR / "data" / "product_perf_latest_share.json"
    if not share_path.exists():
        push("⚠️ 商品表現報表：尚無報表，請先在工具中分析並上傳一期資料。")
        return
    share = json.loads(share_path.read_text(encoding="utf-8"))
    url = share["url"]
    share_period = share.get("period", "")
    message = f"📦 商品表現報表｜{share_period}\n\n{url}"
    if target != "cc_only":
        push(message)
        log.info(f"商品表現報表已推播: {share_period} {url}（→員工群）")
    else:
        log.info(f"商品表現報表完成: {share_period} {url}（僅回傳 CC）")
    return message


def _push_product_perf_report_full(target: str = "both", period: str = ""):
    """完整重新產生商品表現報表（AI 分析 + 上傳 CF Pages）。工具端分享時使用。"""
    shopee_str = str(SHOPEE_TOOL_DIR)
    if shopee_str not in sys.path:
        sys.path.insert(0, shopee_str)

    from core.storage.product_perf_history import ProductPerfHistory
    from core.reporter import product_performance_exporter as pex
    from core.reporter import product_performance_charts as pcharts
    from core.reporter.cf_pages_uploader import PagesUploader

    settings_path = SHOPEE_TOOL_DIR / "config" / "settings.json"
    if not settings_path.exists():
        push("⚠️ 商品表現報表：找不到工具設定檔。")
        return
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    cf_token = settings.get("cf_api_token", "").strip()
    if not cf_token:
        push("⚠️ 商品表現報表：尚未設定 Cloudflare API Token，請先在工具中設定。")
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

            def _rows(df, cols, n=5):
                if df is None or df.empty:
                    return ["  （無資料）"]
                out = []
                for _, r in df.head(n).iterrows():
                    out.append("  " + " | ".join(
                        f"{c}: {r.get(c, '')}" for c in cols if c in r
                    ))
                return out

            total = kpis.get("total_revenue", 0)
            to80 = int((stars["累積佔比"] <= 80).sum()) if (stars is not None and not stars.empty and "累積佔比" in stars.columns) else "—"
            ctx_lines = [
                f"【期間】{latest_period}", "",
                "【整體 KPI】",
                f"- 總銷售額：${total:,.0f} TWD",
                f"- 商品數：{int(kpis.get('product_count', 0))} 件",
                f"- 平均轉換率：{kpis.get('avg_cvr', 0):.1f}%",
                f"- 平均回購率：{kpis.get('avg_repurchase', 0):.1f}%",
                f"- 80/20集中度：前 {to80} 件商品貢獻80%銷售", "",
                "【銷售前5名（銷售明星）】",
                *_rows(stars, ["商品名稱", "銷售額", "訂單_num", "轉換率_num", "累積佔比"]), "",
                "【高曝光低轉換（前5名）】",
                *_rows(low_cvr, ["商品名稱", "曝光", "轉換率_num", "加購率_num", "跳出率_num"]), "",
                "【棄單警告（前5名）】",
                *_rows(cart_abandon, ["商品名稱", "加購數", "購→訂率", "銷售額"]), "",
                "【高回購品（前5名）】",
                *_rows(repurchase, ["商品名稱", "回購率_num", "回購天數_num"]),
            ]
            ctx = "\n".join(ctx_lines)

            prompt = f"""你是蝦皮電商分析師，幫業主生成一份「決策摘要」。業主看完後要能馬上知道「哪幾個商品有問題」以及「該怎麼做」。

{ctx}

【強制規則】
1. 每個 bullet 必須點名「具體商品名稱」——取上方資料的真實名稱，截短至15字內的核心關鍵詞（去掉規格、符號等雜訊）
2. 嚴格禁止空泛描述，例如「長尾商品」「頭部商品」「某些商品」「明星商品」
3. 沒有商品名的 bullet = 廢話，不要寫
4. 總字數 230 字以內，不得增減區塊

--- 輸出格式 ---

🌟 本期概況
• 銷售額 $X萬 ／ X件有銷售商品 ／ 前X件撐起80%營收
• 平均轉換率 X% ／ 平均回購率 X%

✅ 表現最佳
• 【商品名A】：銷售額 $X萬，轉換率 X%
• 【商品名B】：回購率 X%，每 X 天回購

⚠️ 需要立即處理（最嚴重的 2–3 個）
• 【商品名C】：加購 X 次但購→訂率 X%（棄單原因：[競品更低價 or 運費門檻 or 評價不足]）
• 【商品名D】：曝光 X 萬但轉換率 X%（建議：[換主圖 or 調標題 or 降價]）

🎯 本週行動
1. 【商品名C】降 $X 元或設滿額折扣，目標購→訂率到 X%
2. 【商品名D】更換主圖，參考【商品名A】的版面風格
3. 【商品名B】推舊客優先通知 or 訂閱優惠，鞏固回購週期"""

            client = anthropic.Anthropic(api_key=api_key)
            msg = client.messages.create(
                model=settings.get("model", "claude-sonnet-4-6"),
                max_tokens=800,
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
    url = PagesUploader(cf_token).upload(html, site_key="product_perf")

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
    elif mode == "short_video":
        push_short_video_report()
    else:
        print("用法: python kt_biker_auto.py [shopee|competitor|product_perf|short_video]")
        sys.exit(1)
