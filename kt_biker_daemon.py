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
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger(__name__)

# ── Supabase ──────────────────────────────────────────────────────────────────
supabase = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_KEY"])

POLL_INTERVAL = 60  # 秒

# ── Task handlers ─────────────────────────────────────────────────────────────

def _import_auto():
    """延遲 import，避免 daemon 啟動時缺少 env 而崩潰。"""
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("kt_biker_auto", BASE / "kt_biker_auto.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

TASK_HANDLERS = {
    "competitor_analysis": lambda params: _import_auto().push_competitor_report(),
    "shopee_push":         lambda params: _import_auto().push_shopee_report(),
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

    log.info(f"收到任務: {task_name} ({task_id[:8]})")
    _set_status(task_id, "running")

    handler = TASK_HANDLERS.get(task_name)
    if not handler:
        msg = f"未知任務: {task_name}"
        log.error(msg)
        _set_status(task_id, "error", msg)
        return

    try:
        handler(params)
        _set_status(task_id, "done", "推播完成")
        log.info(f"任務完成: {task_name}")
    except Exception as e:
        log.error(f"任務失敗: {task_name} — {e}", exc_info=True)
        _set_status(task_id, "error", str(e))


def main():
    log.info("KT BIKER Daemon 啟動")
    while True:
        try:
            poll_once()
        except Exception as e:
            log.error(f"poll 錯誤: {e}", exc_info=True)
        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
