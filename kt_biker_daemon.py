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
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

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

def _run_short_video(params: dict):
    return _import_auto().push_short_video_report(
        target=params.get("target", "both"),
    )

def _run_code_repair(params: dict) -> str:
    """讀相關程式碼 → git 備份 → Claude agent loop 修復 → 回傳摘要"""
    import anthropic
    failed_task = params.get("failed_task", "unknown")
    error_msg   = params.get("error", "（無錯誤訊息）")

    TASK_PRIMARY = {
        "shopee_push":              BASE / "kt_biker_auto.py",
        "product_perf_push":        BASE / "kt_biker_auto.py",
        "competitor_analysis":      BASE / "kt_biker_auto.py",
        "single_platform_analysis": BASE / "kt_biker_auto.py",
        "latest_platform_report":   BASE / "kt_biker_auto.py",
    }
    primary_file = TASK_PRIMARY.get(failed_task, BASE / "kt_biker_auto.py")

    ALLOWED_DIRS = [
        str(BASE),
        "/Users/kuanghao/Downloads/kuanghao-claude/kh_shopee_tool",
        "/Users/kuanghao/Downloads/kuanghao-claude/kh_competitor_tool",
    ]
    SAFE_PREFIXES = ("python", "python3", "ls", "find", "grep", "cat",
                     "git log", "git diff", "git status", "pip")

    # ── git 備份 ──────────────────────────────────────────────
    import subprocess
    subprocess.run(["git", "add", "-A"], cwd=str(BASE), capture_output=True)
    bk = subprocess.run(
        ["git", "commit", "-m", f"auto-backup before repair: {failed_task}"],
        cwd=str(BASE), capture_output=True, text=True
    )
    if "nothing to commit" in bk.stdout + bk.stderr:
        backup_note = "（無新變更，略過備份）"
    else:
        backup_note = "✅ git 備份完成"
    log.info(f"git backup: {bk.stdout.strip() or bk.stderr.strip()}")

    # ── 工具定義 ──────────────────────────────────────────────
    tools = [
        {"name": "read_file",
         "description": "讀取本地檔案內容",
         "input_schema": {"type": "object",
                          "properties": {"path": {"type": "string"}},
                          "required": ["path"]}},
        {"name": "write_file",
         "description": "將修復後的完整內容寫入檔案（完整覆蓋）",
         "input_schema": {"type": "object",
                          "properties": {"path": {"type": "string"},
                                         "content": {"type": "string"}},
                          "required": ["path", "content"]}},
        {"name": "run_command",
         "description": "在 line-bot 目錄執行安全指令（語法檢查、ls、grep 等）",
         "input_schema": {"type": "object",
                          "properties": {"command": {"type": "string"}},
                          "required": ["command"]}},
    ]

    def _exec(name: str, inp: dict) -> str:
        try:
            if name == "read_file":
                p = Path(inp["path"])
                if not any(str(p).startswith(d) for d in ALLOWED_DIRS):
                    return "⚠️ 安全限制：路徑不在允許範圍"
                return p.read_text(encoding="utf-8")
            if name == "write_file":
                p = Path(inp["path"])
                if not any(str(p).startswith(d) for d in ALLOWED_DIRS):
                    return "⚠️ 安全限制：路徑不在允許範圍"
                p.write_text(inp["content"], encoding="utf-8")
                return f"✅ 已寫入 {p.name}"
            if name == "run_command":
                cmd = inp["command"].strip()
                if not any(cmd.startswith(pfx) for pfx in SAFE_PREFIXES):
                    return f"⚠️ 安全限制：指令需以下列開頭：{', '.join(SAFE_PREFIXES)}"
                proc = subprocess.run(
                    cmd, shell=True, cwd=str(BASE),
                    capture_output=True, text=True, timeout=30
                )
                return (proc.stdout + proc.stderr).strip()[:2000]
        except Exception as e:
            return f"工具執行錯誤：{e}"

    # ── Agent loop ────────────────────────────────────────────
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    messages = [{"role": "user", "content": (
        f"你是本地程式碼修復 Agent。\n\n"
        f"主要檔案：{primary_file}\n"
        f"失敗任務：{failed_task}\n"
        f"錯誤訊息：\n{error_msg}\n\n"
        f"請：1) 讀相關程式碼找出根本原因  "
        f"2) 用 write_file 寫入修復後的完整檔案  "
        f"3) 用 run_command 做 Python 語法驗證  "
        f"4) 最後用繁中回傳 100 字內的修復摘要\n\n"
        f"安全限制：只能修改 line-bot、kh_shopee_tool、kh_competitor_tool 目錄內的檔案。"
    )}]

    for _ in range(8):
        resp = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=4096,
            tools=tools,
            messages=messages,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "end_turn":
            summary = next((b.text.strip() for b in resp.content if hasattr(b, "text") and b.text.strip()), "修復完成")
            return f"{backup_note}\n\n{summary[:800]}"

        if resp.stop_reason != "tool_use":
            break

        tool_results = [{"type": "tool_result", "tool_use_id": b.id, "content": _exec(b.name, b.input)}
                        for b in resp.content if b.type == "tool_use"]
        messages.append({"role": "user", "content": tool_results})

    return f"{backup_note}\n⚠️ 自動修復未完成，請手動檢查 {failed_task}"


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
        "short_video_push":         _run_short_video,
        "code_repair":              _run_code_repair,
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
