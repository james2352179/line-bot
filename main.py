import os
import json
import logging
import re
import threading
import urllib.parse
import requests
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, TextMessage, PushMessageRequest
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, JoinEvent
from linebot.v3.exceptions import InvalidSignatureError
import anthropic
import googlemaps
import flood_api  # 路線淹水雷達核心（民生公共物聯網 SensorThings 淹水感測器）
import tdx_api    # TDX 即時路況封路/管制（路名比對）
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
FAMILY_GROUP_ID = os.environ.get('FAMILY_GROUP_ID', '')

supabase = create_client(os.environ['SUPABASE_URL'], os.environ['SUPABASE_KEY'])

cc_handler = WebhookHandler(LINE_CHANNEL_SECRET)
cc_config = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
kt_handler = WebhookHandler(KT_CHANNEL_SECRET)
kt_config = Configuration(access_token=KT_CHANNEL_ACCESS_TOKEN)
family_config = Configuration(access_token=os.environ.get('FAMILY_CHANNEL_ACCESS_TOKEN', ''))
family_handler = WebhookHandler(os.environ.get('FAMILY_CHANNEL_SECRET', ''))

# ── 客戶登錄表（新增客戶只需在此加一欄 + Railway 環境變數）────────
CLIENT_REGISTRY = {
    'kt_biker': {
        'display_name': 'KT BIKER',
        'token_config': kt_config,
        'group_id': KT_GROUP_ID,
    },
    'family': {
        'display_name': '家族',
        'token_config': family_config,
        'group_id': FAMILY_GROUP_ID,
    },
}

def _client_cfg(client_id: str) -> dict:
    return CLIENT_REGISTRY.get(client_id, CLIENT_REGISTRY['kt_biker'])

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
gmaps = googlemaps.Client(key=os.environ.get('GOOGLE_MAPS_API_KEY', ''))
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


# ── 家族功能 ──────────────────────────────────────────────────

_CITY_COORDS = {
    '台北': (25.0330, 121.5654), '臺北': (25.0330, 121.5654),
    '新北': (25.0170, 121.4627), '基隆': (25.1276, 121.7392),
    '桃園': (24.9936, 121.3010), '新竹': (24.8138, 120.9675),
    '苗栗': (24.5602, 120.8214), '台中': (24.1477, 120.6736),
    '臺中': (24.1477, 120.6736), '彰化': (24.0518, 120.5161),
    '南投': (23.9609, 120.9718), '雲林': (23.7092, 120.4313),
    '嘉義': (23.4801, 120.4491), '台南': (22.9999, 120.2269),
    '臺南': (22.9999, 120.2269), '高雄': (22.6273, 120.3014),
    '屏東': (22.5519, 120.5487), '宜蘭': (24.7021, 121.7378),
    '花蓮': (23.9871, 121.6015), '台東': (22.7972, 121.0717),
    '臺東': (22.7972, 121.0717), '澎湖': (23.5711, 119.5793),
    '金門': (24.4493, 118.3767),
}
_WMO_CODE = {
    0:'☀️ 晴天', 1:'🌤 大致晴朗', 2:'⛅ 部分多雲', 3:'☁️ 多雲',
    45:'🌫 霧', 48:'🌫 霧',
    51:'🌦 毛毛雨', 53:'🌦 毛毛雨', 55:'🌧 毛毛雨',
    61:'🌧 小雨', 63:'🌧 中雨', 65:'🌧 大雨',
    71:'🌨 小雪', 73:'🌨 中雪', 75:'❄️ 大雪',
    80:'🌦 陣雨', 81:'🌧 陣雨', 82:'⛈ 大陣雨',
    95:'⛈ 雷暴', 96:'⛈ 雷暴', 99:'⛈ 強雷暴',
}

def query_weather(city: str, day_offset: int = 0) -> dict | None:
    """回傳天氣資料 dict；day_offset=0今天，1明天，2後天"""
    import urllib.request
    lat, lon = None, None
    city_name = city
    for key, coords in _CITY_COORDS.items():
        if key in city:
            lat, lon = coords
            city_name = key
            break
    if lat is None:
        return None
    try:
        url = (f"https://api.open-meteo.com/v1/forecast"
               f"?latitude={lat}&longitude={lon}"
               f"&current=temperature_2m,apparent_temperature,weathercode,windspeed_10m,relativehumidity_2m"
               f"&daily=weathercode,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
               f"&timezone=Asia%2FTaipei&forecast_days=3")
        with urllib.request.urlopen(url, timeout=5) as r:
            import json as _json
            data = _json.loads(r.read())
        daily = data['daily']
        idx = min(day_offset, len(daily['weathercode']) - 1)
        code = daily['weathercode'][idx]
        rain_prob = daily['precipitation_probability_max'][idx] or 0
        result = {
            'city': city_name,
            'day_offset': day_offset,
            'desc': _WMO_CODE.get(code, '未知'),
            'max_temp': daily['temperature_2m_max'][idx],
            'min_temp': daily['temperature_2m_min'][idx],
            'rain_prob': rain_prob,
        }
        if day_offset == 0:
            cur = data['current']
            result.update({
                'current_temp': cur['temperature_2m'],
                'feels_like': cur['apparent_temperature'],
                'humidity': cur['relativehumidity_2m'],
            })
        return result
    except Exception as e:
        logger.error(f"query_weather error: {e}")
        return None

def format_weather(w: dict, question: str = '') -> str:
    """把天氣 dict 格式化，若有 question 就讓 Claude 加上情境建議"""
    day_label = ['今天', '明天', '後天'][min(w['day_offset'], 2)]
    lines = [f"🌍 {w['city']}市 {day_label}天氣",
             w['desc'],
             f"🔺最高 {w['max_temp']}°C　🔻最低 {w['min_temp']}°C",
             f"🌂 降雨機率：{w['rain_prob']}%"]
    if 'current_temp' in w:
        lines.insert(2, f"🌡 現在 {w['current_temp']}°C（體感 {w['feels_like']}°C）")
        lines.append(f"💧 濕度：{w['humidity']}%")
    weather_summary = '\n'.join(lines)

    # 若問題含活動建議類字眼，讓 Claude 加一句話點評
    if question and re.search(r'適合|要不要|可以|好嗎|怎樣|建議|出門|帶傘|曬|熱|涼|海邊|出遊|爬山|踏青|外出|騎車|打球|烤肉|游泳|戶外', question):
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=80,
                system="你是家族助理內莉，根據天氣資料給出一句口語化的活動建議，繁體中文，20字內。",
                messages=[{"role": "user", "content": f"天氣：{weather_summary}\n問題：{question}"}])
            advice = resp.content[0].text.strip()
            weather_summary += f"\n\n💬 {advice}"
        except Exception:
            pass
    return weather_summary

# ── 潮汐查詢 ──────────────────────────────────────────────────
_CWA_GUEST_KEY = 'rdec-key-123-45678-011121314'
_TIDE_CITY_MAP = {
    '花蓮': '10015050', '台東': '10015050', '臺東': '10015050',
    '宜蘭': 'I02200',
    '基隆': 'I05100', '台北': 'I05100', '臺北': 'I05100',
    '新北': 'I05100', '桃園': 'I05100', '新竹': 'I05100',
    '台南': '10013220', '臺南': '10013220',
    '高雄': '10013220', '屏東': '10013220', '澎湖': '10013220',
    '嘉義': '10013220', '雲林': '10013220',
    '墾丁': 'N01100', '恆春': 'N01100',
}
_TIDE_STATION_DISPLAY = {
    '10015050': '花蓮吉安', 'I02200': '梗枋漁港（宜蘭）',
    'I05100': '下山漁港（基隆）', 'N01100': '船帆石（恆春）',
    'N00900': '核三廠附近（恆春）', '10013220': '小琉球（屏東）',
}
_tide_cache: dict = {}

def query_tide(city: str, day_offset: int = 0) -> dict | None:
    import urllib.request, json as _json
    from datetime import datetime, timedelta
    # 找最近站點
    station_id = None
    for key, sid in _TIDE_CITY_MAP.items():
        if key in city:
            station_id = sid
            break
    if station_id is None:
        station_id = '10013220'  # 預設小琉球（台南附近）
    target_date = (datetime.now() + timedelta(days=day_offset)).strftime('%Y-%m-%d')
    cache_key = f"{station_id}_{target_date}"
    if cache_key in _tide_cache:
        return _tide_cache[cache_key]
    try:
        cwa_key = os.environ.get('CWA_API_KEY', _CWA_GUEST_KEY)
        url = (f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-A0021-001"
               f"?Authorization={cwa_key}&format=JSON")
        with urllib.request.urlopen(url, timeout=8) as r:
            data = _json.loads(r.read())
        forecasts = data['result']['records']['TideForecasts']
        for item in forecasts:
            loc = item['Location']
            if loc['LocationId'] != station_id:
                continue
            for daily in loc['TimePeriods']['Daily']:
                if daily['Date'] != target_date:
                    continue
                tides = []
                for t in daily.get('Time', []):
                    dt = t.get('DateTime', '')
                    tides.append({
                        'time': dt[11:16] if len(dt) >= 16 else dt,
                        'type': t.get('Tide', ''),
                        'cm': t.get('TideHeights', {}).get('AboveTWVD', ''),
                    })
                result = {
                    'station': _TIDE_STATION_DISPLAY.get(station_id, station_id),
                    'city': city, 'date': target_date, 'day_offset': day_offset,
                    'lunar': daily.get('LunarDate', '')[5:],  # "04-11"
                    'range': daily.get('TideRange', ''),
                    'tides': tides,
                }
                _tide_cache[cache_key] = result
                return result
        return None
    except Exception as e:
        logger.error(f"query_tide error: {e}")
        return None

def format_tide_advice(tide: dict, activity: str, question: str) -> str:
    day_label = ['今天', '明天', '後天'][min(tide['day_offset'], 2)]
    range_label = {'大': '🌊 大潮', '中': '🌊 中潮', '小': '🌊 小潮'}.get(tide['range'], tide['range'])
    lines = [
        f"🌊 {tide['city']} {day_label}潮汐（參考{tide['station']}）",
        f"農曆 {tide['lunar']} ｜ {range_label}",
        "",
    ]
    for t in tide['tides']:
        icon = '↑' if t['type'] == '滿潮' else '↓'
        cm_str = f"（{t['cm']}cm）" if t['cm'] else ''
        lines.append(f"  {t['time']} {icon}{t['type']}{cm_str}")
    tide_text = '\n'.join(lines)
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=100,
            system="你是家族助理內莉。根據潮汐資料，針對指定活動給出最佳時間與注意事項，繁體中文口語化，40字內。",
            messages=[{"role": "user", "content": f"活動：{activity}\n問題：{question}\n{tide_text}"}])
        tide_text += f"\n\n💬 {resp.content[0].text.strip()}"
    except Exception:
        pass
    return tide_text

def query_route(origin: str, destination: str) -> str:
    try:
        result = gmaps.directions(origin, destination, mode="driving", language="zh-TW")
        if not result:
            return f"找不到從「{origin}」到「{destination}」的路線，請確認地名是否正確。"
        leg = result[0]['legs'][0]
        distance = leg['distance']['text']
        duration = leg['duration']['text']
        o_name = leg['start_address'].split(',')[0]
        d_name = leg['end_address'].split(',')[0]
        return (f"🚗 {o_name} → {d_name}\n"
                f"距離：{distance}\n"
                f"開車時間：{duration}\n"
                f"（資料來源：Google Maps）")
    except Exception as e:
        logger.error(f"query_route error: {e}")
        return "路線查詢失敗，請稍後再試 😅"

# Haiku 偶爾會在 JSON 前後夾說明文字（即使要求純 JSON），單純去 ``` 擋不住 → json.loads 拋例外。
# 一律抓出第一個完整 {...} 區塊再解析，容忍前後散文。
_JSON_ONLY_SYSTEM = "你是嚴格的解析器。只能輸出 JSON 或單一個 null，禁止任何說明、前言、註解或 markdown 文字。"


def _extract_json_obj(raw: str):
    """從 LLM 回應抽出第一個 JSON 物件；找不到或為 null 回傳 None。"""
    raw = re.sub(r'^```(?:json)?\s*|\s*```$', '', raw.strip()).strip()
    if not raw or raw.lower() == 'null':
        return None
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return None
    return json.loads(m.group(0))


def parse_reminder_request(question: str) -> dict | None:
    """用 Claude Haiku 解析提醒請求，回傳 {'target_at': ISO8601, 'message': str} 或 None"""
    from datetime import timezone, timedelta as _td
    _now = datetime.now(timezone(_td(hours=8)))
    _weekday = '一二三四五六日'[_now.weekday()]
    now_str = _now.strftime('%Y-%m-%d %H:%M') + f'（星期{_weekday}）'
    prompt = (f"現在時間是 {now_str}（台灣時間 UTC+8）。\n"
              f"使用者說：「{question}」\n\n"
              f"請判斷這是否是一個提醒設定請求（例如：明天早上9點提醒我刮鬍子、1小時後叫我開會）。\n\n"
              f"時間判讀規則（重要）：\n"
              f"1. 若使用者沒明確講「上午/下午/早上/晚上」也沒講「明天/某月某日」，"
              f"一律解讀成「從現在算起最近的下一次」，也就是今天還沒到就排今天，今天已過才排明天。\n"
              f"   例：現在 20:56，使用者說「9:30」或「等一下9:30」→ 今天 21:30（不是隔天早上）。\n"
              f"   例：現在 20:56，使用者說「7點」→ 今天的時間已過，排明天 07:00。\n"
              f"2.「等一下/待會/等等/晚點」代表今天接下來的時間，絕對不要跳到隔天。\n"
              f"3. 只有使用者明確講「明天」「後天」「X月X日」「早上/上午」時才照字面排。\n\n"
              f"如果是提醒請求，輸出純 JSON：{{\"target_at\": \"YYYY-MM-DDTHH:MM:00+08:00\", \"message\": \"提醒事項\"}}\n"
              f"如果不是，只輸出：null")
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=160,
            system=_JSON_ONLY_SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        result = _extract_json_obj(resp.content[0].text)
        if isinstance(result, dict) and 'target_at' in result and 'message' in result:
            return result
        return None
    except Exception as e:
        logger.error(f"parse_reminder_request error: {e}")
        return None


def parse_reminder_cancel(question: str) -> str | None:
    """回傳要取消的事項關鍵字、'__all__'（全部），或 None（非取消請求）"""
    prompt = (f"使用者說：「{question}」\n\n"
              f"這是否是取消/刪除提醒的請求？（例如：取消睡覺的提醒、不用提醒我喝水、刪掉提醒）\n"
              f"如果是，輸出要取消的事項關鍵字（純文字，例如：睡覺、喝水）\n"
              f"如果要取消全部提醒，輸出：__all__\n"
              f"如果不是取消請求，只輸出：null")
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=30,
            messages=[{"role": "user", "content": prompt}])
        raw = resp.content[0].text.strip()
        return None if raw.lower() == 'null' or not raw else raw
    except Exception as e:
        logger.error(f"parse_reminder_cancel error: {e}")
        return None


def parse_reminder_modify(question: str) -> dict | None:
    """回傳 {'keyword': '事項關鍵字', 'target_at': 'ISO8601'} 或 None"""
    from datetime import timezone, timedelta as _td
    _now = datetime.now(timezone(_td(hours=8)))
    _weekday = '一二三四五六日'[_now.weekday()]
    now_str = _now.strftime('%Y-%m-%d %H:%M') + f'（星期{_weekday}）'
    prompt = (f"現在時間是 {now_str}（台灣時間 UTC+8）。\n"
              f"使用者說：「{question}」\n\n"
              f"這是否是修改提醒時間的請求？（例如：把睡覺提醒改到2點、把喝水的提醒延後30分鐘）\n\n"
              f"時間判讀規則（重要）：若使用者沒明確講「上午/下午/早上/晚上」也沒講「明天/某月某日」，"
              f"一律解讀成「從現在算起最近的下一次」——今天還沒到就排今天，今天已過才排明天；"
              f"「等一下/待會/晚點」一律是今天接下來的時間，不要跳到隔天。\n\n"
              f"如果是，輸出純 JSON：{{\"keyword\": \"事項關鍵字\", \"target_at\": \"YYYY-MM-DDTHH:MM:00+08:00\"}}\n"
              f"如果不是，只輸出：null")
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001", max_tokens=120,
            system=_JSON_ONLY_SYSTEM,
            messages=[{"role": "user", "content": prompt}])
        result = _extract_json_obj(resp.content[0].text)
        if isinstance(result, dict) and 'keyword' in result and 'target_at' in result:
            return result
        return None
    except Exception as e:
        logger.error(f"parse_reminder_modify error: {e}")
        return None


def check_family_reminders():
    """每分鐘掃描到期的提醒並推播"""
    try:
        from datetime import timezone, timedelta as td
        now_iso = datetime.now(timezone(td(hours=8))).isoformat()
        rows = (supabase.table('family_reminders')
                .select('*').lte('target_at', now_iso).eq('sent', False).execute())
        for r in rows.data:
            try:
                msg = f"⏰ 提醒你：{r['message']}"
                if r['push_type'] == 'group':
                    push_to_group(family_config, FAMILY_GROUP_ID, msg)
                else:
                    with ApiClient(family_config) as api_client:
                        MessagingApi(api_client).push_message(
                            PushMessageRequest(to=r['push_target'], messages=[TextMessage(text=msg)]))
                supabase.table('family_reminders').update({'sent': True}).eq('id', r['id']).execute()
                logger.info(f"[家族提醒] 已推播：{r['message']}")
            except Exception as e:
                logger.error(f"check_family_reminders push error id={r.get('id')}: {e}")
    except Exception as e:
        logger.error(f"check_family_reminders error: {e}")


def check_family_birthdays():
    """每天早上 8 點：檢查明天是否有家族成員生日，推播提醒"""
    tomorrow = datetime.now() + timedelta(days=1)
    try:
        rows = (supabase.table('family_birthdays').select('*')
                .eq('month', tomorrow.month).eq('day', tomorrow.day).execute())
    except Exception as e:
        logger.error(f"check_family_birthdays error: {e}")
        return
    for person in rows.data:
        note = f"（{person['note']}）" if person.get('note') else ''
        prompt = f"明天是家族成員「{person['name']}」的生日{note}，請用繁體中文寫一則溫暖簡短的LINE生日提醒，給整個家族群看的，20~40字，不要加emoji以外的特殊符號。"
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=150,
                messages=[{"role": "user", "content": prompt}])
            msg = f"🎂 明天是 {person['name']} 的生日！\n\n{resp.content[0].text}"
        except Exception:
            msg = f"🎂 提醒：明天是 {person['name']} 的生日！記得祝賀 🎉"
        push_to_group(family_config, FAMILY_GROUP_ID, msg)
        logger.info(f"[家族] 生日提醒已推播：{person['name']}")

def _parse_ai_digest_hour(text: str) -> int | None:
    afternoon = bool(re.search(r'下午|傍晚|晚上|夜|pm', text, re.IGNORECASE))
    m = re.search(r'(\d{1,2})\s*[點:時]', text)
    if m:
        h = int(m.group(1))
        if afternoon and h < 12:
            h += 12
        if 0 <= h <= 23:
            return h
    return None

def _parse_ai_digest_weekday(text: str) -> str:
    for pattern, key in [
        (r'週二|星期二|周二', 'tue'), (r'週三|星期三|周三', 'wed'),
        (r'週四|星期四|周四', 'thu'), (r'週五|星期五|周五', 'fri'),
        (r'週六|星期六|周六', 'sat'), (r'週日|星期日|周日|週天|星期天', 'sun'),
        (r'週一|星期一|周一', 'mon'),
    ]:
        if re.search(pattern, text):
            return key
    return 'mon'

def handle_ai_digest_control(question: str) -> str:
    """解析 AI 日報控制指令，更新 Supabase ai_digest_config"""
    try:
        # 暫停
        if re.search(r'暫停|停止|停用|關閉|不.*發|取消.*日報|不要.*日報', question):
            supabase.table('ai_digest_config').upsert({'id': 1, 'enabled': False}).execute()
            return "✅ AI 日報已暫停，說「啟動AI日報」可恢復"

        # 啟動/恢復
        if re.search(r'啟動|開啟|恢復|重啟|啟用|開始.*日報', question):
            supabase.table('ai_digest_config').upsert({'id': 1, 'enabled': True}).execute()
            row = supabase.table('ai_digest_config').select('*').eq('id', 1).single().execute()
            cfg = row.data or {}
            mode = cfg.get('mode', 'daily')
            hour = cfg.get('hour', 9)
            if mode == 'weekly':
                wd = _WEEKDAY_ZH.get(cfg.get('weekday', 'mon'), '一')
                return f"✅ AI 日報已啟動，每週{wd} {hour:02d}:00 發送"
            return f"✅ AI 日報已啟動，每天 {hour:02d}:00 發送"

        # 改成週報
        if re.search(r'週報|每週|每星期|weekly', question, re.IGNORECASE):
            weekday = _parse_ai_digest_weekday(question)
            hour_val = _parse_ai_digest_hour(question)
            updates = {'id': 1, 'mode': 'weekly', 'weekday': weekday}
            if hour_val is not None:
                updates['hour'] = hour_val
            supabase.table('ai_digest_config').upsert(updates).execute()
            row = supabase.table('ai_digest_config').select('hour').eq('id', 1).single().execute()
            h = (row.data or {}).get('hour', 9)
            wd = _WEEKDAY_ZH.get(weekday, '一')
            return f"✅ 已改成每週{wd} {h:02d}:00 發送週報"

        # 改回日報
        if re.search(r'改回.*日報|改.*每天|每天.*發|日報.*每天', question):
            supabase.table('ai_digest_config').upsert({'id': 1, 'mode': 'daily'}).execute()
            row = supabase.table('ai_digest_config').select('hour').eq('id', 1).single().execute()
            h = (row.data or {}).get('hour', 9)
            return f"✅ 已改回每天 {h:02d}:00 發送日報"

        # 修改時間
        if re.search(r'[改調][到成].*[點時]|時間.*[改調]|[點時].*發送|發送.*[點時]', question):
            hour_val = _parse_ai_digest_hour(question)
            if hour_val is not None:
                supabase.table('ai_digest_config').upsert({'id': 1, 'hour': hour_val}).execute()
                row = supabase.table('ai_digest_config').select('mode,weekday').eq('id', 1).single().execute()
                cfg = row.data or {}
                if cfg.get('mode') == 'weekly':
                    wd = _WEEKDAY_ZH.get(cfg.get('weekday', 'mon'), '一')
                    return f"✅ AI 日報時間已改成每週{wd} {hour_val:02d}:00"
                return f"✅ AI 日報時間已改成每天 {hour_val:02d}:00"

        # 查看狀態
        row = supabase.table('ai_digest_config').select('*').eq('id', 1).single().execute()
        if row.data:
            cfg = row.data
            enabled_label = "✅ 啟用中" if cfg.get('enabled', True) else "⏸ 已暫停"
            mode = cfg.get('mode', 'daily')
            h = cfg.get('hour', 9)
            if mode == 'weekly':
                wd = _WEEKDAY_ZH.get(cfg.get('weekday', 'mon'), '一')
                freq = f"每週{wd}（週報）"
            else:
                freq = "每天（日報）"
            return f"📰 AI 日報狀態\n{enabled_label}\n頻率：{freq}\n時間：{h:02d}:00"
        return "📰 目前沒有 AI 日報設定"
    except Exception as e:
        logger.error(f"handle_ai_digest_control error: {e}")
        return "AI 日報設定失敗，請稍後再試 😢"


def _get_active_vote():
    try:
        r = (supabase.table('family_votes').select('*')
             .eq('status', 'active').order('created_at', desc=True).limit(1).execute())
        return r.data[0] if r.data else None
    except Exception:
        return None

def _format_vote_msg(vote):
    opts = vote['options']
    lines = [f"📊 {vote['question']}\n"]
    for i, opt in enumerate(opts, 1):
        lines.append(f"{i}️⃣ {opt}")
    lines.append("\n請回覆數字投票（例：1）")
    return "\n".join(lines)

def _format_vote_result(vote):
    opts = vote['options']
    votes = vote.get('votes') or {}
    counts = {str(i): 0 for i in range(1, len(opts)+1)}
    for choice in votes.values():
        key = str(choice)
        if key in counts:
            counts[key] += 1
    total = sum(counts.values())
    lines = [f"📊 投票結果：{vote['question']}\n"]
    for i, opt in enumerate(opts, 1):
        c = counts[str(i)]
        bar = "█" * c + "░" * (total - c) if total > 0 else ""
        lines.append(f"{i}️⃣ {opt}：{c} 票 {bar}")
    lines.append(f"\n共 {total} 人投票")
    return "\n".join(lines)


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
            try:
                params = task.get('params') or {}
                reply_to = params.get('reply_to_user_id')
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
                if reply_to:
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
                    msg = '\n'.join(lines)
                    if len(msg) > 4900:
                        msg = msg[:4900] + '\n…（訊息過長已截斷）'
                    push_to_group(cc_config, reply_to, msg)
                    logger.info(f"已回通知 {task_name}({status}) → {reply_to[:8]}...")
            except Exception as task_err:
                logger.error(f"notify task {task.get('id')} ({task.get('task_name')}) error: {task_err}")
            finally:
                supabase.table('pending_tasks').delete().eq('id', task['id']).execute()
                logger.info(f"已刪除 pending_task id={task.get('id')}")
    except Exception as e:
        logger.error(f"notify_completed_tasks error: {e}")

_WEEKDAYS = {'mon','tue','wed','thu','fri','sat','sun'}
_WEEKDAY_ZH = {'mon':'一','tue':'二','wed':'三','thu':'四','fri':'五','sat':'六','sun':'日'}

def _add_job(jid, func, job: dict):
    """依 schedule_day 格式決定每日/週/月排程。"""
    day_val = str(job['schedule_day'])
    hour, minute = job['schedule_hour'], job['schedule_minute']
    if day_val.lower() in _WEEKDAYS:
        scheduler.add_job(func, 'cron', id=jid,
                          day_of_week=day_val.lower(), hour=hour, minute=minute,
                          misfire_grace_time=3600)
    elif day_val.lower() == 'daily':
        scheduler.add_job(func, 'cron', id=jid,
                          hour=hour, minute=minute,
                          misfire_grace_time=3600)
    else:
        scheduler.add_job(func, 'cron', id=jid,
                          day=day_val, hour=hour, minute=minute,
                          misfire_grace_time=3600)

def _schedule_label(job: dict) -> str:
    day_val = str(job['schedule_day'])
    if day_val.lower() in _WEEKDAYS:
        names = {'mon':'週一','tue':'週二','wed':'週三','thu':'週四',
                 'fri':'週五','sat':'週六','sun':'週日'}
        return f"每{names.get(day_val.lower(), day_val)}"
    if day_val.lower() == 'daily':
        return "每天"
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
    if not scheduler.get_job('reload_schedules'):
        scheduler.add_job(load_and_schedule_all, 'interval', id='reload_schedules', minutes=5)
    if not scheduler.get_job('family_birthdays'):
        scheduler.add_job(check_family_birthdays, 'cron', id='family_birthdays', hour=8, minute=0)
    if not scheduler.get_job('family_reminders_check'):
        scheduler.add_job(check_family_reminders, 'interval', id='family_reminders_check', seconds=60)

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
- J大 還有一個家族群（家人），機器人名稱是「內莉」

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

E. 刪除排程（含「刪除」「移除」「取消排程」+任務名稱）
   → {"action":"delete_schedule","task_name":"XXX"}

E. 執行工具（trigger_local）— 判斷 client 與 target：

   【client 判斷】
   - 「KT BIKER」「KT」「機車配件」或未指定客戶 → client:"kt_biker"
   - 「家族」「家人」「內莉」「家裡」→ client:"family"

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

F. 推播含 URL → {"action":"push_url","url":"https://...","message":"說明（選填）","client":"kt_biker或family（選填，預設kt_biker）"}

J. 推播自訂文字訊息到指定群組（含「傳」「發」「告訴」「通知」+訊息內容+群組）
   ⚠️ 只有明確要「傳一段話到群組」才用此 action，生日/投票等管理指令絕對不能用這個
   → {"action":"push_message","message":"訊息內容","client":"family或kt_biker"}

K. 家族生日管理（含「加生日」「新增生日」「記生日」「登記生日」）
   測試提醒預覽 → {"action":"test_birthday_reminder"}
   ⚠️ 這是寫進資料庫的操作，不是傳訊息，絕對不能用 push_message
   新增生日 → {"action":"add_birthday","name":"媽媽","month":3,"day":15,"note":"送花（選填）"}
   範例：「幫內莉加生日：菁姊 5/25」→ {"action":"add_birthday","name":"菁姊","month":5,"day":25}
   查看清單 → {"action":"list_birthdays"}
   每天早上 8 點自動檢查隔天有無生日，有則推播到家族群

L. 家族投票（問題 + 最多 4 個選項）
   開始投票 → {"action":"start_vote","question":"週末去哪吃？","options":["火鍋","燒烤","日式"]}
   結束並公布結果 → {"action":"close_vote"}
   家族成員在群裡回覆數字（1/2/3）即完成投票

G. 查詢競品分析狀態（含「跑完了嗎」「分析進行中嗎」「狀態」）
   → {"action":"trigger_local","task_name":"competitor_status","client":"kt_biker","target":"cc_only"}

H. 單平台競品分析（含「只跑」「只分析」+平台名，節省時間）
   平台對應：YouTube/YT → youtube；TikTok/抖音 → tiktok；Facebook/FB → facebook；Instagram/IG → instagram；Threads → threads
   → {"action":"trigger_local","task_name":"single_platform_analysis","client":"kt_biker","platform":"youtube","target":"..."}

I. 取最新報告連結（含「上次的」「最新的」「不重跑」「重新發布」+平台名）
   預設只傳給我（cc_only），不推員工群
   → {"action":"trigger_local","task_name":"latest_platform_report","client":"kt_biker","platform":"youtube","target":"cc_only"}

【可用任務】
排程控制：weekly_report（競品週報，每週一）、monthly_shopee（蝦皮）
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
            label = _schedule_label({'schedule_day': u['schedule_day']})
            parts.append(f"發送日期：{label}")
        if 'schedule_hour' in u:
            parts.append(f"發送時間：{u['schedule_hour']:02d}:{u.get('schedule_minute', 0):02d}")
        if 'content' in u:
            parts.append("播報內容：已更新")
        return f"✅ 【{display_name}】設定完成\n" + "\n".join(parts)

    elif action == 'delete_schedule':
        keyword = cmd['task_name']
        # 先精確比對 task_name，找不到再模糊比對 display_name
        rows = supabase.table('bot_schedules').select('task_name,display_name').execute()
        matched = None
        for r in (rows.data or []):
            if r['task_name'] == keyword:
                matched = r
                break
        if not matched:
            kw_lower = keyword.lower()
            for r in (rows.data or []):
                if kw_lower in (r.get('display_name') or '').lower() or kw_lower in r['task_name'].lower():
                    matched = r
                    break
        if not matched:
            return f"❌ 找不到含「{keyword}」的排程，請用「查看排程」確認名稱"
        real_task = matched['task_name']
        display_name = matched['display_name']
        if scheduler.get_job(real_task):
            scheduler.remove_job(real_task)
        supabase.table('bot_schedules').delete().eq('task_name', real_task).execute()
        logger.info(f"排程已刪除: {real_task}")
        return f"🗑️ 【{display_name}】排程已刪除"

    elif action == 'list_schedules':
        rows = supabase.table('bot_schedules').select('*').execute()
        lines = ["📋 目前播報排程：\n"]
        for job in rows.data:
            st = "✅" if job['enabled'] else "⏸ 已暫停"
            lines.append(f"{st} {job['display_name']}")
            lines.append(f"   {_schedule_label(job)} {job['schedule_hour']:02d}:{job['schedule_minute']:02d}")
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

    elif action == 'add_birthday':
        name  = cmd.get('name', '').strip()
        month = cmd.get('month')
        day   = cmd.get('day')
        note  = cmd.get('note', '').strip()
        if not name or not month or not day:
            return "❌ 請提供 name、month、day"
        supabase.table('family_birthdays').insert(
            {'name': name, 'month': int(month), 'day': int(day), 'note': note}).execute()
        return f"✅ 已新增生日提醒：{name} {month}/{day}"

    elif action == 'test_birthday_reminder':
        rows = supabase.table('family_birthdays').select('*').execute()
        if not rows.data:
            return "📅 還沒有生日資料，請先新增"
        import random
        person = random.choice(rows.data)
        note = f"（{person['note']}）" if person.get('note') else ''
        prompt = f"明天是家族成員「{person['name']}」的生日{note}，請用繁體中文寫一則溫暖簡短的LINE生日提醒，給整個家族群看的，20~40字，不要加emoji以外的特殊符號。"
        try:
            resp = claude.messages.create(
                model="claude-haiku-4-5-20251001", max_tokens=150,
                messages=[{"role": "user", "content": prompt}])
            ai_msg = resp.content[0].text
        except Exception:
            ai_msg = f"記得明天是 {person['name']} 的生日！記得祝賀 🎉"
        preview = f"🎂 明天是 {person['name']} 的生日！\n\n{ai_msg}"
        return f"📋 生日提醒預覽（模擬對象：{person['name']} {person['month']}/{person['day']}）：\n\n{preview}\n\n✅ 以上是實際會推到家族群的訊息格式"

    elif action == 'list_birthdays':
        rows = supabase.table('family_birthdays').select('*').order('month').order('day').execute()
        if not rows.data:
            return "📅 目前沒有生日記錄"
        lines = ["📅 家族生日清單：\n"]
        for r in rows.data:
            note = f" （{r['note']}）" if r.get('note') else ''
            lines.append(f"• {r['name']}：{r['month']}/{r['day']}{note}")
        return "\n".join(lines)

    elif action == 'start_vote':
        question = cmd.get('question', '').strip()
        options  = cmd.get('options', [])
        if not question or len(options) < 2:
            return "❌ 請提供 question 和至少 2 個 options"
        active = _get_active_vote()
        if active:
            return "❌ 目前已有進行中的投票，請先關閉"
        row = supabase.table('family_votes').insert(
            {'question': question, 'options': options, 'votes': {}, 'status': 'active'}).execute()
        vote = row.data[0]
        push_to_group(family_config, FAMILY_GROUP_ID, _format_vote_msg(vote))
        return f"✅ 已在家族群開始投票：{question}"

    elif action == 'close_vote':
        active = _get_active_vote()
        if not active:
            return "❌ 目前沒有進行中的投票"
        supabase.table('family_votes').update({'status': 'closed'}).eq('id', active['id']).execute()
        result_msg = _format_vote_result(active)
        push_to_group(family_config, FAMILY_GROUP_ID, result_msg)
        return f"✅ 投票已結束，結果已推播到家族群"

    elif action == 'push_message':
        msg = cmd.get('message', '').strip()
        client = cmd.get('client', 'kt_biker')
        if not msg:
            return "❌ 沒有偵測到訊息內容"
        cfg = _client_cfg(client)
        push_to_group(cfg['token_config'], cfg['group_id'], msg)
        return f"✅ 已傳送到{cfg['display_name']}群"

    elif action == 'push_url':
        url = cmd.get('url', '').strip()
        msg = cmd.get('message', '').strip()
        client = cmd.get('client', 'kt_biker')
        if not url:
            return "❌ 沒有偵測到網址"
        content = f"{msg}\n{url}" if msg else url
        cfg = _client_cfg(client)
        push_to_group(cfg['token_config'], cfg['group_id'], content)
        return f"✅ 已推播連結到{cfg['display_name']}群"

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
    valid_actions = ('update_schedule', 'delete_schedule', 'list_schedules', 'manual_push',
                     'push_url', 'push_message', 'trigger_local', 'ask_target',
                     'add_birthday', 'list_birthdays', 'test_birthday_reminder',
                     'start_vote', 'close_vote')
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


# ── 家族 Webhook ──────────────────────────────────────────────

@app.route('/webhook/family', methods=['POST'])
def webhook_family():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        family_handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@family_handler.add(JoinEvent)
def on_family_join(event):
    if hasattr(event.source, 'group_id'):
        logger.info(f"[家族] 加入群組 ID: {event.source.group_id}")

# ── 淹水查詢（路線淹水雷達 LINE 版）──────────────────────────
# 台南行政區（含區名前綴的淹水感測站可用區名子字串比對）
# 刻意不含單字「東/南/北/中西」避免與「台南/南化」誤撞
_TAINAN_DISTRICTS = [
    "玉井", "南化", "左鎮", "楠西", "大內", "山上", "新化", "關廟", "龍崎",
    "官田", "六甲", "東山", "白河", "仁德", "歸仁", "永康", "安南", "安平",
    "佳里", "學甲", "麻豆", "下營", "柳營", "新營", "後壁", "鹽水", "西港",
    "七股", "將軍", "北門", "善化", "新市", "安定",
]


def query_flood(question: str) -> str:
    """內莉淹水查詢：從問句抓台南區名→回報沿線淹水感測器即時積水。"""
    found = [d for d in _TAINAN_DISTRICTS if d in question]
    if not found:
        return ("想查哪一區會不會淹水呢？跟我說區名就好 🌧\n"
                "例如：「玉井 南化 左鎮 淹水」")
    res = flood_api.query(found)
    if not res.get("ok"):
        return f"抱歉，淹水資料查詢失敗了 😢（{res.get('error', '')[:40]}）"
    sts = res["stations"]
    missing = res.get("missing", [])
    v = flood_api.overall_verdict(sts)
    t = res["queried_at"].strftime("%H:%M")
    lines = [f"🌧 淹水雷達（{t} 更新）", f"總結：{v['label']} — {v['advice']}", ""]
    if not sts:
        lines.append("此範圍查無淹水感測站。")
    for s in sts[:12]:
        emoji = s["level"]["label"].split()[0]  # 🟢/🟡/🟠/🔴
        tt = s["time"].strftime("%H:%M") if s["time"] else "—"
        warn = " ⚠️舊資料" if s["stale"] else ""
        lines.append(f"{emoji} {s['name']} {s['depth_cm']:.0f}cm @{tt}{warn}")
        if s["depth_cm"] > 0 and s.get("cctv"):
            lines.append(f"   📷 {s['cctv']}")
    if missing:
        lines.append("")
        lines.append(f"⚠️ {'、'.join(missing)}：查無感測站，需看 CCTV／封路，別當成沒淹")
    lines.append("")
    lines.append("（只看點位積水，不含坍方／封橋／封路，山路請另查封路資訊）")
    return "\n".join(lines)


# Google 地圖連結偵測（短連結 / 完整地圖網址）
_MAPS_LINK_RE = re.compile(
    r'(https?://(?:maps\.app\.goo\.gl|goo\.gl/maps|maps\.google\.[^\s]+|'
    r'(?:www\.)?google\.[^\s/]+/maps)[^\s]+)')


def _resolve_route_link(link: str):
    """解析 Google 地圖路線連結 → (origin, dest) 字串；非路線回 (None, None)。"""
    try:
        final = urllib.parse.unquote(
            requests.get(link, allow_redirects=True, timeout=20,
                         headers={"User-Agent": "Mozilla/5.0"}).url)
    except Exception as e:
        logger.error(f"[家族 淹水路線] resolve error: {e}")
        return None, None
    m = re.search(r'/maps/dir/([^/]+)/([^/@]+)', final)
    if m:
        return m.group(1).strip(), m.group(2).replace('+', ' ').strip()
    return None, None


def query_flood_route(link: str) -> str:
    """貼路線連結 → 分析沿線（0.8km 內）淹水感測器即時積水。"""
    origin, dest = _resolve_route_link(link)
    if not origin:
        return ("這看起來不是「路線」連結 🤔\n"
                "請在 Google 地圖規劃好『路線』後，用分享鈕複製連結給我～")
    try:
        d = gmaps.directions(origin, dest, mode="driving")
        if not d:
            return "找不到這條路線的開車路徑 😢，請確認起訖點。"
        leg = d[0]["legs"][0]
        segments = []
        instrs = []
        for st in leg["steps"]:
            pts = googlemaps.convert.decode_polyline(st["polyline"]["points"])
            plist = [(p["lat"], p["lng"]) for p in pts]
            instr = re.sub(r"<[^>]+>", "", st.get("html_instructions", ""))
            instrs.append(instr)
            segments.append((plist, flood_api.is_highway_step(instr)))
    except Exception as e:
        logger.error(f"[家族 淹水路線] directions error: {e}")
        return "路線規劃失敗了 😢，請稍後再試。"

    allres = flood_api.query_tainan_all()
    if not allres.get("ok"):
        return "淹水感測資料暫時取不到 😢，請稍後再試。"
    near = flood_api.classify_near_route(allres["stations"], segments, radius_km=0.8)
    t = allres["queried_at"].strftime("%H:%M")
    dest_short = dest.split('臺南市')[-1] if '臺南市' in dest else dest

    # 沿線封路/管制（TDX，路名比對）
    roads = tdx_api.extract_route_roads(instrs)
    counties = list(tdx_api._COUNTY_RE.findall(tdx_api._norm(dest)))
    alerts = tdx_api.route_alerts(
        os.environ.get("TDX_CLIENT_ID", ""), os.environ.get("TDX_CLIENT_SECRET", ""),
        roads, region_hints=[dest_short, "台南"], allowed_counties=counties or ["台南"])

    out = [f"🗺 路線淹水分析（{t} 更新）", f"→ {dest_short}",
           f"全程 {leg['distance']['text']} · 約 {leg['duration']['text']} · 沿線 {len(near)} 站", ""]

    # 封路示警放最前（安全最優先）
    if alerts.get("ok"):
        if alerts["closed"]:
            out.append("🚧 沿線封路／管制（高優先）：")
            for c in alerts["closed"][:6]:
                out.append(f"⛔ {c['title'][:80]}")
            out.append("")
        if alerts["weather"]:
            out.append("🌧 路況天氣提醒：")
            for w in alerts["weather"][:3]:
                out.append(f"• {w['title'][:70]}")
            out.append("")
        if alerts["closed_far"]:
            out.append(f"（另有 {len(alerts['closed_far'])} 筆同路名封閉但在他縣，可能不在你路段；如需可查公路局168）")
            out.append("")
    elif alerts.get("error"):
        out.append(f"（⚠️封路資訊暫不可用：{alerts['error']}）")
        out.append("")
    if not near:
        out.append("沿線 0.8km 內查無淹水感測站。\n（不代表沒淹，山路請另查封路資訊）")
        return "\n".join(out)

    surface = [s for s in near if not s["highway"]]
    highway = [s for s in near if s["highway"]]
    # 總結以「平面路段」為準（高架旁淹水多在路面下方，不影響行駛）
    v = flood_api.overall_verdict(surface if surface else near)
    out.append(f"總結：{v['label']} — {v['advice']}（以平面路段為準）")

    def _line(s):
        emoji = s["level"]["label"].split()[0]
        tt = s["time"].strftime("%H:%M") if s["time"] else "—"
        warn = " ⚠️舊資料" if s["stale"] else ""
        rows = [f"{emoji} {s['name']} {s['depth_cm']:.0f}cm（距路線{s['dist_km']*1000:.0f}m @{tt}{warn}）"]
        if s.get("cctv"):
            rows.append(f"   📷 {s['cctv']}")
        return rows

    sf = [s for s in surface if s["depth_cm"] > 0]
    hf = [s for s in highway if s["depth_cm"] > 0]
    if sf:
        out.append("")
        out.append("⚠️ 平面路段積水（會影響行駛）：")
        for s in sf:
            out += _line(s)
    if hf:
        out.append("")
        out.append("🛣️ 高架/匝道旁積水（多在路面下方，通常不影響；匝道、涵洞請留意）：")
        for s in hf:
            out += _line(s)
    if not sf and not hf:
        out.append("沿線各站目前皆無積水 🟢")
    normal = len(near) - len(sf) - len(hf)
    if normal:
        out.append(f"\n🟢 其餘 {normal} 站正常")
    out.append("")
    out.append("（淹水＝點位感測；封路＝國道/省道事件，小路坍方未必涵蓋，山區仍請留意）")
    return "\n".join(out)


@family_handler.add(MessageEvent, message=TextMessageContent)
def on_family_message(event):
    text = event.message.text.strip()
    user_id = event.source.user_id if hasattr(event.source, 'user_id') else 'unknown'
    is_group = hasattr(event.source, 'group_id')
    logger.info(f"[家族] is_group={is_group} text_repr={repr(text[:50])}")

    # 功能 8：投票接收
    active = _get_active_vote()
    if active and text in [str(i) for i in range(1, len(active['options']) + 1)]:
        votes = active.get('votes') or {}
        votes[user_id] = int(text)
        supabase.table('family_votes').update({'votes': votes}).eq('id', active['id']).execute()
        with ApiClient(family_config) as api_client:
            MessagingApi(api_client).reply_message(
                ReplyMessageRequest(reply_token=event.reply_token,
                                    messages=[TextMessage(text="✅ 你的票已記錄！")]))
        return

    # 功能 4：Q&A
    # 群組：需要「內莉」或「@內莉」開頭；私聊：直接回應所有訊息
    is_group = hasattr(event.source, 'group_id')
    if is_group:
        if not (text.startswith('內莉') or text.startswith('@內莉')):
            return
        question = text.replace('@內莉', '', 1).replace('內莉', '', 1).strip()
        if not question:
            return
    else:
        question = text

    def _reply():
        # 路線淹水分析：偵測到 Google 地圖連結 → 自動分析沿線淹水（最優先）
        mlink = _MAPS_LINK_RE.search(question)
        if mlink:
            reply_text = query_flood_route(mlink.group(1))
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 淹水路線] reply error: {e}")
            return

        # 淹水查詢（手打區名）— 優先於天氣/路程
        is_flood_q = bool(re.search(r'淹水|積水|淹大水|會不會淹|水淹|涉水|路.{0,3}積水', question))
        if is_flood_q:
            reply_text = query_flood(question)
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 淹水] reply error: {e}")
            return

        # 潮汐查詢（釣魚、挖蛤蜊、潮汐相關）
        is_tide_q = bool(re.search(r'釣魚|挖蛤蜊|潮汐|退潮|漲潮|滿潮|乾潮|捕魚|海釣|磯釣|蚵仔|潮水', question))
        if is_tide_q:
            if '後天' in question:
                day_offset = 2
            elif '明天' in question:
                day_offset = 1
            else:
                day_offset = 0
            city = None
            for key in _CITY_COORDS:
                if key in question:
                    city = key
                    break
            if city is None:
                city = '台南'
            if re.search(r'挖蛤蜊|蚵仔|採貝|貝類', question):
                activity = '挖蛤蜊'
            elif re.search(r'釣魚|海釣|磯釣|捕魚', question):
                activity = '釣魚'
            else:
                activity = '海邊活動'
            tide = query_tide(city, day_offset)
            if tide is None:
                reply_text = f"抱歉，「{city}」附近暫時沒有潮汐資料 😢"
            else:
                reply_text = format_tide_advice(tide, activity, question)
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 潮汐] reply error: {e}")
            return

        # 天氣查詢（支援今天/明天/後天 + 活動建議）
        is_weather_q = bool(re.search(r'天氣|氣溫|溫度|下雨|會不會雨|降雨|幾度', question))
        is_activity_q = bool(re.search(r'適合|戶外|海邊|出遊|爬山|踏青|外出|出門|騎車|打球|烤肉|游泳|帶傘', question))
        if is_weather_q or is_activity_q:
            # 偵測日期偏移
            if '後天' in question:
                day_offset = 2
            elif '明天' in question:
                day_offset = 1
            else:
                day_offset = 0
            # 從已知城市清單中掃描問題，找不到就用台南當預設
            city = None
            for key in _CITY_COORDS:
                if key in question:
                    city = key
                    break
            if city is None:
                city = '台南'
            w = query_weather(city, day_offset)
            if w is None:
                reply_text = f"抱歉，「{city}」查不到天氣資料，目前只支援台灣縣市喔 🌦️"
            else:
                reply_text = format_weather(w, question)
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 天氣] reply error: {e}")
            return

        # 提醒清單查詢
        is_list_reminder_q = bool(re.search(r'提醒清單|查提醒|列出.*提醒|有什麼提醒|哪些提醒|查看提醒|目前.*提醒|有沒有提醒|我的提醒', question))
        if is_list_reminder_q:
            try:
                from datetime import timezone, timedelta as _td
                now_iso = datetime.now(timezone(_td(hours=8))).isoformat()
                rows = (supabase.table('family_reminders')
                        .select('*').eq('sent', False).gte('target_at', now_iso)
                        .order('target_at').execute())
                if not rows.data:
                    reply_text = "目前沒有待處理的提醒事項 😊"
                else:
                    lines = ["📋 待處理提醒：\n"]
                    for r in rows.data:
                        try:
                            dt = datetime.fromisoformat(r['target_at'])
                            time_str = f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"
                        except Exception:
                            time_str = r['target_at']
                        lines.append(f"⏰ {time_str} {r['message']}")
                    reply_text = '\n'.join(lines)
            except Exception as e:
                logger.error(f"[家族提醒] list error: {e}")
                reply_text = "查詢提醒清單失敗，請稍後再試 😢"
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 提醒清單] reply error: {e}")
            return

        # 提醒取消
        is_cancel_q = bool(re.search(r'取消提醒|刪除提醒|不用提醒|取消.*提醒|刪掉提醒', question))
        if is_cancel_q:
            if re.search(r'取消所有|全部取消|清空提醒|所有提醒|全部提醒', question):
                keyword = '__all__'
            else:
                keyword = parse_reminder_cancel(question)
            if keyword:
                try:
                    if keyword == '__all__':
                        supabase.table('family_reminders').delete().eq('user_id', user_id).eq('sent', False).execute()
                        reply_text = "✅ 已取消所有待處理的提醒"
                    else:
                        rows = (supabase.table('family_reminders')
                                .select('id,message').eq('sent', False)
                                .filter('message', 'ilike', f'%{keyword}%').execute())
                        if not rows.data:
                            reply_text = f"找不到「{keyword}」相關的待處理提醒 🤔"
                        else:
                            for r in rows.data:
                                supabase.table('family_reminders').delete().eq('id', r['id']).execute()
                            msgs = '、'.join(r['message'] for r in rows.data)
                            reply_text = f"✅ 已取消提醒：{msgs}"
                except Exception as e:
                    logger.error(f"[家族提醒] cancel error: {e}")
                    reply_text = "取消提醒失敗，請稍後再試 😢"
                try:
                    with ApiClient(family_config) as api_client:
                        MessagingApi(api_client).reply_message(
                            ReplyMessageRequest(reply_token=event.reply_token,
                                                messages=[TextMessage(text=reply_text)]))
                except Exception as e:
                    logger.error(f"[家族 取消提醒] reply error: {e}")
                return

        # 提醒修改
        is_modify_q = bool(re.search(r'修改提醒|改.*提醒|提醒.*改到|延後.*提醒|提醒.*延', question))
        if is_modify_q:
            parsed = parse_reminder_modify(question)
            if parsed:
                try:
                    rows = (supabase.table('family_reminders')
                            .select('id,message').eq('sent', False)
                            .filter('message', 'ilike', f'%{parsed["keyword"]}%').execute())
                    if not rows.data:
                        reply_text = f"找不到「{parsed['keyword']}」相關的待處理提醒 🤔"
                    else:
                        rid = rows.data[0]['id']
                        msg = rows.data[0]['message']
                        supabase.table('family_reminders').update({'target_at': parsed['target_at']}).eq('id', rid).execute()
                        try:
                            dt = datetime.fromisoformat(parsed['target_at'])
                            time_str = f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"
                        except Exception:
                            time_str = parsed['target_at']
                        reply_text = f"✅ 已把「{msg}」改到 {time_str} 提醒你"
                except Exception as e:
                    logger.error(f"[家族提醒] modify error: {e}")
                    reply_text = "修改提醒失敗，請稍後再試 😢"
                try:
                    with ApiClient(family_config) as api_client:
                        MessagingApi(api_client).reply_message(
                            ReplyMessageRequest(reply_token=event.reply_token,
                                                messages=[TextMessage(text=reply_text)]))
                except Exception as e:
                    logger.error(f"[家族 修改提醒] reply error: {e}")
                return

        # 提醒設定（提醒我、叫我、幫我提醒等）
        is_reminder_q = bool(re.search(r'提醒我|提醒一下|幫我提醒|設提醒|叫我', question))
        if is_reminder_q:
            parsed = parse_reminder_request(question)
            if parsed:
                push_type = 'group' if is_group else 'user'
                push_target = FAMILY_GROUP_ID if is_group else user_id
                try:
                    supabase.table('family_reminders').insert({
                        'user_id': user_id,
                        'message': parsed['message'],
                        'target_at': parsed['target_at'],
                        'push_target': push_target,
                        'push_type': push_type,
                        'sent': False,
                    }).execute()
                    try:
                        dt = datetime.fromisoformat(parsed['target_at'])
                        time_str = f"{dt.month}/{dt.day} {dt.hour:02d}:{dt.minute:02d}"
                    except Exception:
                        time_str = parsed['target_at']
                    reply_text = f"好的！⏰ 我會在 {time_str} 提醒你：{parsed['message']}"
                except Exception as e:
                    logger.error(f"[家族提醒] save error: {e}")
                    reply_text = "抱歉，提醒設定失敗，請稍後再試 😢"
                try:
                    with ApiClient(family_config) as api_client:
                        MessagingApi(api_client).reply_message(
                            ReplyMessageRequest(reply_token=event.reply_token,
                                                messages=[TextMessage(text=reply_text)]))
                except Exception as e:
                    logger.error(f"[家族 提醒] reply error: {e}")
                return

        # AI 日報控制
        is_ai_digest_q = bool(re.search(
            r'AI日報|ai日報|ai 日報|人工智慧日報|AI新聞|日報.*設定|日報.*時間|日報.*改|日報.*暫停|日報.*啟動|日報.*週報',
            question, re.IGNORECASE))
        if is_ai_digest_q:
            reply_text = handle_ai_digest_control(question)
            try:
                with ApiClient(family_config) as api_client:
                    MessagingApi(api_client).reply_message(
                        ReplyMessageRequest(reply_token=event.reply_token,
                                            messages=[TextMessage(text=reply_text)]))
            except Exception as e:
                logger.error(f"[家族 AI日報] reply error: {e}")
            return

        # 路程查詢
        route_match = re.search(
            r'從\s*(.+?)\s*到\s*(.+?)(?:\s*多遠|多久|路程|距離|開車|怎麼走|怎麼開|要幾分|要多久|$)',
            question)
        if not route_match:
            route_match = re.search(
                r'(.+?)\s*(?:到|→|至|~)\s*(.+?)(?:\s*多遠|多久|路程|距離|開車|怎麼走|怎麼開|要幾分|要多久)',
                question)
        if route_match:
            origin = route_match.group(1).strip()
            destination = route_match.group(2).strip()
            reply_text = query_route(origin, destination)
        else:
            try:
                resp = claude.messages.create(
                    model="claude-haiku-4-5-20251001", max_tokens=200,
                    system="你是溫暖親切的家族助理，名叫內莉。用繁體中文、口語化的方式回答，回答簡潔（100字內）。",
                    messages=[{"role": "user", "content": question}])
                reply_text = resp.content[0].text.strip()
            except Exception as e:
                reply_text = "抱歉，我現在有點忙～稍後再試試看 😅"
                logger.error(f"[家族 Q&A] error: {e}")
        try:
            with ApiClient(family_config) as api_client:
                MessagingApi(api_client).reply_message(
                    ReplyMessageRequest(reply_token=event.reply_token,
                                        messages=[TextMessage(text=reply_text)]))
        except Exception as e:
            logger.error(f"[家族 Q&A] reply error: {e}")
    threading.Thread(target=_reply, daemon=True).start()


# ── 啟動 ─────────────────────────────────────────────────────

load_and_schedule_all()
scheduler.start()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 8000))
    app.run(host='0.0.0.0', port=port)
