#!/usr/bin/env python3
"""
KT BIKER 本地 Daemon
每 60 秒輪詢 Supabase pending_tasks，執行對應工具後推播結果。
開機自動啟動：~/Library/LaunchAgents/com.ktbiker.daemon.plist
"""
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from supabase import create_client

# ── 路徑設定 ──────────────────────────────────────────────────────────────────
BASE = Path(__file__).parent
LOG_FILE = BASE / "daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)s  %(message)s",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────────
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

POLL_INTERVAL = 60  # 秒

# ── Task handlers ─────────────────────────────────────────────────────────────

_auto_mod = None  # 第一次載入後快取，避免每次任務都重載模組

def _import_auto():
    """延遲 import，避免 daemon 啟動時缺少 env 而崩潰；載入後快取。"""
    global _auto_mod
    if _auto_mod is None:
        import importlib.util
        spec = importlib.util.spec_from_file_location("kt_biker_auto", BASE / "kt_biker_auto.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _auto_mod = mod
    return _auto_mod

def _run_competitor(params: dict):
    return _import_auto().push_competitor_report(
        profile_keyword=params.get("profile") or None,
        target=params.get("target", "both"),
    )

def _run_shopee(params: dict):
    return _import_auto().push_shopee_report(
        target=params.get("target", "both"),
    )

def _run_product_perf(params: dict):
    return _import_auto().push_product_perf_report(
        target=params.get("target", "both"),
        period=params.get("period", ""),
    )

def _run_competitor_status(params: dict):
    return _import_auto().query_competitor_status()

def _run_single_platform(params: dict):
    return _import_auto().run_single_platform_report(
        platform=params.get("platform", ""),
        target=params.get("target", "both"),
    )

def _run_latest_platform(params: dict):
    return _import_auto().get_latest_platform_report(
        platform=params.get("platform", ""),
        target=params.get("target", "cc_only"),
    )

# 新增客戶時：在此 dict 加一個 key，並在上方實作對應的 _run_xxx 函式
CLIENT_TASK_HANDLERS = {
    "kt_biker": {
        "competitor_analysis":      _run_competitor,
        "shopee_push":              _run_shopee,
        "product_perf_push":        _run_product_perf,
        "competitor_status":        _run_competitor_status,
        "single_platform_analysis": _run_single_platform,
        "latest_platform_report":   _run_latest_platform,
    },
}

# ── 主迴圈 ────────────────────────────────────────────────────────────────────

def _set_status(task_id: str, status: str, result: str = ""):
    supabase.table("pending_tasks").update({
        "status": status,
        "result": result,
        "updated_at": datetime.now().isoformat(),
    }).eq("id", task_id).execute()


def poll_once():
    rows = (supabase.table("pending_tasks")
            .select("*")
            .eq("status", "pending")
            .order("created_at")
            .limit(1)
            .execute())
    if not rows.data:
        return

    task = rows.data[0]
    task_id   = task["id"]
    task_name = task["task_name"]
    params    = task.get("params") or {}
    client    = params.get("client", "kt_biker")

    log.info(f"收到任務: {client}/{task_name} ({task_id[:8]})")
    _set_status(task_id, "running")

    handler = CLIENT_TASK_HANDLERS.get(client, {}).get(task_name)
    if not handler:
        msg = f"未知任務或客戶: {client}/{task_name}"
        log.error(msg)
        _set_status(task_id, "error", msg)
        return

    try:
        result_msg = handler(params) or "推播完成"
        _set_status(task_id, "done", result_msg)
        log.info(f"任務完成: {task_name}")
    except Exception as e:
        log.error(f"任務失敗: {task_name} — {e}", exc_info=True)
        _set_status(task_id, "error", str(e))


def main():
    log.info("多客戶 Daemon 啟動")
    while True:
        try:
            poll_once()
        except Exception as e:
            log.error(f"poll 錯誤: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
