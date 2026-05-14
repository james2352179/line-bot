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

ADMIN_SYSTEM = """你是J大的私人AI助理，同時你是 KT BIKER BOT（行銷策略bot）的排程控制器。
你可以直接啟用、暫停、修改 KT BIKER BOT 的播報排程。

【指令對應規則】—— 優先判斷，不要回一般對話：

1. 啟動 / 開始 / 啟用 / 恢復 + 任務名稱
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":true}}

2. 暫停 / 停用 / 關閉 + 任務名稱
   → {"action":"update_schedule","task_name":"XXX","updates":{"enabled":false}}

3. 查詢 / 目前 / 列出 + 排程
   → {"action":"list_schedules"}

4. 修改時間/日期/內容
   → {"action":"update_schedule","task_name":"XXX","updates":{...只含要改的欄位...}}

可用任務（task_name）：
- biweekly_report：雙週競品分析報表（含「競品」「雙週」即為此任務）
- monthly_shopee：每月蝦皮廣告報表（含「蝦皮」「shopee」即為此任務）

輸出純 JSON，不要有其他文字。
若非排程相關指令，用繁體中文正常回答。"""

ADMIN_KEYWORDS = ['設定', '修改', '更新', '排程', '播報', '報表時間', '內容改', '改成', '改到',
                  '查看排程', '目前排程', '幾號發', '幾點發',
                  '暫停', '停用', '啟用', '恢復', '開始', '重啟', '重新啟動']

def is_admin_command(text: str) -> bool:
    return any(k in text for k in ADMIN_KEYWORDS)

def handle_admin_command(text: str) -> str:
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

    if is_admin_command(text):
        reply = handle_admin_command(text)
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
