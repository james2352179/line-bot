import os
import json
import logging
import re
import threading
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent
from linebot.v3.exceptions import InvalidSignatureError
import anthropic
from apscheduler.schedulers.background import BackgroundScheduler
from datetime import datetime, timedelta
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

app = Flask(__name__)

LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']
KT_CHANNEL_SECRET = os.environ['KT_CHANNEL_SECRET']
KT_CHANNEL_ACCESS_TOKEN = os.environ['KT_CHANNEL_ACCESS_TOKEN']
ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']
KT_GROUP_ID = os.environ.get('KT_GROUP_ID', '')

supabase = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])

cc_handler = WebhookHandler(LINE_CHANNEL_SECRET)
cc_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
kt_handler = WebhookHandler(KT_CHANNEL_SECRET)
kt_config = Configuration(access_token=KT_CHANNEL_ACCESS_TOKEN)

# ── 客戶登錄表（新增客戶只需在此加一欄 + Railway 環境變數）────────
CLIENT_REGISTRY = {
    'kt_biker': {
        'display_name': 'KT BIKER',
        'token_config': kt_config,
        'group_id': KT_GROUP_ID,
    },
    # 'client_a': {
    #     'display_name': 'Client A',
    #     'token_config': Configuration(access_token=os.environ.get('CLIENT_A_TOKEN', '')),
    #     'group_id': os.environ.get('CLIENT_A_GROUP_ID', ''),
    # },
}

def _client_cfg(client_id: str) -> dict:
    return CLIENT_REGISTRY.get(client_id, CLIENT_REGISTRY['kt_biker'])

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
MAX_HISTORY_USERS = 50
histories = {}        # user_id → list[{role,content}]，記憶體快取
pending_actions = {}  # user_id → (任務 dict, 建立時間)
PENDING_TTL = timedelta(minutes=10)
pending_repairs = {}  # user_id → (修復 ctx dict, 建立時間)
REPAIR_TTL = timedelta(minutes=30)
scheduler = BackgroundScheduler(timezone="Asia/Taipei")


# ── 對話記憶（持久化 + 記憶體快取）────────────────────────────

def load_history(user_id: str) -> list:
    """從快取讀取；若無快取則從 Supabase 載入最近 30 則"""
    if user_id in histories:
        return histories[user_id]
    try:
        rows = (supabase.table('cc_conversations')
                .select('role,content')
                .eq('user_id', user_id)
                .order('created_at', desc=True)
                .limit(30)
                .execute())
        msgs = [{'role': r['role'], 'content': r['content']}
                for r in reversed(rows.data)]
    except Exception as e:
        logger.warning(f"load_history error: {e}")
        msgs = []
    if len(histories) >= MAX_HISTORY_USERS:
        oldest = next(iter(histories))
        del histories[oldest]
    histories[user_id] = msgs
    return msgs

def save_exchange(user_id: str, user_msg: str, assistant_msg: str):
    """異步寫入 Supabase，不阻塞主流程"""
    def _save():
        try:
            supabase.table('cc_conversations').insert([
                {'user_id': user_id, 'role': 'user',      'content': user_msg},
                {'user_id': user_id, 'role': 'assistant', 'content': assistant_msg},
            ]).execute()
        except Exception as e:
            logger.error(f"save_exchange error: {e}")
    threading.Thread(target=_save, daemon=True).start()


# ── 排程管理 ──────────────────────────────────────────────────

def push_to_group(token_config, group_id, message):
    if not group_id:
        logger.warning("Group ID 尚未設定，略過推播")
        return
    with ApiClient(token_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(to=group_id, messages=[TextMessage(text=message)])
        )

def make_report_func(task_name):
    def send_report():
        row = supabase.table('bot_schedules').select('*').eq('task_name', task_name).single().execute()
        if row.data and row.data.get('enabled'):
            content = (row.data['content']
                       .replace('{date}', datetime.now().strftime("%Y/%m/%d"))
                       .replace('\\n', '\n'))
            push_to_group(kt_config, KT_GROUP_ID, content)
            logger.info(f"已發送: {task_name}")
    return send_report

def notify_completed_tasks():
    """每 60 秒輪詢已完成/失敗任務，把結果推回給下達指令的 CC 用戶，通知後刪除 row"""
    try:
        rows = (supabase.table('pending_tasks').select('*')
                .in_('status', ['done', 'error']).execute())
        for task in rows.data:
            params = task.get('params') or {}
            reply_to = params.get('reply_to_user_id')
            if not reply_to:
                supabase.table('pending_tasks').delete().eq('id', task['id']).execute()
                continue
            status = task.get('status', 'done')
            result = (task.get('result') or '').strip()
            task_name = task.get('task_name', '')
            labels = {
                'competitor_analysis':      '競品分析',
                'shopee_push':              '蝦皮廣告報表',
                'product_perf_push':        '商品表現報表',
                'competitor_status':        '競品分析狀態',
                'single_platform_analysis': '單平台競品分析',
                'latest_platform_report':   '最新競品報告',
                'code_repair':              '程式碼修復',
                'short_video_push':         '蝦皮短影音報表',
            }
            label = labels.get(task_name, task_name)
            if status == 'error':
                lines = [f"❌ 【{label}】執行失敗", '', result or '（無錯誤訊息）',
                         '', '回覆「修復」讓我嘗試自動修復，或「略過」忽略。']
                pending_repairs[reply_to] = ({
                    'task_name': task_name,
                    'error': result or '（無錯誤訊息）',
                    'client': params.get('client', 'kt_biker'),
                }, datetime.now())
            else:
                target = params.get('target', 'both')
                header = f"✅ 【{label}】已完成" + ("，已同步推播至員工群" if target != 'cc_only' else "")
                lines = [header]
                if result and result not in ('推播完成', ''):
                    lines.append('')
                    lines.append(result)
            push_to_group(cc_config, reply_to, '\n'.join(lines))
            supabase.table('pending_tasks').delete().eq('id', task['id']).execute()
            logger.info(f"已回通知並刪除 {task_name}({status}) → {reply_to[:8]}...")
    except Exception as e:
        logger.error(f"notify_completed_tasks error: {e}")

_WEEKDAYS = {'mon','tue','wed','thu','fri','sat','sun'}

def _add_job(jid, func, job: dict):
    """依 schedule_day 格式決定月排程或週排程。"""
    day_val = str(job['schedule_day'])
    hour, minute = job['schedule_hour'], job['schedule_minute']
    if day_val.lower() in _WEEKDAYS:
        scheduler.add_job(func, 'cron', id=jid,
                          day_of_week=day_val.lower(), hour=hour, minute=minute)
    else:
        scheduler.add_job(func, 'cron', id=jid,
                          day=day_val, hour=hour, minute=minute)

def _schedule_label(job: dict) -> str:
    day_val = str(job['schedule_day'])
    if day_val.lower() in _WEEKDAYS:
        names = {'mon':'週一','tue':'週二','wed':'週三','thu':'週四',
                 'fri':'週五','sat':'週六','sun':'週日'}
        return f"每{names.get(day_val.lower(), day_val)}"
    return f"每月{day_val}號"

def load_and_schedule_all():
    rows = supabase.table('bot_schedules').select('*').eq('enabled', True).execute()
    for job in rows.data:
        jid = job['task_name']
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)
        _add_job(jid, make_report_func(jid), job)
        logger.info(f"排程載入: {job['display_name']} {_schedule_label(job)} {job['schedule_hour']:02d}:{job['schedule_minute']:02d}")
    if not scheduler.get_job('notify_completed'):
        scheduler.add_job(notify_completed_tasks, 'interval', id='notify_completed', seconds=60)

def apply_schedule_update(task_name, updates: dict) -> str:
    # 相容處理：CC 可能輸出 hour/minute 分開或合併的 time 字串
    if 'time' in updates:
        t = updates.pop('time')
        if isinstance(t, str) and ':' in t:
            h, m = t.split(':', 1)
            updates.setdefault('schedule_hour', int(h))
            updates.setdefault('schedule_minute', int(m))
    if 'day' in updates:
        updates.setdefault('schedule_day', updates.pop('day'))

    updates['updated_at'] = datetime.now().isoformat()
    # 先嘗試 update；若無此 row（新任務）則 insert
    result = supabase.table('bot_schedules').update(updates).eq('task_name', task_name).execute()
    if not result.data:
        updates['task_name'] = task_name
        updates.setdefault('display_name', task_name)
        updates.setdefault('enabled', True)
        updates.setdefault('schedule_day', 1)
        updates.setdefault('schedule_hour', 9)
        updates.setdefault('schedule_minute', 0)
        updates.setdefault('content', '')
        supabase.table('bot_schedules').insert(updates).execute()
        logger.info(f"新排程任務已建立: {task_name}")
    row = supabase.table('bot_schedules').select('*').eq('task_name', task_name).single().execute()
    job = row.data
    if scheduler.get_job(task_name):
        scheduler.remove_job(task_name)
    if job.get('enabled'):
        _add_job(task_name, make_report_func(task_name), job)
    return job['display_name']


# ── 指揮Bot AI 系統提示 ──────────────────────────────────────

ADMIN_SYSTEM = """你是J大的私人AI助理，同時是 KT BIKER BOT 的指揮控制器。
你具備完整的語意理解能力，同時擁有對話記憶，能記住本次及之前幾次對話的內容和上下文。

【回覆原則】
- 識別到明確的控制指令 → 輸出純 JSON（不含其他文字）
- 一般對話、業務討論、閒聊、問問題 → 繁體中文自然回答，發揮 AI 助理的完整能力
- 你了解 J大 的業務：KT BIKER 機車配件品牌，有員工群組，有蝦皮廣告和競品分析工具

【控制指令規則】

A. 啟用任務（含「啟動」「開始」「恢復」「重啟」+任務名稱）
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":true}}

B. 暫停任務（含「暫停」「停用」「關閉」「停止」+任務名稱）
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":false}}

C. 查詢排程（含「查看」「目前」「列出」「排程狀態」）
   → {"action":"list_schedules"}

D. 修改排程設定
   → {"action":"update_schedule","task_name":"XXX","updates":{只含要改的欄位}}
   欄位名稱對照（務必使用這些名稱，不可自創）：
   - 日期 → "schedule_day": 數字（例：3）
   - 時間 → "schedule_hour": 數字, "schedule_minute": 數字（例："schedule_hour":9,"schedule_minute":30）
   - 啟用狀態 → "enabled": true/false
   - 內容 → "content": "訊息文字"
   - 顯示名稱 → "display_name": "名稱"
   新增不存在的任務也用 update_schedule，系統會自動建立

E. 執行工具（trigger_local）— 判斷 client 與 target：

   【client 判斷】
   - 「KT BIKER」「KT」「機車配件」或未指定客戶 → client:"kt_biker"
   - 未來新增客戶會在此補充對應名稱

   【target 判斷】
   - 「傳給我」「發給我」「我要看」「傳上來」「給我看」「私下」→ target:"cc_only"
   - 「傳到群組」「推播到員工群」「發到XX群」「傳給員工」→ target:"group"
   - 「給我以及員工群組」「同時推播」「都要」「兩個都」→ target:"both"（只輸出一個 JSON）
   - 未說明傳給誰 → 改用 ask_target 先問清楚

   蝦皮廣告報表 → {"action":"trigger_local","task_name":"shopee_push","client":"kt_biker","target":"..."}
   商品表現報表 → {"action":"trigger_local","task_name":"product_perf_push","client":"kt_biker","target":"...","period":"YYYY年MM月（選填，不指定則最新期）"}
     ※ 若使用者說「四月」→ period:"04月"；說「2026年4月」→ period:"2026年04月"；未指定月份 → 省略 period 欄位
   競品分析 → {"action":"trigger_local","task_name":"competitor_analysis","client":"kt_biker","profile":"客戶名（選填）","target":"..."}
   蝦皮短影音報表 → {"action":"trigger_local","task_name":"short_video_push","client":"kt_biker","target":"..."}
   意圖不明 → {"action":"ask_target","task_name":"...","client":"kt_biker","profile":"（若有）"}

F. 推播含 URL → {"action":"push_url","url":"https://...","message":"說明（選填）"}

G. 查詢競品分析狀態（含「跑完了嗎」「分析進行中嗎」「狀態」）
   → {"action":"trigger_local","task_name":"competitor_status","client":"kt_biker","target":"cc_only"}

H. 單平台競品分析（含「只跑」「只分析」+平台名，節省時間）
   平台對應：YouTube/YT → youtube；TikTok/抖音 → tiktok；Facebook/FB → facebook；Instagram/IG → instagram；Threads → threads
   → {"action":"trigger_local","task_name":"single_platform_analysis","client":"kt_biker","platform":"youtube","target":"..."}

I. 取最新報告連結（含「上次的」「最新的」「不重跑」「重新發布」+平台名）
   預設只傳給我（cc_only），不推員工群
   → {"action":"trigger_local","task_name":"latest_platform_report","client":"kt_biker","platform":"youtube","target":"cc_only"}

【可用任務】
排程控制：biweekly_report（競品）、monthly_shopee（蝦皮）
本地工具：competitor_analysis、shopee_push、product_perf_push（商品表現）、short_video_push（短影音）
         competitor_status（查詢狀態）、single_platform_analysis（單平台，需 platform）、latest_platform_report（最新連結，需 platform）

【對話記憶使用原則】
- 若前幾則訊息已討論過某任務，新指令直接引用（如「改成傳到群組」指前一個任務）
- 若上下文能推斷 task_name 或 profile，直接使用，不必再問
- 跨天的對話記憶同樣有效，記得之前討論過的設定和偏好

【輸出格式鐵律】
- 每則回覆只能輸出一個 JSON 物件，絕對禁止輸出兩個或多個 JSON
- 若使用者一句話觸發了多個意圖，優先執行最直接的意圖，其他的用自然語言說明"""


# ── 修復指令觸發 ──────────────────────────────────────────────

def _trigger_code_repair(ctx: dict, user_id: str) -> str:
    params = {
        'client':        ctx.get('client', 'kt_biker'),
        'target':        'cc_only',
        'failed_task':   ctx.get('task_name', ''),
        'error':         ctx.get('error', ''),
        'reply_to_user_id': user_id,
    }
    supabase.table('pending_tasks').insert({
        'task_name': 'code_repair',
        'status':    'pending',
        'params':    params,
    }).execute()
    return ("✅ 已下達修復指令。\n"
            "我會先做 git 備份，再嘗試自動修復。\n"
            "完成後會把修復摘要傳回這裡。\n"
            "（請確保 Mac 已開機）")


# ── 指令執行 ─────────────────────────────────────────────────

def execute_command(cmd: dict, user_id: str = None) -> str:
    action = cmd.get('action', '')

    if action == 'update_schedule':
        display_name = apply_schedule_update(cmd['task_name'], cmd['updates'])
        u = cmd['updates']
        parts = []
        if 'enabled' in u:
            parts.append("狀態：" + ("✅ 已啟用" if u['enabled'] else "⏸ 已暫停"))
        if 'schedule_day' in u:
            parts.append(f"發送日期：每月 {u['schedule_day']} 號")
        if 'schedule_hour' in u:
            parts.append(f"發送時間：{u['schedule_hour']:02d}:{u.get('schedule_minute', 0):02d}")
        if 'content' in u:
            parts.append("播報內容：已更新")
        return f"✅ 【{display_name}】設定完成\n" + "\n".join(parts)

    elif action == 'list_schedules':
        rows = supabase.table('bot_schedules').select('*').execute()
        lines = ["📋 目前播報排程：\n"]
        for job in rows.data:
            st = "✅" if job['enabled'] else "⏸ 已暫停"
            lines.append(f"{st} {job['display_name']}")
            lines.append(f"   每月 {job['schedule_day']} 號 {job['schedule_hour']:02d}:{job['schedule_minute']:02d}")
            preview = job['content'].replace('\\n', '\n')[:40].replace('\n', ' ')
            lines.append(f"   內容預覽：{preview}...")
        return "\n".join(lines)

    elif action == 'manual_push':
        task_name = cmd['task_name']
        client = cmd.get('client', 'kt_biker')
        row = supabase.table('bot_schedules').select('*').eq('task_name', task_name).single().execute()
        if not row.data:
            return f"❌ 找不到任務：{task_name}"
        content = (row.data['content']
                   .replace('{date}', datetime.now().strftime("%Y/%m/%d"))
                   .replace('\\n', '\n'))
        cfg = _client_cfg(client)
        push_to_group(cfg['token_config'], cfg['group_id'], content)
        return f"✅ 已手動推播【{row.data['display_name']}】到{cfg['display_name']}員工群"

    elif action == 'push_url':
        url = cmd.get('url', '').strip()
        msg = cmd.get('message', '').strip()
        client = cmd.get('client', 'kt_biker')
        if not url:
            return "❌ 沒有偵測到網址"
        content = f"{msg}\n{url}" if msg else url
        cfg = _client_cfg(client)
        push_to_group(cfg['token_config'], cfg['group_id'], content)
        return f"✅ 已推播連結到{cfg['display_name']}員工群"

    elif action == 'trigger_local':
        return _do_trigger_local(cmd, user_id)

    elif action == 'ask_target':
        task_name = cmd.get('task_name', '')
        client = cmd.get('client', 'kt_biker')
        platform = cmd.get('platform', '')
        profile = cmd.get('profile', '').strip()
        if user_id:
            pending_actions[user_id] = ({'task_name': task_name, 'client': client, 'platform': platform, 'profile': profile}, datetime.now())
        task_labels = {
            'competitor_analysis':      '競品分析',
            'shopee_push':              '蝦皮廣告報表',
            'product_perf_push':        '商品表現報表',
            'competitor_status':        '競品分析狀態',
            'single_platform_analysis': '單平台競品分析',
            'latest_platform_report':   '最新競品報告',
            'short_video_push':         '蝦皮短影音報表',
        }
        label = task_labels.get(task_name, task_name)
        profile_hint = f"「{profile}」的" if profile else ""
        return f"請問您要把{profile_hint}【{label}】：\n\n1️⃣ 傳給我個人查看\n2️⃣ 推播到員工群組\n\n回覆「給我」或「推播到群組」即可。"

    return "❌ 未知的指令類型"


def _do_trigger_local(cmd: dict, user_id: str = None) -> str:
    task_name = cmd.get('task_name', '')
    if not task_name:
        return "❌ 無法識別要執行的工具"
    client   = cmd.get('client', 'kt_biker')
    platform = cmd.get('platform', '').strip()
    profile  = cmd.get('profile', '').strip()
    period   = cmd.get('period', '').strip()
    target   = cmd.get('target', 'cc_only')

    params = {'client': client, 'target': target}
    if platform:
        params['platform'] = platform
    if profile:
        params['profile'] = profile
    if period:
        params['period'] = period
    if user_id and target in ('cc_only', 'both', 'group'):
        params['reply_to_user_id'] = user_id
    supabase.table('pending_tasks').insert({
        'task_name': task_name,
        'status': 'pending',
        'params': params,
    }).execute()

    cfg = _client_cfg(client)
    task_labels = {
        'competitor_analysis':      '競品戰情室分析',
        'shopee_push':              '蝦皮廣告報表推播',
        'product_perf_push':        '商品表現報表推播',
        'competitor_status':        '競品分析狀態查詢',
        'single_platform_analysis': '單平台競品分析',
        'latest_platform_report':   '最新競品報告',
        'short_video_push':         '蝦皮短影音報表推播',
    }
    label = task_labels.get(task_name, task_name)
    client_hint = f"【{cfg['display_name']}】" if client != 'kt_biker' else ""
    platform_hint = f"（{platform.upper()}）" if platform else ""
    profile_hint = f"（{profile}）" if profile else ""
    hint = f"{platform_hint}{profile_hint}"
    if target == 'both':
        return f"✅ 已下達指令：{client_hint}【{label}】{hint}\n完成後會傳回這裡，並同步推播至員工群。\n（請確保 Mac 已開機）"
    elif target == 'cc_only':
        return f"✅ 已下達指令：{client_hint}【{label}】{hint}\n完成後結果會傳回這裡，不推播員工群。\n（請確保 Mac 已開機）"
    else:
        return f"✅ 已下達指令：{client_hint}【{label}】{hint}\n完成後推播至員工群，同時傳回這裡通知您。\n（請確保 Mac 已開機）"


def resolve_pending_action(pending: dict, response: str, user_id: str) -> str:
    if any(k in response for k in ['給我', '個人', '私下', '我要看', '只給我', '1', '①']):
        target = 'cc_only'
    elif any(k in response for k in ['群組', '員工', '推播', '群', '2', '②']):
        target = 'group'
    else:
        pending_actions[user_id] = (pending, datetime.now())
        return "請回覆「給我個人」或「推播到群組」，我再執行。"
    return _do_trigger_local({**pending, 'target': target}, user_id)


# ── 統一訊息處理（含對話記憶）──────────────────────────────────

def process_cc_message(text: str, user_id: str) -> str:
    """所有訊息統一入口：載入歷史 → Claude → 執行指令或回覆 → 存回記憶"""
    history = load_history(user_id)
    send_history = history[-20:] + [{"role": "user", "content": text}]

    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=ADMIN_SYSTEM,
        messages=send_history,
    )
    raw = resp.content[0].text.strip()

    # 嘗試解析 JSON 指令：掃描所有 { 起點，取第一個合法 JSON 執行
    reply = raw
    valid_actions = ('update_schedule', 'list_schedules', 'manual_push',
                     'push_url', 'trigger_local', 'ask_target')
    decoder = json.JSONDecoder()
    for match in re.finditer(r'\{', raw):
        try:
            cmd, _ = decoder.raw_decode(raw, match.start())
            if isinstance(cmd, dict) and cmd.get('action') in valid_actions:
                reply = execute_command(cmd, user_id)
                break
        except (json.JSONDecodeError, ValueError):
            continue
        except Exception as e:
            logger.error(f"process_cc_message execute error: {e}, raw: {raw}")
            break

    # API 成功後才更新快取，避免孤立 user 訊息導致對話卡死
    history.append({"role": "user", "content": text})
    history.append({"role": "assistant", "content": reply})
    histories[user_id] = history
    save_exchange(user_id, text, reply)
    return reply


# ── 指揮Bot Webhook ───────────────────────────────────────────

@app.route('/webhook', methods=['POST'])
def webhook_cc():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        cc_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@cc_handler.add(MessageEvent, message=TextMessageContent)
def on_cc_message(event):
    user_id = event.source.user_id
    text = event.message.text
    reply = "（系統錯誤，請稍後再試）"

    try:
        # 清除超時的 pending_actions / pending_repairs
        expired = [uid for uid, (_, ts) in pending_actions.items()
                   if datetime.now() - ts > PENDING_TTL]
        for uid in expired:
            del pending_actions[uid]
        expired_r = [uid for uid, (_, ts) in pending_repairs.items()
                     if datetime.now() - ts > REPAIR_TTL]
        for uid in expired_r:
            del pending_repairs[uid]

        if user_id in pending_repairs:
            # 修復確認流程
            repair_ctx, _ = pending_repairs[user_id]
            if any(k in text for k in ['修復', '修', '幫我修', '去修', '確認', '好']):
                pending_repairs.pop(user_id)
                reply = _trigger_code_repair(repair_ctx, user_id)
                history = load_history(user_id)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                histories[user_id] = history
                save_exchange(user_id, text, reply)
            elif any(k in text for k in ['略過', '不用', '取消', '跳過', '沒關係']):
                pending_repairs.pop(user_id)
                reply = "好的，略過此次修復。"
                history = load_history(user_id)
                history.append({"role": "user", "content": text})
                history.append({"role": "assistant", "content": reply})
                histories[user_id] = history
                save_exchange(user_id, text, reply)
            else:
                reply = process_cc_message(text, user_id)
        elif user_id in pending_actions:
            # ask_target 確認流程
            pending, _ = pending_actions.pop(user_id)
            reply = resolve_pending_action(pending, text, user_id)
            history = load_history(user_id)
            history.append({"role": "user", "content": text})
            history.append({"role": "assistant", "content": reply})
            histories[user_id] = history
            save_exchange(user_id, text, reply)
        else:
            reply = process_cc_message(text, user_id)
    except Exception as e:
        logger.error(f"on_cc_message error: {e}", exc_info=True)

    if len(reply) > 4900:
        reply = reply[:4900] + "\n\n（訊息過長，已截斷）"

    with ApiClient(cc_config) as api_client:
        MessagingApi(api_client).reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=reply)]
            )
        )


# ── KT BIKER Webhook ─────────────────────────────────────────

@app.route('/webhook/ktbiker', methods=['POST'])
def webhook_kt():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        kt_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@kt_handler.add(JoinEvent)
def on_kt_join(event):
    if hasattr(event.source, 'group_id'):
        logger.info(f"[KT BIKER] 加入群組 ID: {event.source.group_id}")

@kt_handler.add(MessageEvent, message=TextMessageContent)
def on_kt_message(event):
    if hasattr(event.source, 'group_id'):
        logger.info(f"[KT BIKER] 群組訊息 ID: {event.source.group_id}")


# ── 啟動 ─────────────────────────────────────────────────────

load_and_schedule_all()
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
