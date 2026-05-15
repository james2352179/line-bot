import os
import json
import logging
import re
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
from datetime import datetime
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

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

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
histories = {}
scheduler = BackgroundScheduler(timezone="Asia/Taipei")


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
    """每 60 秒輪詢已完成任務，把結果推回給下達指令的 CC 用戶"""
    try:
        rows = supabase.table('pending_tasks').select('*').eq('status', 'done').execute()
        for task in rows.data:
            params = task.get('params') or {}
            reply_to = params.get('reply_to_user_id')
            if not reply_to or params.get('notified'):
                continue
            result = (task.get('result') or '').strip()
            task_name = task.get('task_name', '')
            labels = {'competitor_analysis': '競品分析', 'shopee_push': '蝦皮廣告報表'}
            label = labels.get(task_name, task_name)
            lines = [f"✅ 【{label}】已完成，同步推播至員工群"]
            if result and result not in ('推播完成', ''):
                lines.append('')
                lines.append(result)
            push_to_group(cc_config, reply_to, '\n'.join(lines))
            # 標記已通知，避免重複推播
            supabase.table('pending_tasks').update(
                {'params': {**params, 'notified': True}}
            ).eq('id', task['id']).execute()
            logger.info(f"已回通知 {task_name} → {reply_to[:8]}...")
    except Exception as e:
        logger.error(f"notify_completed_tasks error: {e}")

def load_and_schedule_all():
    rows = supabase.table('bot_schedules').select('*').eq('enabled', True).execute()
    for job in rows.data:
        jid = job['task_name']
        if scheduler.get_job(jid):
            scheduler.remove_job(jid)
        scheduler.add_job(
            make_report_func(jid), 'cron', id=jid,
            day=job['schedule_day'],
            hour=job['schedule_hour'],
            minute=job['schedule_minute']
        )
        logger.info(f"排程載入: {job['display_name']} 每月{job['schedule_day']}號 {job['schedule_hour']:02d}:{job['schedule_minute']:02d}")
    # 任務完成回通知（每 60 秒檢查一次）
    if not scheduler.get_job('notify_completed'):
        scheduler.add_job(notify_completed_tasks, 'interval', id='notify_completed', seconds=60)

def apply_schedule_update(task_name, updates: dict) -> str:
    updates['updated_at'] = datetime.now().isoformat()
    supabase.table('bot_schedules').update(updates).eq('task_name', task_name).execute()
    row = supabase.table('bot_schedules').select('*').eq('task_name', task_name).single().execute()
    job = row.data
    if scheduler.get_job(task_name):
        scheduler.remove_job(task_name)
    if job.get('enabled'):
        scheduler.add_job(
            make_report_func(task_name), 'cron', id=task_name,
            day=job['schedule_day'],
            hour=job['schedule_hour'],
            minute=job['schedule_minute']
        )
    return job['display_name']


# ── 指揮Bot 管理員指令 ────────────────────────────────────────

ADMIN_SYSTEM = """你是J大的私人AI助理，同時是 KT BIKER BOT 的指揮控制器。
你具備完整的語意理解能力，能夠從自然語言中識別意圖，並輸出精確的 JSON 指令。

【輸出規則】
- 若識別到以下任一意圖：輸出純 JSON，不得有其他文字
- 若為一般對話（問候、閒聊、非指令）：用繁體中文自然回答

【指令意圖對應】

A. 啟用任務（含「啟動」「開始」「恢復」「重啟」「打開」+任務名稱）
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":true}}

B. 暫停任務（含「暫停」「停用」「關閉」「停止」+任務名稱）
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":false}}

C. 查詢排程（含「查看」「目前」「列出」「有哪些」「排程狀態」）
   → {"action":"list_schedules"}

D. 修改排程設定（時間、日期、內容）
   → {"action":"update_schedule","task_name":"XXX","updates":{只含要改的欄位}}

E. 推播蝦皮報表（含「蝦皮」「shopee」，無 URL）
   → {"action":"trigger_local","task_name":"shopee_push"}

F. 競品分析（含「競品」「分析」「戰情」，可指定客戶設定檔）
   - 有指定客戶名稱（如「汽美」「汽車美容」「KT」等）：
     → {"action":"trigger_local","task_name":"competitor_analysis","profile":"[客戶名稱關鍵詞]"}
   - 未指定客戶：
     → {"action":"trigger_local","task_name":"competitor_analysis"}
   ✦ 範例：「請執行汽美用品的競品分析傳上來」→ {"action":"trigger_local","task_name":"competitor_analysis","profile":"汽美用品"}
   ✦ 範例：「幫我跑一下競品戰情」→ {"action":"trigger_local","task_name":"competitor_analysis"}

G. 推播含 URL 的訊息（訊息中含 http）
   → {"action":"push_url","url":"https://...","message":"前綴說明（選填）"}

【可用排程任務 task_name】（僅用於 update_schedule）
- biweekly_report：雙週競品分析（含「競品」「雙週」關鍵詞）
- monthly_shopee：每月蝦皮廣告報表（含「蝦皮」「shopee」關鍵詞）

【可用本地工具 task_name】（用於 trigger_local）
- competitor_analysis：競品戰情室分析
- shopee_push：蝦皮廣告報表推播

【語意理解原則】
- 「傳上來」「傳給我看」「我想看」→ 執行工具並通知
- 「傳到群組」「推播到員工群」→ 執行工具並推播
- 「汽美」「汽車美容」「汽美用品」→ profile 關鍵詞為「汽美」
- 未指定客戶時，使用預設設定檔（汽車美容用品業）"""

ADMIN_KEYWORDS = ['設定', '修改', '更新', '排程', '播報', '報表時間', '內容改', '改成', '改到',
                  '查看排程', '目前排程', '幾號發', '幾點發',
                  '暫停', '停用', '啟用', '恢復', '開始', '重啟', '重新啟動',
                  '推播', '立即推', '手動推', '馬上發', '發送報表', '立刻發',
                  '執行工具', '執行分析', '啟動分析', '跑分析', '執行競品', '執行蝦皮',
                  '競品', '分析', '戰情', '蝦皮報表', '傳上來', '傳給我', '傳給我看']

def is_admin_command(text: str) -> bool:
    return any(k in text for k in ADMIN_KEYWORDS)

def handle_admin_command(text: str, user_id: str = None) -> str:
    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=ADMIN_SYSTEM,
        messages=[{"role": "user", "content": text}]
    )
    result = resp.content[0].text.strip()
    try:
        m = re.search(r'\{.*\}', result, re.DOTALL)
        if m:
            cmd = json.loads(m.group())
            if cmd['action'] == 'update_schedule':
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

            elif cmd['action'] == 'list_schedules':
                rows = supabase.table('bot_schedules').select('*').execute()
                lines = ["📋 目前播報排程：\n"]
                for job in rows.data:
                    st = "✅" if job['enabled'] else "⏸ 已暫停"
                    lines.append(f"{st} {job['display_name']}")
                    lines.append(f"   每月 {job['schedule_day']} 號 {job['schedule_hour']:02d}:{job['schedule_minute']:02d}")
                    preview = job['content'].replace('\\n', '\n')[:40].replace('\n', ' ')
                    lines.append(f"   內容預覽：{preview}...")
                return "\n".join(lines)

            elif cmd['action'] == 'manual_push':
                task_name = cmd['task_name']
                row = supabase.table('bot_schedules').select('*').eq('task_name', task_name).single().execute()
                if not row.data:
                    return f"❌ 找不到任務：{task_name}"
                content = (row.data['content']
                           .replace('{date}', datetime.now().strftime("%Y/%m/%d"))
                           .replace('\\n', '\n'))
                push_to_group(kt_config, KT_GROUP_ID, content)
                return f"✅ 已手動推播【{row.data['display_name']}】到員工群"

            elif cmd['action'] == 'push_url':
                url = cmd.get('url', '').strip()
                msg = cmd.get('message', '').strip()
                if not url:
                    return "❌ 沒有偵測到網址"
                content = f"{msg}\n{url}" if msg else url
                push_to_group(kt_config, KT_GROUP_ID, content)
                return f"✅ 已推播連結到員工群"

            elif cmd['action'] == 'trigger_local':
                task_name = cmd.get('task_name', '')
                if not task_name:
                    return "❌ 無法識別要執行的工具"
                profile = cmd.get('profile', '').strip()
                params = {}
                if profile:
                    params['profile'] = profile
                if user_id:
                    params['reply_to_user_id'] = user_id
                supabase.table('pending_tasks').insert({
                    'task_name': task_name,
                    'status': 'pending',
                    'params': params,
                }).execute()
                task_labels = {
                    'competitor_analysis': '競品戰情室分析',
                    'shopee_push': '蝦皮廣告報表推播',
                }
                label = task_labels.get(task_name, task_name)
                profile_hint = f"（{profile}）" if profile else ""
                return f"✅ 已下達指令：【{label}】{profile_hint}\nMac 收到後開始執行，完成後會把結果傳回這裡和員工群。\n（請確保 Mac 已開機）"

    except Exception as e:
        logger.error(f"Admin parse error: {e}, raw: {result}")
    return result


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
        if is_admin_command(text):
            reply = handle_admin_command(text, user_id=user_id)
        else:
            if user_id not in histories:
                histories[user_id] = []
            histories[user_id].append({"role": "user", "content": text})
            histories[user_id] = histories[user_id][-20:]
            resp = claude.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                system="你是J大的私人AI助理，透過LINE與他溝通。回覆請簡潔精準，使用繁體中文。",
                messages=histories[user_id]
            )
            reply = resp.content[0].text
            histories[user_id].append({"role": "assistant", "content": reply})
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
