
<img src="app/web/static/icon.png" width="90" height="90">
<h1>Bus API Server</h1>

YetAnotherBusApp（YABus）的後端 API 伺服器

<p align="left">
  <img src="https://img.shields.io/badge/Python-3.13-blue">
  <img src="https://img.shields.io/badge/FastAPI-0.115%2B-teal">
  <img src="https://img.shields.io/badge/DB-SQLite-lightgrey">
</p>

## 關於專案

Bus API Server 是一個以 FastAPI 打造的交通資訊後端，整合 [TDX 運輸資料流通服務](https://tdx.transportdata.tw/) 作為主要資料來源，並以 SQLite 儲存靜態路線資料與應用程式資料。它是 [YetAnotherBusApp（YABus）](https://github.com/AvianJay/yetanotherbusapp) App 的官方後端，同時也可獨立部署作為公開的交通資訊 API。

除了公車動態，伺服器也整合了捷運、台鐵／高鐵、YouBike 等資料，並提供帳號系統（Discord / Google OAuth）、雲端收藏同步、推播通知、公告與意見回饋管理等完整功能。

## 功能特色

### 多模式交通資料

- 公車即時到站、車輛位置、路線站序、路線線型、班表與假日資訊
- 捷運（含台北、高雄、桃園等系統）路線、車站、到站看板、班距查詢
- 台鐵（TRA）與高鐵（THSR）車站、時刻表、即時到站看板、列車位置、行車警示
- YouBike 等公共自行車站點與鄰近站點查詢

### 帳號系統

- 僅支援 Discord / Google OAuth，無帳號密碼登入
- 以裝置為單位核發登入權杖，可管理已登入裝置並個別登出
- 支援將 Discord 與 Google 連結到同一個帳號

### 雲端收藏同步

- 以命名空間（namespace）儲存使用者資料（例如收藏路線），支援樂觀鎖與衝突策略
- 可跨裝置同步收藏內容

### 推播與公告

- 透過 Firebase Cloud Messaging 推播公告通知
- 公告可依平台與版本鎖定目標受眾
- 使用者意見回饋收集，並可選擇性轉發至 Discord Webhook

### 管理後台

- 使用者管理（角色調整、強制登出）
- 請求分析儀表板（依端點、平台、版本統計）
- 公告與意見回饋管理頁面

### 下載式資料庫

- 提供精簡過的 SQLite 資料庫下載，方便 App 端離線查詢路線目錄與站牌資料

## 技術棧

- **框架**：FastAPI、Uvicorn
- **資料庫**：SQLite（靜態路線資料庫、應用程式資料庫各自獨立）
- **主要資料來源**：[TDX 運輸資料流通服務](https://tdx.transportdata.tw/)、新北市政府開放資料（公車即時到站的輔助資料來源）
- **身份驗證**：Discord OAuth2、Google OAuth2 / Google Sign-In（含原生 Android／iOS）
- **推播**：Firebase Cloud Messaging
- **其他**：Cloudflare Tunnel（選用，供無公開 IP 的部署環境使用）

## 快速開始

### 需求

- Python 3.13+（建議與部署環境版本一致）

### Clone

```bash
git clone https://github.com/AvianJay/busapiserver.git
cd busapiserver
```

### 安裝依賴

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 設定環境變數

至少需要 TDX 的 API 憑證。可寫入 shell 環境變數，或在專案根目錄建立 `.env`：

```env
TDX_CLIENT_ID=your_client_id
TDX_CLIENT_SECRET=your_client_secret
```

若需要啟用 OAuth 登入、推播等完整功能，請見下方[環境變數](#環境變數)與[身份驗證與-oauth-設定](#身份驗證與-oauth-設定)。

### 初始化並同步資料

初始化資料庫，並從 TDX 同步路線／站牌／線型等靜態資料：

```bash
python -m app.sync_static
```

預設會同步 `TDX_CITIES` 設定中的所有城市（未設定時為全台縣市）。同步完成後：

- 主資料庫 `./bus.db` 保留完整資料（`routes`、`paths`、`stops`、`path_points`）
- `./downloads/bus.db` 為精簡後的路線目錄下載檔（`routes` 含彙總 `path_name`，`paths` 含各方向metadata）
- `./downloads/{City}.db` 只含該城市的 `stops`（不含 `routes`／`paths`／`path_points`）

只同步特定城市：

```bash
python -m app.sync_static --cities Taipei,NewTaipei
```

強制完整刷新並強制版本號遞增：

```bash
python -m app.sync_static --cities Taichung --force
```

抓取單一路線的即時資料並印出 JSON（用於除錯）：

```bash
python -m app.sync_realtime --routeid TPE307
```

`routeid` 為 TDX 的 `SubRouteUID`（例如 `TPE307`）。

#### 用 admin API 手動觸發同步

排程固定在每週一 04:00。若部署環境沒有 shell（只能啟動／關閉服務），可以用 admin
端點觸發 —— **重新啟動伺服器本身不會觸發同步**，開機只會重建 download db 與版本號。

```bash
# 全量同步
curl -X POST https://bus.avianjay.sbs/api/v1/admin/static-sync \
  -H "Authorization: Bearer <admin token>"

# 只同步特定縣市（公路客運一律會跟著跑）
curl -X POST https://bus.avianjay.sbs/api/v1/admin/static-sync \
  -H "Authorization: Bearer <admin token>" \
  -H "Content-Type: application/json" \
  -d '{"cities": ["Hsinchu"]}'

# 查詢進度
curl https://bus.avianjay.sbs/api/v1/admin/static-sync \
  -H "Authorization: Bearer <admin token>"
```

也接受登入用的 `yabus_auth_token` cookie，所以用 admin 帳號登入網站後，直接在瀏覽器
主控台執行即可：

```js
await (await fetch('/api/v1/admin/static-sync', {method: 'POST'})).json()
await (await fetch('/api/v1/admin/static-sync')).json()   // 查進度
```

行為說明：

- 立刻回傳 `202`，實際工作交給既有的排程執行緒 —— 手動同步與週一 04:00 的排程**不可能
  同時跑**（`sync_static` 會換掉 `bus.db.tmp`，並行會互相破壞）。
- 已經在排隊或執行中時再次觸發會得到 `409`。
- 狀態端點回傳 `state`（`idle` / `queued` / `running`）、`started_at`、`finished_at`、
  `duration_seconds`、`last_error`。
- `cities` 只接受 TDX 正式縣市名稱，打錯會回 `422` 而不是安靜地變成 no-op。
- `force` 會強制所有資料庫跳版本（含沒變動的），一般不要用。

#### 去返程合併

部分業者（公路客運／THB 與多數縣市）會把同一條路線的每個方向各給一個
`SubRouteUID`，例如 `THB181501`（去程）與 `THB181502`（返程），同屬
`RouteUID` = `THB1815`。這會讓同一條路線在搜尋結果出現兩筆。

`sync_static` 會依 `(RouteUID, SubRouteName)` 把方向兄弟合併成一條路線、兩個
`pathid`，與六都本來的資料形狀一致。`pathid` 維持等於 TDX 的 `Direction`。

- 存活下來的 `routeid` 是方向最小的成員（通常就是去程），另一個成為別名。
- 別名記錄在 `route_subroutes` 表（`subroute_uid` → `routeid`），單獨成組的路線
  也會有一筆 identity 列。
- 路線詳情類 API 收到別名 `routeid` 時會自動導向存活的路線，並在回應中回傳
  正規 `routeid`，同時附上 `X-Route-Alias-Resolved` 標頭。
- 即時到站會用 `route_subroutes` 展開查詢，確保兩個方向都拿得到資料。
- 字母變體（`1815A`~`1815G`）與環狀線不會被合併；台南把方向寫進 `SubRouteName`，
  因此依資料自然不會被合併。
- 可用 `STATIC_MERGE_DIRECTIONS=false` 關閉合併（產出與合併前位元相同的列）。

合併後若要修正既有雲端收藏中已被吸收的 `routeid`：

```bash
python -m app.migrate_favorite_routeids --dry-run
python -m app.migrate_favorite_routeids
```

### 啟動伺服器

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

啟動後可於 `/info/docs`（Swagger UI）或 `/info/redoc`（ReDoc）瀏覽完整、即時的 API 規格。

## 環境變數

### TDX 資料來源

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `TDX_CLIENT_ID` | TDX API Client ID（**必填**） | — |
| `TDX_CLIENT_SECRET` | TDX API Client Secret（**必填**） | — |
| `TDX_CITIES` | 逗號分隔的同步城市清單 | 全部支援的 `CityBus` 縣市 |
| `TDX_BASE_URL` | TDX API 基礎網址 | `https://tdx.transportdata.tw/api/basic` |
| `TDX_TOKEN_URL` | TDX OAuth token 端點 | TDX 官方 token URL |
| `TDX_REQUEST_TIMEOUT` | 上游請求逾時秒數 | `30` |
| `TDX_TOKEN_REFRESH_SKEW` | 權杖到期前幾秒預先更新 | `300` |
| `TDX_RETRY_ATTEMPTS` | `429`／`5xx` 最大重試次數 | `6` |
| `TDX_RETRY_BACKOFF` | 重試的基礎退避秒數 | `2.0` |
| `TDX_MIN_REQUEST_INTERVAL` | 每次 TDX 請求間的最小間隔秒數 | `0.5` |
| `STATIC_MERGE_DIRECTIONS` | 是否把每方向各一個 `SubRouteUID` 的路線合併成單一路線 | `true` |

### 資料庫與快取

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `BUS_DB_PATH` | 主資料庫路徑（完整資料） | `./bus.db` |
| `BUS_DOWNLOAD_DB_PATH` | 下載用路線目錄資料庫路徑 | `./downloads/bus.db` |
| `BUS_APP_DB_PATH` | 應用程式資料庫路徑（帳號、分析、公告、回饋、同步資料，與靜態資料庫分離） | `bus.db` 同目錄下的 `app.db` |
| `REALTIME_CACHE_TTL` | 記憶體內即時資料快取秒數 | `5` |
| `REALTIME_TRACK_TTL` | 同城市即時查詢的批次時窗秒數 | `30` |

### 網路與部署

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `CORS_ORIGINS` | 逗號分隔的允許 CORS 來源 | *(空，停用 CORS)* |
| `CLOUDFLARED_TUNNEL_TOKEN` | 設定後會在啟動時自動建立 Cloudflare Tunnel | *(未設定)* |

### 身份驗證（OAuth）

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `AUTH_PUBLIC_BASE_URL` | API 伺服器的對外網址（OAuth callback 會以此為基準） | `https://bus.avianjay.sbs` |
| `AUTH_STATE_TTL_SECONDS` | OAuth state 有效秒數 | `600` |
| `AUTH_SNOWFLAKE_NODE_ID` | 帳號 ID 產生用的 Snowflake node ID | `0` |
| `DISCORD_OAUTH_CLIENT_ID` | Discord OAuth Client ID | — |
| `DISCORD_OAUTH_CLIENT_SECRET` | Discord OAuth Client Secret | — |
| `GOOGLE_OAUTH_CLIENT_ID` | Google OAuth Web Client ID | — |
| `GOOGLE_OAUTH_CLIENT_SECRET` | Google OAuth Web Client Secret | — |
| `GOOGLE_NATIVE_OAUTH_CLIENT_IDS` | 逗號分隔，Android／iOS 原生 Google Sign-In 允許的 ID token `aud` | *(空)* |
| `APP_PUBLIC_BASE_URL` | App／Web 前端的對外網址 | `https://busapp.avianjay.sbs` |

`GOOGLE_OAUTH_CLIENT_ID` 也會被接受為原生 Google ID token 的合法 audience，方便 App 以 Web Client ID 作為 `serverClientId` 使用。

### 推播通知（Firebase Cloud Messaging）

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `FCM_PROJECT_ID` | Firebase 專案 ID | `yabus-111c1` |
| `FCM_SERVICE_ACCOUNT_JSON` | FCM 服務帳戶 JSON 內容（伺服器端發送推播用） | — |
| `FCM_SERVICE_ACCOUNT_JSON_PATH` | FCM 服務帳戶 JSON 檔案路徑（與上者擇一） | — |
| `FCM_WEB_API_KEY` | Web push 用的 Firebase API Key | 專案預設值 |
| `FCM_WEB_AUTH_DOMAIN` | Firebase Auth Domain | 專案預設值 |
| `FCM_WEB_STORAGE_BUCKET` | Firebase Storage Bucket | 專案預設值 |
| `FCM_WEB_MESSAGING_SENDER_ID` | Firebase Messaging Sender ID | 專案預設值 |
| `FCM_WEB_APP_ID` | Firebase Web App ID | 專案預設值 |
| `FCM_WEB_MEASUREMENT_ID` | Firebase Measurement ID | 專案預設值 |
| `FCM_WEB_VAPID_KEY` | Web push 訂閱用的 VAPID key | *(空)* |

`FCM_WEB_*` 屬於公開的用戶端設定值，會透過 `GET /api/v1/push/public-config` 提供給前端；`FCM_SERVICE_ACCOUNT_JSON`／`FCM_SERVICE_ACCOUNT_JSON_PATH` 才是需要保密的伺服器端憑證。

### 帳號雲端同步

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `ACCOUNT_SYNC_MAX_PAYLOAD_BYTES` | 單一同步文件的最大位元組數 | `524288`（512 KiB） |
| `ACCOUNT_SYNC_MAX_FAVORITES` | 單一帳號最多可同步的收藏數量 | `25` |
| `ACCOUNT_SYNC_MAX_GROUP_NAME_LENGTH` | 收藏群組名稱最大長度 | `120` |
| `ACCOUNT_SYNC_MAX_JSON_DEPTH` | 同步 payload 允許的最大 JSON 巢狀深度 | `16` |

### 意見回饋

| 變數 | 說明 | 預設值 |
| --- | --- | --- |
| `FEEDBACK_DISCORD_WEBHOOK_URL` | 新意見回饋通知用的 Discord Webhook（選用） | — |

新回饋通知只會包含 metadata 與後台連結，不會把使用者填寫的標題／內容原文送到 Discord。

## API 端點

所有端點皆掛在同一個 FastAPI 應用下，完整、即時的請求／回應規格請直接查看：

- `GET /info/docs` — Swagger UI
- `GET /info/redoc` — ReDoc
- `GET /info/openapi.json` — OpenAPI schema

以下依模組列出主要端點：

<details>
<summary><strong>公車（Bus）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/downloads/bus.db` | 下載路線目錄資料庫 |
| GET | `/downloads/{name}.db` | 下載指定城市的站牌資料庫（如 `Taipei`） |
| GET | `/api/v1/routes` | 跨城市搜尋路線 |
| GET | `/api/v1/cities/{city}/routes` | 依城市搜尋路線 |
| GET | `/api/v1/cities/{city}/stops/nearby` | 查詢城市內鄰近站牌 |
| GET | `/api/v1/routes/{routeid}/realtime` | 單一路線即時到站 |
| GET | `/api/v1/batchroutes/{routeids}/realtime` | 批次查詢多條路線即時到站（同城市共用一次 TDX 請求） |
| GET | `/api/v1/routes/{routeid}/realtime/buses` | 路線上所有公車目前位置 |
| GET | `/api/v1/cities/{city}/buses` | 整個城市的公車即時位置（全公車地圖）|
| GET | `/api/v1/routes/{routeid}/stops` | 路線站序清單 |
| GET | `/api/v1/routes/{routeid}/paths/{pathid}/points` | 路線線型座標點 |
| GET | `/api/v1/routes/{routeid}/schedule` | 路線班表 |
| GET | `/api/v1/routes/{routeid}/operators` | 路線所屬客運業者 |
| GET | `/api/v1/routes/{routeid}/stop-estimated-times` | 各站牌估計行車時間 |
| GET | `/api/v1/routes/{routeuid}/alerts` | 路線警示資訊 |
| GET | `/api/v1/stops/{stopid}/passby` | 會停靠指定站牌的路線與預估到站時間 |
| GET | `/api/v1/holidays` | 假日資訊（用於判斷是否採用假日班表） |
| GET | `/api/v1/database/{name}/version` | 資料庫版本查詢（`main`／`download`／城市名稱） |

</details>

<details>
<summary><strong>捷運（Metro）</strong> — 前綴 <code>/api/v1/metro</code></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/systems` | 支援的捷運系統清單 |
| GET | `/{system}/lines` | 路線清單 |
| GET | `/{system}/stations` | 車站清單 |
| GET | `/{system}/station-of-line` | 路線行經車站 |
| GET | `/{system}/lines/{line_id}/liveboard` | 列車到站看板 |
| GET | `/{system}/lines/{line_id}/eta` | 列車到站預估時間 |
| GET | `/{system}/lines/{line_id}/shape` | 路線線型 |
| GET | `/{system}/frequency` | 班距資訊 |
| GET | `/{system}/s2s-traveltime` | 站間行駛時間 |
| GET | `/{system}/station-timetable` | 車站時刻表 |

</details>

<details>
<summary><strong>台鐵／高鐵（Rail）</strong> — 前綴 <code>/api/v1</code></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/thsr/stations` | 高鐵車站清單 |
| GET | `/thsr/timetable/od` | 高鐵起訖站時刻表 |
| GET | `/thsr/timetable/today` | 高鐵當日時刻表 |
| GET | `/thsr/seats/{station_id}` | 高鐵座位供需狀況 |
| GET | `/thsr/alerts` | 高鐵行車警示 |
| GET | `/thsr/shape` | 高鐵路線線型 |
| GET | `/tra/stations` | 台鐵車站清單 |
| GET | `/tra/lines` | 台鐵路線清單 |
| GET | `/tra/station-of-line` | 台鐵各路線的站序清單 |
| GET | `/tra/timetable/od` | 台鐵起訖站時刻表 |
| GET | `/tra/liveboard/{station_id}` | 台鐵車站即時到站看板 |
| GET | `/tra/train-positions/{station_id}` | 台鐵列車位置 |
| GET | `/tra/shape` | 台鐵路線線型 |
| GET | `/tra/alerts` | 台鐵行車警示 |

</details>

<details>
<summary><strong>YouBike／公共自行車（Bike）</strong> — 前綴 <code>/api/v1/bike</code></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/cities` | 支援公共自行車系統的城市 |
| GET | `/stations` | 站點與即時可借還車輛數 |
| GET | `/nearby` | 依座標查詢鄰近自行車站 |

</details>

<details>
<summary><strong>身份驗證（Auth）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/auth/discord-start` | 開始 Discord OAuth 流程 |
| GET | `/api/v1/auth/google-start` | 開始 Google OAuth 流程 |
| GET | `/api/v1/auth/discord-callback` | Discord OAuth callback |
| GET | `/api/v1/auth/google-callback` | Google OAuth callback |
| POST | `/api/v1/auth/google-native` | 原生 Android／iOS Google Sign-In（驗證 ID token） |
| GET | `/api/v1/auth/me` | 取得目前登入帳號資訊 |
| GET | `/api/v1/auth/devices` | 列出目前帳號已登入的裝置 |
| POST | `/api/v1/auth/link/discord-start` | 將 Discord 連結到目前帳號 |
| POST | `/api/v1/auth/link/google-start` | 將 Google 連結到目前帳號 |
| GET | `/api/v1/auth/link/pending` | 查詢待確認的連結請求 |
| POST | `/api/v1/auth/link/confirm` | 確認帳號連結 |
| POST | `/api/v1/auth/logout` | 登出目前裝置 |
| POST | `/api/v1/auth/logout-all` | 登出目前帳號的所有裝置 |

</details>

<details>
<summary><strong>帳號雲端同步（Account Sync）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/account/sync` | 取得所有同步命名空間的狀態摘要 |
| GET | `/api/v1/account/sync/{namespace}` | 取得指定命名空間的內容（如收藏） |
| PUT | `/api/v1/account/sync/{namespace}` | 上傳／合併同步內容（支援 `base_revision`／`base_etag` 樂觀鎖與衝突策略） |

</details>

<details>
<summary><strong>推播（Push）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/push/public-config` | 取得公開的 FCM Web 設定 |
| POST | `/api/v1/push/fcm-token` | 註冊裝置的 FCM token |

</details>

<details>
<summary><strong>公告（Announcements）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/announcements` | 依平台／版本取得有效公告 |
| GET | `/api/v1/announcements/all` | 取得全部公告，含已過期（mod／admin） |
| POST | `/api/v1/announcements` | 建立公告（mod／admin） |
| PATCH | `/api/v1/announcements/{announcement_id}` | 更新公告（mod／admin） |
| GET | `/admin/announcements` | 公告管理後台頁面 |

</details>

<details>
<summary><strong>意見回饋（Feedback）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| POST | `/api/v1/feedback` | 提交意見回饋 |
| GET | `/api/v1/admin/feedbacks` | 意見回饋清單（admin） |
| GET | `/admin/feedbacks` | 意見回饋管理後台頁面 |

</details>

<details>
<summary><strong>管理與分析（Admin / Analytics）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/admin/users` | 使用者清單（admin） |
| PATCH | `/api/v1/admin/users/{account_id}` | 變更使用者角色（admin） |
| POST | `/api/v1/admin/users/{account_id}/logout-all` | 撤銷該使用者所有裝置登入（admin） |
| GET | `/api/v1/admin/analytics` | 請求分析彙總（admin） |
| GET | `/admin/user_manage` | 使用者管理後台頁面 |
| GET | `/admin/analytics` | 分析儀表板頁面 |

</details>

<details>
<summary><strong>法律頁面（Legal）</strong></summary>

| Method | Path | 說明 |
| --- | --- | --- |
| GET | `/api/v1/terms-of-service` | 服務條款（JSON） |
| GET | `/api/v1/privacy-policy` | 隱私權政策（JSON） |
| GET | `/terms-of-service` | 服務條款頁面 |
| GET | `/privacy-policy` | 隱私權政策頁面 |

</details>

### 公車即時到站回應格式

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime
```

每個站牌物件包含：

- `eta`：該站最近一班的到站秒數（向下相容欄位）
- `message`：站牌狀態文字（向下相容；`eta` 有值時通常為空）
- `buses`：預估最快抵達本站的車輛車牌（每輛車只會出現一次）
  - `source`：`tdx`（原生逐站到站資料）或 `backfill_buses`（原本在 TDX 到站資料中出現、後來消失的車輛，暫時以 `/realtime/buses` 車輛位置回補）
- `etas`：本站所有有效預估到站清單，每筆包含：
  - `plate`：車牌（若有）
  - `eta`：預估到站秒數
  - `is_arriving`：是否標記為即將進站
  - `source`：同上，`tdx` 或 `backfill_buses`
  - `estimated`：是否為由車輛位置回推合成的到站時間，而非 TDX 直接提供

車輛位置查詢：

```bash
curl http://127.0.0.1:8000/api/v1/routes/TPE307/realtime/buses
```

```json
[
  {
    "id": "ABC-1234",
    "direction": 0,
    "lat": 25.0478,
    "lon": 121.5319,
    "speed": 32,
    "azimuth": 120,
    "status": 0,
    "time": 1712654400
  }
]
```

所有 API 回傳的時間戳皆為 Unix timestamp（秒）。

## 身份驗證與 OAuth 設定

此服務僅支援 OAuth 登入（Discord / Google），沒有帳號密碼登入機制。

### Discord OAuth App

在 [Discord Developer Portal](https://discord.com/developers/applications) 建立應用程式，並新增以下 Redirect：

```text
https://bus.avianjay.sbs/api/v1/auth/discord-callback
```

主機需與 `AUTH_PUBLIC_BASE_URL` 一致。所需 scope：

```text
identify email
```

### Google OAuth Clients

建立一個 **Web** OAuth client 供 API 伺服器使用，並新增以下 Authorized redirect URI：

```text
https://bus.avianjay.sbs/api/v1/auth/google-callback
```

主機需與 `AUTH_PUBLIC_BASE_URL` 一致。所需 scope：

```text
openid email profile
```

Web client 的 ID／Secret 對應到 `GOOGLE_OAUTH_CLIENT_ID`／`GOOGLE_OAUTH_CLIENT_SECRET`；Web client ID 也可以傳給 Flutter App 作為 `YABUS_GOOGLE_WEB_CLIENT_ID`（`serverClientId`），但 Secret 絕對不能放進 App。不需要把 `yabus://auth-callback` 加入 Google Cloud——Google 只需要上述 API callback，App 端的最終導向由 API 伺服器在交換完授權碼後處理。

若要支援 Android 原生 Google Sign-In，額外建立一個 Android OAuth client：

```text
Package name: tw.avianjay.taiwanbus.flutter
SHA-1:        debug／release 簽章憑證指紋
```

若要支援 iOS 原生 Sign-In，額外建立一個 iOS OAuth client：

```text
Bundle ID: tw.avianjay.taiwanbus.flutter
```

再把 Google Cloud（或 `GoogleService-Info.plist`）提供的 iOS reversed client ID 加入 `ios/YABus/Info.plist` 的 `CFBundleURLTypes`，並保留原有的 `yabus` URL scheme：

```xml
<dict>
  <key>CFBundleTypeRole</key>
  <string>Editor</string>
  <key>CFBundleURLSchemes</key>
  <array>
    <string>com.googleusercontent.apps.your-ios-reversed-client-id</string>
  </array>
</dict>
```

Android／iOS 的 OAuth client 不需要設定 Authorized redirect URI。Android client ID 也不需要寫進 Dart 程式碼，Google Cloud 會依 package name 與 SHA 指紋自動比對。

### Redirect 白名單

API 伺服器只接受導向以下位址：

```text
yabus://...
https://busapp.avianjay.sbs/...
```

登入成功／失敗的資料會放在 URL 的 fragment 中，例如：

```text
yabus://auth-callback#token=...&account_id=...&device_id=...&role=user
```

使用 fragment 是為了避免 token 以一般 query string 的形式被送到 Web callback 伺服器。

### Token 與速率限制

Token 格式：

```text
base64(snowflake).base64(timestamp).random_secret
```

伺服器只保存 `sha256(token)`，不會保存原始 token。每個裝置只有一個有效 token；同一裝置重新登入會撤銷該裝置先前的 token。

全域速率限制（涵蓋所有 API 端點，非個別端點各自計算）：

```text
已登入：每帳號 ID 每 60 秒最多 60 次請求
未登入：每個用戶端 IP 每 60 秒最多 60 次請求
```

超過限制會回傳 `429`，並附上 `Retry-After` 標頭。

唯一的例外是全公車地圖 `/api/v1/cities/{city}/buses`：地圖每 15 秒左右輪詢一次，
若計入全域額度會吃掉其他功能的配額，因此它有自己的 `city-buses` 額度
（預設每 60 秒 30 次，`CITY_BUSES_RATE_LIMIT_REQUESTS` 可調），不佔用上面的 60 次。

### 全公車地圖設定

一個城市的即時車位只會向 TDX 抓一次並快取，所有客戶端共用同一份快照，
上游負載由 TTL 決定而不是由開著地圖的人數決定。

| 環境變數 | 預設 | 說明 |
| --- | --- | --- |
| `CITY_BUSES_CACHE_TTL` | `15` | 每個城市的快照保鮮秒數（多 worker 部署請乘上 worker 數）|
| `CITY_BUSES_STALE_MAX_SECONDS` | `120` | 上游失敗後還願意續供舊快照的上限，超過就回 502 讓客戶端退避 |
| `CITY_BUSES_PAGE_SIZE` | `1000` | 未過濾查詢的 `$top`（實測台北 777、新北 633、台中 328 筆一頁可拿完）|
| `CITY_BUSES_RATE_LIMIT_REQUESTS` | `30` | 地圖專屬額度，每 60 秒 |

日誌中值得警戒的兩行：`city buses page cap detected`（TDX 悄悄截斷分頁）與
`city buses item count dropped sharply`（車輛數異常腰斬）。

### 角色與權限

角色儲存於 `accounts.role`，可為：

```text
admin
mod
user
```

新帳號預設為 `user`。已有管理員後，可透過管理後台（`/admin/user_manage`）或 `PATCH /api/v1/admin/users/{account_id}` 升級其他帳號；但**建立第一個管理員**仍須直接在 SQLite 操作：

```sql
UPDATE accounts SET role = 'admin', updated_at = strftime('%s', 'now')
WHERE id = 123;
```

### Flutter App 端設定

App 端**不需要** Discord 或 Google 的 client secret，請勿把 secret 放進 Flutter。公開的 Google client ID 可以放在 App 內供原生 Android／iOS Google Sign-In 使用。

正式環境的預設值已經寫死在 App 中：

```text
YABUS_API_BASE_URL=https://bus.avianjay.sbs
YABUS_APP_AUTH_REDIRECT_URI=yabus://auth-callback
YABUS_WEB_AUTH_REDIRECT_URI=https://busapp.avianjay.sbs/
```

Build 時可覆寫：

```bash
flutter build windows --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
flutter build macos --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
flutter build apk --dart-define=YABUS_API_BASE_URL=https://bus.avianjay.sbs
```

可選的 redirect 覆寫：

```bash
--dart-define=YABUS_APP_AUTH_REDIRECT_URI=yabus://auth-callback
--dart-define=YABUS_WEB_AUTH_REDIRECT_URI=https://busapp.avianjay.sbs/
```

Android／iOS 原生 Google Sign-In 用的 defines：

```bash
--dart-define=YABUS_GOOGLE_WEB_CLIENT_ID=web-client-id.apps.googleusercontent.com
--dart-define=YABUS_GOOGLE_IOS_CLIENT_ID=ios-client-id.apps.googleusercontent.com
```

`YABUS_GOOGLE_WEB_CLIENT_ID` 會作為 Google Sign-In 的 `serverClientId`；Android 不需要在 Dart 中設定 Android client ID（由 package name 與 SHA 指紋比對），iOS 則使用 `YABUS_GOOGLE_IOS_CLIENT_ID` 作為 App client ID。

各平台登入方式：

```text
Android: App 內原生 Google Sign-In
iOS:     App 內原生 Google Sign-In
Web:     瀏覽器 OAuth 流程
Desktop: 瀏覽器 OAuth 流程
Discord: 所有平台皆為瀏覽器 OAuth 流程
```

App 首次啟動時會產生一組 UUIDv4 裝置金鑰並存於本機持久化儲存；重新安裝 App 會視為新裝置。App 不會讀取 MAC 位址、序號或其他硬體指紋。

### 桌面應用程式 URL Scheme

macOS 與行動裝置的 URL scheme 已在 App 專案內設定完成。

Windows 封裝版會在 NSIS 安裝程式中註冊 `yabus://` protocol：

```text
HKCU\Software\Classes\yabus
```

Linux `.deb` 套件內含：

```text
MimeType=x-scheme-handler/yabus;
Exec=yabus %u
```

安裝後腳本會刷新桌面資料庫並嘗試設定為預設處理程式。AppImage 的 URL scheme 是否自動註冊視系統的桌面整合工具而定；若未自動整合，可改安裝 `.deb` 套件，或手動註冊內附的 `.desktop` 項目。

App 進程會以參數形式收到完整的 callback URL；Dart 進入點已會解析啟動參數中的 `yabus://auth-callback#...`。

在 Windows 上進行本機開發、尚未透過安裝程式安裝時，可手動註冊 debug 或 release 執行檔：

```powershell
reg add HKCU\Software\Classes\yabus /ve /d "URL:YABus Protocol" /f
reg add HKCU\Software\Classes\yabus /v "URL Protocol" /d "" /f
reg add HKCU\Software\Classes\yabus\shell\open\command /ve /d "\"D:\yetanotherbusapp\build\windows\x64\runner\Debug\YetAnotherBusApp.exe\" \"%1\"" /f
```

若測試的是 release build，記得改成對應的執行檔路徑。

### App 帳號頁面

App 的「設定」中有一個帳號頁面，可用來：

```text
使用 Discord 登入
使用 Google 登入
將 Discord 或 Google 連結到目前的裝置帳號
重新整理已連結的登入方式狀態
只登出目前這台裝置
```

## 資料同步與版本控制

- TDX 身份驗證採用 `client_credentials`；access token 會快取在記憶體中，接近到期前才重新取得
- 靜態資料同步使用 TDX 的 `Last-Modified`／`If-Modified-Since` 做條件式請求；同步狀態記錄在 `tdx_fetch_state`
- `--force` 會停用 `If-Modified-Since`，並強制該次同步的資料庫版本遞增
- 若一個城市的三項靜態資源（`Route`、`StopOfRoute`、`Shape`）皆回傳 `304`，該城市會被跳過
- 靜態同步以「路線」為單位做原子性替換
- 伺服器啟動時**不會**自動執行靜態同步，但會在**每週一凌晨 04:00（伺服器本機時間）**自動執行一次
- 資料庫版本記錄在 `database_versions`，並附帶內容雜湊；版本從 `1` 開始，只有在追蹤的資料表內容變動時才遞增
- 支援查詢版本的名稱：`main`、`download`，以及各城市名稱（如 `Taichung`）
- 即時資料快取在伺服器記憶體中；同一城市內的即時查詢會依 `REALTIME_TRACK_TTL` 批次成一次 TDX `$filter` 查詢，涵蓋該城市當下所有被追蹤的路線（但只回傳被請求的那條路線，其餘路線的快取會一併更新）
- 即時批次抓取的狀態同樣存於 `tdx_fetch_state`，key 為 `realtime_eta:{city}:...` 與 `realtime_buses:{city}:...`
- 新北市（NewTaipei）的公車即時資料除了 TDX 外，也會輔以新北市政府開放資料平台作為補充來源

## 記錄與監控

- Runtime 日誌寫入 `./logs/app.log`，每日輪替，超過 7 天自動清除
- 日誌內容包含本機時間、等級與 logger 名稱
- 每個 API 請求會記錄到應用程式資料庫（`app.db`）的 `request_analytics`
- App 端會傳送 `YABus/version-commitHash (Platform)` 格式的 User-Agent，會與一般瀏覽器 User-Agent 分開解析
- `GET /api/v1/admin/analytics` 提供彙總後的請求分析（僅限 admin）
- `GET /admin/analytics` 提供對應的視覺化儀表板頁面

## 測試

專案內含大量以 `TestClient` 撰寫的測試檔（`test_*.py`）。測試相依套件（例如 `pytest`）未列在 `requirements.txt` 中，需另外安裝：

```bash
pip install pytest
pytest
```

## 隱私權與服務條款

服務的隱私權政策與服務條款分別在 [PRIVACY.md](PRIVACY.md) 與 [TERMS.md](TERMS.md)，部署後也可透過 `GET /privacy-policy` 與 `GET /terms-of-service` 存取對應頁面。

## Acknowledgements

- TDX 運輸資料流通服務
- 新北市政府資料開放平台
- FastAPI
- Discord、Google（OAuth 提供者）
- Firebase Cloud Messaging
