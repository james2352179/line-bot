# KT BIKER LINE Bot 系統 — Claude Code 交接文件

> 本文件給「安裝在客戶電腦上的新 Claude Code instance」閱讀。
> 目的：讓新 CC 不重蹈踩過的坑，能在最短時間內還原或複製整套系統。
>
> 最後更新：2026-05-19

---

## 一、系統定位與目的

KT BIKER 系統是一套「行銷自動化 + AI 助理」解決方案，由以下幾個部分組成：

1. **指揮 Bot（CC Bot）**：J大的私人 AI 助理，語意理解指令、控制 KT BIKER Bot、觸發本地工具
2. **KT BIKER Bot**：單向推播報表到員工群組（月排程 + 手動觸發）
3. **本地 Daemon**：Mac 常駐程式，輪詢任務佇列、執行工具（競品分析、蝦皮報表等）
4. **本地工具集**：競品戰情室、蝦皮分析工具，產生 HTML 報表後上傳 Cloudflare Pages

訊息流：

```
J大 LINE 訊息
    ↓
Railway（CC Bot / main.py）
    ↓ 語意解析（Claude Sonnet）
    ↓ 若是工具指令：寫 pending_tasks 到 Supabase
    ↓
Supabase（pending_tasks 表）
    ↓
Mac 本地 Daemon（kt_biker_daemon.py，每 60 秒輪詢）
    ↓ 執行對應工具
    ↓ 寫結果回 Supabase（status=done/error）
    ↓
Railway 每 60 秒讀取已完成任務
    ↓
LINE 推播結果給 J大 / 員工群組
```

---

## 二、基礎設施一覽

| 服務 | 用途 | 費用 | 備註 |
|------|------|------|------|
| Railway | 跑 Flask LINE Bot Server | $5/月 | 服務名：`incredible-radiance` |
| Supabase | 持久化排程、對話記憶、任務佇列 | 免費 | Tokyo region，project ID: `zkpxxvchpqqbusgoyfsh` |
| Cloudflare Pages | 靜態 HTML 報表發布 | 免費 | 多個 site：shopee, competitor, short_video... |
| Claude API | CC Bot AI 路由 + Daemon 修復 Agent | $1.5~6/月 | Sonnet 4.6 |
| macOS launchd | 本地 Daemon 開機自動啟動 | 免費 | 4 個 plist |

---

## 三、檔案結構

```
/Users/kuanghao/Downloads/kuanghao-claude/
├── line-bot/                        ← 本目錄
│   ├── main.py                      ← Railway Flask Server（CC Bot + KT BIKER Bot）
│   ├── kt_biker_daemon.py           ← 本地常駐 Daemon（任務執行器）
│   ├── kt_biker_auto.py             ← 推播函式庫（shopee/competitor/short_video）
│   ├── Procfile                     ← Railway 啟動指令
│   ├── requirements.txt             ← Python 依賴
│   ├── kt_biker.env                 ← 本地工具用的 env（KT_CHANNEL_ACCESS_TOKEN, KT_GROUP_ID）
│   ├── kt_biker_supabase.env        ← 本地 Supabase 連線資訊
│   ├── venv/                        ← Python 虛擬環境（Daemon 用）
│   └── HANDOVER.md                  ← 本文件
│
├── kh_shopee_tool/                  ← 蝦皮廣告分析工具
│   ├── main.py                      ← GUI 主程式
│   ├── core/reporter/
│   │   └── cf_pages_uploader.py     ← Cloudflare Pages 上傳器（用 Wrangler CLI）
│   ├── data/history.json            ← 蝦皮各期報表資料
│   └── config/settings.json         ← 工具設定（含 short_video_files）
│
├── kh_competitor_tool/              ← 競品戰情室
│   └── webapp/app.py                ← Flask Server（port 5173）
│
└── sentiment_radar/                 ← SENTINEL 聲量雷達（獨立工具）
```

---

## 四、Supabase 表結構

### 4.1 `pending_tasks`（任務佇列）

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | uuid (PK) | 自動產生 |
| task_name | text | 任務類型，見下方清單 |
| status | text | `pending` / `running` / `done` / `error` |
| params | jsonb | 任務參數（含 client, target, reply_to_user_id 等）|
| result | text | 執行完成後的結果文字 |
| created_at | timestamptz | 自動 |
| updated_at | timestamptz | 手動更新 |

**task_name 清單：**
- `competitor_analysis` — 全平台競品分析
- `shopee_push` — 蝦皮廣告報表
- `product_perf_push` — 蝦皮商品表現報表（需 params.period）
- `short_video_push` — 蝦皮短影音報表
- `competitor_status` — 查詢競品分析進行狀態
- `single_platform_analysis` — 單平台分析（需 params.platform）
- `latest_platform_report` — 取最新報告連結（需 params.platform）
- `code_repair` — 自動修復失敗任務（Daemon 的 AI Agent）

**params 常用欄位：**
```json
{
  "client": "kt_biker",
  "target": "cc_only",
  "reply_to_user_id": "Uxxxxxxxxxxxx",
  "platform": "youtube",
  "profile": "汽車美容用品業",
  "period": "2026年04月"
}
```

### 4.2 `bot_schedules`（排程設定）

| 欄位 | 類型 | 說明 |
|------|------|------|
| task_name | text (PK) | 任務識別名 |
| display_name | text | 顯示用名稱 |
| enabled | bool | 是否啟用 |
| schedule_day | int | 每月幾號 |
| schedule_hour | int | 幾點（24h）|
| schedule_minute | int | 幾分 |
| content | text | 推播訊息內容（支援 `{date}` 和 `\n`）|
| updated_at | timestamptz | 上次更新 |

**目前排程：**
| task_name | 說明 | 時間 |
|-----------|------|------|
| monthly_shopee | 蝦皮廣告報表（靜態備用）| 每月 3 號 09:30 |
| shopee_short_video_push | 蝦皮短影音報表 | 每月 5 號 09:30 |

### 4.3 `cc_conversations`（CC Bot 對話記憶）

| 欄位 | 類型 | 說明 |
|------|------|------|
| id | uuid (PK) | 自動 |
| user_id | text | LINE 用戶 ID |
| role | text | `user` / `assistant` |
| content | text | 訊息內容 |
| created_at | timestamptz | 自動 |

---

## 五、環境變數完整清單

### 5.1 Railway 環境變數（main.py 使用）

| 變數名 | 說明 | 取得方式 |
|--------|------|----------|
| `LINE_CHANNEL_SECRET` | CC Bot Channel Secret | LINE Developers Console |
| `LINE_CHANNEL_ACCESS_TOKEN` | CC Bot Access Token | LINE Developers Console |
| `KT_CHANNEL_SECRET` | KT BIKER Bot Channel Secret | LINE Developers Console |
| `KT_CHANNEL_ACCESS_TOKEN` | KT BIKER Bot Access Token | LINE Developers Console |
| `ANTHROPIC_API_KEY` | Claude API Key | console.anthropic.com |
| `KT_GROUP_ID` | KT BIKER 員工群組 LINE Group ID | 見下方「取得 Group ID」|
| `SUPABASE_URL` | Supabase 專案 URL | Supabase → Settings → API |
| `SUPABASE_KEY` | **service_role key**（不是 anon key）| Supabase → Settings → API |
| `PORT` | Railway 自動注入（8080） | 不需手動設定 |

> ⚠️ `SUPABASE_KEY` 必須用 **service_role key**，用 anon key 會靜默查不到資料。

### 5.2 本地 launchd 環境變數

Daemon plist（`com.ktbiker.daemon.plist`）和排程 plist 需要以下變數：

| 變數名 | 使用場景 |
|--------|----------|
| `KT_CHANNEL_ACCESS_TOKEN` | 推播到 KT BIKER 群組 |
| `KT_GROUP_ID` | 推播目標 |
| `SUPABASE_URL` | Daemon 讀寫任務佇列 |
| `SUPABASE_KEY` | 同上（service_role）|
| `ANTHROPIC_API_KEY` | code_repair Agent |
| `PATH` | `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin`（Wrangler 需要）|
| `HOME` | `/Users/<username>`（Wrangler 寫 log 需要）|

---

## 六、LINE Bot 設定（LINE OA Manager）

### CC Bot（@327uqtmc）
1. 前往 LINE OA Manager
2. 回應設定 → **聊天：關閉**（否則群組訊息不會觸發 webhook）
3. Webhook：`https://incredible-radiance-production-6803.up.railway.app/webhook`
4. Webhook 發送：開啟

### KT BIKER Bot（@405asmhw）
1. 回應設定 → **聊天：關閉**
2. Webhook：`.../webhook/ktbiker`
3. 這個 Bot 只做推播，不需回應訊息

### 取得員工群組 Group ID
1. 把 KT BIKER Bot 加入目標群組
2. 群組任意人發訊息，Railway log 會印出 `[KT BIKER] 加入群組 ID: Cxxxxxxx`
3. 把這個 ID 存到 Railway 環境變數 `KT_GROUP_ID`

---

## 七、Railway 部署

```bash
# 本地測試
cd line-bot
python main.py  # 注意：本地需手動載入 .env

# 部署（GitHub auto-deploy，push 到 main 分支即自動觸發）
git push origin main
```

**Procfile 內容：**
```
web: gunicorn main:app --bind 0.0.0.0:$PORT
```
或：
```
web: python main.py
```

**Railway 注意事項：**
- Flask 要 listen `0.0.0.0:$PORT`（PORT Railway 自動設為 8080）
- 若 webhook 突然斷線：Railway → Settings → Source 重新連 GitHub repo
- 新增環境變數後需 redeploy 才生效

---

## 八、本地 Daemon 設定

### 8.1 安裝步驟

```bash
# 1. 建立 venv
cd /Users/kuanghao/Downloads/kuanghao-claude/line-bot
/opt/homebrew/bin/python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 2. 放置 plist
cp com.ktbiker.daemon.plist ~/Library/LaunchAgents/

# 3. 載入 plist
launchctl load ~/Library/LaunchAgents/com.ktbiker.daemon.plist

# 4. 確認啟動
launchctl list | grep ktbiker
tail -f /Users/kuanghao/Downloads/kuanghao-claude/line-bot/daemon.log
```

### 8.2 四個 launchd plist

| plist 名稱 | 觸發時機 | 執行內容 |
|------------|----------|----------|
| `com.ktbiker.daemon.plist` | 開機常駐（KeepAlive=true）| `kt_biker_daemon.py`，輪詢任務佇列 |
| `com.ktbiker.shopee.plist` | 每月 2 號 09:00 | `kt_biker_auto.py shopee` |
| `com.ktbiker.competitor.plist` | 每月 1、15 號 09:00 | `kt_biker_auto.py competitor` |
| `com.ktbiker.shortvideo.plist` | 每月 5 號 09:30 | `kt_biker_auto.py short_video` |

### 8.3 正確的 plist 寫法（避免 exit 126）

```xml
<key>ProgramArguments</key>
<array>
  <string>/opt/homebrew/bin/python3.12</string>
  <string>/path/to/script.py</string>
  <string>argument</string>
</array>
<key>EnvironmentVariables</key>
<dict>
  <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  <key>HOME</key><string>/Users/kuanghao</string>
  <!-- 其他 key/token 環境變數 -->
</dict>
```

> ⚠️ 絕對不可以用 `bash → shell script` 的方式。macOS `com.apple.provenance` 安全屬性在 launchd 非互動環境會封鎖，導致 exit 126。

---

## 九、Cloudflare Pages 部署

報表上傳由 `kh_shopee_tool/core/reporter/cf_pages_uploader.py` 的 `PagesUploader` 類別統一處理。

**目前 CF Pages sites：**
| site_key | 用途 | domain |
|----------|------|--------|
| shopee | 蝦皮廣告報表 | kh-shopee.pages.dev（類）|
| short_video | 短影音報表 | kh-short-video.pages.dev（類）|
| competitor | 競品分析報表 | （依 profile 分別）|

**CF Pages 部署必須用 Wrangler CLI，不可自己打 API：**
```python
# 正確做法（已實作在 cf_pages_uploader.py）
subprocess.run([npx_path, "wrangler@3", "pages", "deploy", dir, "--project-name", name],
               env={**os.environ, "HOME": home_dir, "PATH": "/opt/homebrew/bin:..."},
               cwd=tmpdir)
```

> ⚠️ 直接 POST `/deployments` API 會踩到 manifest 格式坑（code 8000096）且 build pipeline 不會跑。

---

## 十、新增客戶（最少改動清單）

當要複製這套系統給新客戶時，只需要改以下幾處：

### 10.1 Railway main.py

```python
# CLIENT_REGISTRY 加一欄
CLIENT_REGISTRY = {
    'kt_biker': { ... },           # 保留原有
    'new_client': {
        'display_name': '客戶名稱',
        'token_config': Configuration(access_token=os.environ.get('NEW_CLIENT_TOKEN', '')),
        'group_id': os.environ.get('NEW_CLIENT_GROUP_ID', ''),
    },
}
```

同時在 Railway 加環境變數：`NEW_CLIENT_TOKEN`, `NEW_CLIENT_GROUP_ID`

### 10.2 Daemon kt_biker_daemon.py

```python
# CLIENT_TASK_HANDLERS 加一欄（可複用現有 handler）
CLIENT_TASK_HANDLERS = {
    'kt_biker': { ... },
    'new_client': {
        'competitor_analysis': _run_competitor,
        'shopee_push':         _run_shopee,
        # 依客戶需求選填
    },
}
```

### 10.3 launchd plist

複製現有 plist，修改：
- `Label`：改成 `com.newclient.daemon`
- `KT_CHANNEL_ACCESS_TOKEN` / `KT_GROUP_ID`：換成新客戶的 token 和群組 ID
- `StandardOutPath` / `StandardErrorPath`：改 log 路徑

### 10.4 ADMIN_SYSTEM 提示詞

在 main.py 的 `ADMIN_SYSTEM` 的「client 判斷」區塊補上：
```
- 「客戶名稱」「關鍵字」→ client:"new_client"
```

---

## 十一、已踩過的地雷（所有坑都在這裡）

### 🔴 必看：致命錯誤

1. **Supabase key 用錯**
   - ❌ 錯：`anon key`（REST API 查不到資料、靜默空回傳）
   - ✅ 對：`service_role key`

2. **LINE OA 聊天模式沒關**
   - ❌ 結果：群組訊息不觸發 webhook
   - ✅ 設定路徑：LINE OA Manager → 回應設定 → 聊天 → 關閉

3. **launchd 用 bash script 包裝**
   - ❌ 結果：exit 126 / Operation not permitted
   - ✅ plist `ProgramArguments` 直接叫 python3.12，不透過 bash

4. **CF Pages 自己打 raw API**
   - ❌ 結果：code 8000096 / deployment stages 全 idle / 頁面 404
   - ✅ 用 `PagesUploader` class（Wrangler CLI）

### 🟡 常見坑

5. **Railway port 填錯**
   - Railway 給的 PORT 是 8080，不是 Flask 預設的 5000 或 8000
   - Flask 要 `app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))`

6. **daemon 環境 npx 找不到**
   - launchd PATH 精簡，沒有 `/opt/homebrew/bin`
   - subprocess 呼叫前要手動 prepend PATH

7. **daemon 環境 HOME 為空**
   - Wrangler 需要 `~/.wrangler/` 目錄，HOME 空會報錯
   - plist 的 `EnvironmentVariables` 補 `HOME` 或 subprocess 前 `pwd.getpwuid(os.getuid()).pw_dir`

8. **daemon cwd 是 `/`**
   - Wrangler 某些操作需要 cwd 有寫入權限
   - `subprocess.run(..., cwd=tmpdir)` 傳 HTML 所在的 temp 目錄

9. **Supabase upsert 靜默失敗**
   - `supabase.table().upsert(data, on_conflict='col')` 在部分版本 no error 但沒更新
   - ✅ 改用 update + insert fallback：
     ```python
     result = supabase.table('t').update(data).eq('key', val).execute()
     if not result.data:
         supabase.table('t').insert({**defaults, **data}).execute()
     ```

10. **Railway webhook 斷線**
    - 症狀：LINE 發訊息無回應、Railway log 無進入
    - 修法：Railway → Settings → Source → 重新連 GitHub repo

11. **CC Bot 輸出兩段 JSON（多目標推播）**
    - ❌ 結果：只執行第一個 JSON，第二個變成文字回傳
    - ✅ 多目標一律用 `target: "both"`，不輸出兩段 JSON

12. **CC Bot 新功能用關鍵字路由**
    - ❌ 問題：要說很精確的詞才能被理解
    - ✅ 新功能寫進 ADMIN_SYSTEM 提示詞，讓 Claude 語意判斷，不加 keyword 陣列

---

## 十二、可客製化參數（交給客戶調整的項目）

| 項目 | 修改位置 | 說明 |
|------|----------|------|
| Bot 人設 / 業務知識 | `ADMIN_SYSTEM` 提示詞 | 換品牌名稱、業務背景描述 |
| 員工群組 | Railway 環境變數 `KT_GROUP_ID` | 換群組 ID 即可 |
| 排程時間 | Supabase `bot_schedules` 表 | 透過 CC Bot 用自然語言改（「把蝦皮報表改到每月5號」）|
| 推播內容 | Supabase `bot_schedules.content` | 支援 `{date}` 動態日期 |
| 競品分析對象 | `kt_biker_auto.py` 的 `COMPETITOR_PID` | 換成目標行業的 profile ID |
| 工具組合 | `CLIENT_TASK_HANDLERS` | 每個客戶可選配不同工具 |
| 報表 domain | CF Pages project name | 每客戶建一組 CF Pages |

---

## 十三、逐步安裝 Checklist（新環境從零建立）

### Phase 1：Supabase

- [ ] 建立新 Supabase 專案（Tokyo region）
- [ ] 建立 `pending_tasks` 表（見第四節 schema）
- [ ] 建立 `bot_schedules` 表（見第四節 schema）
- [ ] 建立 `cc_conversations` 表（見第四節 schema）
- [ ] 記下 service_role key（不是 anon key）

### Phase 2：LINE Bot

- [ ] 在 LINE Developers Console 建立兩個 Messaging API Channel（CC Bot + 客戶 Bot）
- [ ] 記下兩組 Channel Secret + Channel Access Token
- [ ] LINE OA Manager → 回應設定 → **聊天：關閉**（兩個 Bot 都要做）

### Phase 3：Railway

- [ ] 建立 Railway 服務，連接 GitHub repo（`line-bot` 目錄）
- [ ] 設定所有環境變數（見第五節）
- [ ] 確認 Flask 跑在 `0.0.0.0:$PORT`
- [ ] 把 Railway domain 填入 LINE Developers Console 的 Webhook URL
- [ ] 用 LINE 發一則訊息給 CC Bot，確認有回應

### Phase 4：本地 Daemon

- [ ] 建立 venv：`/opt/homebrew/bin/python3.12 -m venv venv`
- [ ] 安裝依賴：`pip install -r requirements.txt`
- [ ] 複製並修改 plist 檔案，填入正確的 token / group ID / path
- [ ] `launchctl load ~/Library/LaunchAgents/com.ktbiker.daemon.plist`
- [ ] 確認 daemon.log 顯示「多客戶 Daemon 啟動」

### Phase 5：Cloudflare Pages

- [ ] 申請 Cloudflare 帳號
- [ ] 建立 CF Pages project（每個報表類型一個）
- [ ] 取得 API Token（Zone:Read + Page:Edit 權限）
- [ ] 更新 `cf_pages_uploader.py` 的 `ACCOUNT_ID` 和 `project_name` mapping

### Phase 6：驗收測試

- [ ] 用 CC Bot 說「給我蝦皮報表」→ 確認 daemon 執行、結果回傳到 CC
- [ ] 用 CC Bot 說「查一下排程狀態」→ 確認列出排程
- [ ] 手動觸發一次競品分析 → 確認報表生成並上傳 CF Pages
- [ ] 確認每月自動排程的 plist 正確載入

---

## 十四、常用指令速查

```bash
# 查看 daemon 狀態
launchctl list | grep ktbiker

# 重啟 daemon
launchctl unload ~/Library/LaunchAgents/com.ktbiker.daemon.plist
launchctl load ~/Library/LaunchAgents/com.ktbiker.daemon.plist

# 即時看 daemon log
tail -f /Users/kuanghao/Downloads/kuanghao-claude/line-bot/daemon.log

# 手動跑一次推播（測試用）
cd /Users/kuanghao/Downloads/kuanghao-claude/line-bot
source kt_biker.env  # 載入 env
python kt_biker_auto.py shopee
python kt_biker_auto.py competitor
python kt_biker_auto.py short_video

# 查 Railway log
railway logs  # 需安裝 railway CLI

# 手動清除 stuck 的 pending task
# 在 Supabase 後台 Table Editor → pending_tasks → 刪掉 status=running 的 row
```

---

## 十五、相關檔案路徑速查

| 內容 | 路徑 |
|------|------|
| Railway Flask Server | `line-bot/main.py` |
| 本地 Daemon | `line-bot/kt_biker_daemon.py` |
| 推播函式 | `line-bot/kt_biker_auto.py` |
| CF Pages 上傳器 | `kh_shopee_tool/core/reporter/cf_pages_uploader.py` |
| 蝦皮報表資料 | `kh_shopee_tool/data/history.json` |
| 蝦皮工具設定 | `kh_shopee_tool/config/settings.json` |
| 競品 Flask Server | `kh_competitor_tool/webapp/app.py`（port 5173）|
| Daemon plist（常駐）| `~/Library/LaunchAgents/com.ktbiker.daemon.plist` |
| Shopee plist（排程）| `~/Library/LaunchAgents/com.ktbiker.shopee.plist` |
| Competitor plist（排程）| `~/Library/LaunchAgents/com.ktbiker.competitor.plist` |
| Short video plist（排程）| `~/Library/LaunchAgents/com.ktbiker.shortvideo.plist` |
