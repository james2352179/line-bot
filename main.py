import os
import logging
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

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)

# 指揮Bot (CC)
LINE_CHANNEL_SECRET = os.environ['LINE_CHANNEL_SECRET']
LINE_CHANNEL_ACCESS_TOKEN = os.environ['LINE_CHANNEL_ACCESS_TOKEN']

# KT BIKER 行銷策略bot
KT_CHANNEL_SECRET = os.environ['KT_CHANNEL_SECRET']
KT_CHANNEL_ACCESS_TOKEN = os.environ['KT_CHANNEL_ACCESS_TOKEN']

ANTHROPIC_API_KEY = os.environ['ANTHROPIC_API_KEY']

# 員工群組 ID（bot 加入群組後從 log 取得再填入）
KT_GROUP_ID = os.environ.get('KT_GROUP_ID', '')

cc_handler = WebhookHandler(LINE_CHANNEL_SECRET)
cc_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)

kt_handler = WebhookHandler(KT_CHANNEL_SECRET)
kt_config = Configuration(access_token=KT_CHANNEL_ACCESS_TOKEN)

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
histories = {}


# ── 指揮Bot ──────────────────────────────────────────────────

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

    if user_id not in histories:
        histories[user_id] = []

    histories[user_id].append({"role": "user", "content": text})
    histories[user_id] = histories[user_id][-20:]

    resp = claude.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1500,
        system=(
            "你是J大的私人AI助理，透過LINE與他溝通。"
            "回覆請簡潔精準，使用繁體中文。"
            "可以協助分析、整理資料、規劃任務、撰寫文案等工作。"
        ),
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


# ── KT BIKER 行銷策略bot ──────────────────────────────────────

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
        logger.info(f"[KT BIKER] 已加入群組 Group ID: {event.source.group_id}")

@kt_handler.add(MessageEvent, message=TextMessageContent)
def on_kt_message(event):
    if hasattr(event.source, 'group_id'):
        logger.info(f"[KT BIKER] 收到群組訊息 Group ID: {event.source.group_id}")


# ── 推播函式 ──────────────────────────────────────────────────

def push_to_group(token_config, group_id, message):
    if not group_id:
        logger.warning("Group ID 尚未設定，略過推播")
        return
    with ApiClient(token_config) as api_client:
        MessagingApi(api_client).push_message(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=message)]
            )
        )

def send_biweekly_report():
    now = datetime.now().strftime("%Y/%m/%d")
    message = f"📊 【雙週競品分析報表】{now}\n\n（報表內容待串接競品戰情室資料）"
    push_to_group(kt_config, KT_GROUP_ID, message)
    logger.info("雙週競品報表已發送")

def send_monthly_shopee_report():
    now = datetime.now().strftime("%Y/%m")
    message = f"🛒 【{now} 蝦皮廣告數據分析】\n\n（報表內容待串接蝦皮廣告資料）"
    push_to_group(kt_config, KT_GROUP_ID, message)
    logger.info("每月蝦皮報表已發送")


# ── 排程 ──────────────────────────────────────────────────────
# 每月1日和15日早上9:00 發競品報表
# 每月1日早上9:30 發蝦皮廣告報表
scheduler = BackgroundScheduler(timezone="Asia/Taipei")
scheduler.add_job(send_biweekly_report, 'cron', day='1,15', hour=9, minute=0)
scheduler.add_job(send_monthly_shopee_report, 'cron', day='1', hour=9, minute=30)
scheduler.start()


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
