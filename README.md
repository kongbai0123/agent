# Local AI Workbench

Windows 本機優先的 AI 聊天工作台。現行版本以 Basic Chat 為主體，並提供可選的 Hermes sidecar、專案隔離的 Project Skills、附件、Session、輸出面板，以及受治理的本機 n8n／Gmail 整合。

目前開發版本：`0.8.0-n8n-graph-authoring-beta.1`

## 目前功能

- Ollama 與 OpenAI-compatible 模型連線
- 專案預設根目錄 `<repository>\projects`（此工作站為 `D:` 資料碟）
- 每個 Project Skill 僅屬於單一專案，避免跨專案載入與名稱碰撞
- Skill 專案啟停與 Session 模式：依專案、本對話、下一輪、不使用
- 右側「輸出內容」面板顯示目前專案 Skills
- Hermes 以獨立 loopback sidecar 漸進接入，不取代 Workbench UI
- Hermes 生產化監控、熔斷、回退與 Canary → 5% → 25% → 50% → 全量 rollout
- Hermes Project Skills 工具限 Docker、單一專案、唯讀政策
- Sidebar「流程」提供受管理的本機 n8n 生命週期、Gmail 草稿與人工核准
- Agent 可先提出 2–3 個 n8n 架構，再由固定版本 Node Catalog 與伺服器端編譯器配對、設定及連接官方內建節點
- 選定方案必須先 materialize 成通過驗證的節點圖；更新既有流程使用語意 Patch，不由模型整份覆蓋 Workflow JSON
- 右側檢查器顯示節點／連線 Diff、分支、Credential alias、外部目標、風險及不可變 digest；使用者核准後才由 Broker 建立未啟用草稿
- 受保護的 Workbench Agent Bridge、Credential alias 與執行時核准邊界已完成；模型看不到 n8n Credential ID 或 Secret
- n8n API Key、Gmail OAuth 與郵件密文只保存在本機安全邊界，不會提供給模型
- 執行資料、對話、附件、資料庫、模型與本機設定不納入 Git

## 目錄

```text
backend/      FastAPI、Basic Chat、Project Skills、Hermes、n8n Broker 與資料存取
frontend/     Workbench UI、流程工作區與浮動檢查器
config/       Hermes 與 n8n 固定版本、安全政策及 Workflow 模板
scripts/      Windows 啟動、更新、Hermes／n8n 安裝與維運工具
tests/        離線、可重現的契約與整合測試
docs/         現行維運與安全說明
launcher/     Windows 啟動器原始碼
runtime/      本機執行資料（Git 排除）
projects/     使用者專案資料（Git 排除）
```

## Windows 啟動

需求：Windows 10/11、Python 3.11、Node.js 20（開發檢查）；啟用受管理 n8n 時另使用固定的 Node.js 24.15.0 與 n8n 2.32.5。模型可使用 Ollama 或其他 OpenAI-compatible 服務。

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r backend\requirements.lock
Copy-Item backend\settings.json.example backend\settings.json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_workbench.ps1
```

啟動器會將主 UI 綁定至 loopback 位址，建立當次 Session 驗證資訊，並將執行資料存到 `runtime/`。若 8000 或 8080 已被占用，會從受控候選埠選擇可用埠。

## Project Skills

Skill 由專案 ID 隔離，實體資料位於：

```text
runtime/projects/<project_id>/skills/<skill_slug>/
```

它不會寫入使用者連結的唯讀專案根目錄。聊天只會載入目前 Session 綁定專案內已啟用的 Skill；相同 slug 可存在於不同專案而不互相覆蓋。

Session 可選擇：

- `依專案`：跟隨專案預設啟停狀態
- `本對話`：只在目前 Session 啟用
- `下一輪`：只在下一次建立 prompt 時啟用一次
- `不使用`：只在目前 Session 停用

## Hermes

Hermes 是可選服務。預設關閉；未安裝、健康檢查失敗、能力不符、監控不可用或熔斷器開啟時，Workbench 會 fail closed 並回到 Basic Chat。

生產操作、升級條件、監控與回退方式請參閱 [Hermes 生產化手冊](docs/HERMES_PRODUCTION_RUNBOOK.md)。

## n8n 與 Gmail

n8n 是獨立的本機 loopback 服務。Workbench 不會讓 Agent 直接取得 n8n API Key、Gmail OAuth Token 或管理介面權限；Agent 只建立 Project／Session 綁定的結構化提案，伺服器重新計算 Workflow 快照、Diff、風險與 digest，人工核准後才由 Broker 執行。Gmail V1 的固定收件者由本機 `WORKBENCH_N8N_GMAIL_RECIPIENT` 提供，不寫入公開原始碼；未設定時郵件整合會安全停用。

`0.8.0-n8n-graph-authoring-beta.1` 已完成 Node Catalog、Workflow Spec 編譯、materialize、語意 Patch、伺服器權威 Diff、受保護 Agent／Approval Bridge、Project-scoped Credential alias、Agent task runtime 與執行時核准。建立草稿、發布、啟用及每次外部寫入仍是不同的核准邊界。

兩個 Bridge 範本只會以受保護 JSON 與驗證器隨版本提供；Workbench／Launcher 不會自動匯入或發布。正式使用前仍須由受控部署流程綁定 HMAC Credential、匯入並發布兩個只有 Execute Workflow Trigger 的受保護子流程，設定其 Workflow ID 並完成 canary。n8n `2.32.5` 以 `active` 表示已發布；這些子流程沒有排程或 Webhook，不會自行啟動。使用者建立的流程草稿仍保持未啟用，直到另行核准發布。

預設採限制權限。Code、Execute Command、檔案系統、Community／Custom Node 及缺少隔離 Runner 的高風險節點均 fail closed；系統管理的 Gmail 與 Bridge Workflows 永遠受保護。安裝、部署、操作與故障處理請參閱 [n8n Agent 治理手冊](docs/N8N_AGENT_GOVERNANCE.md)。

## 驗證

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend scripts
.\.venv\Scripts\python.exe -m pytest tests -q
Get-ChildItem frontend -Filter *.js | ForEach-Object { node --check $_.FullName }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_public_tree.ps1
```

GitHub Actions 使用同一份 hashed dependency lock，並上傳 JUnit 與 dependency audit 結果。

## 資料與秘密邊界

下列內容只留在本機，不應提交：

- `backend/settings.json`
- `.env*`、API keys、私鑰
- `runtime/`、`projects/`、`workspaces/`、`artifacts/`
- SQLite／DB、log、對話、附件與模型檔

公開版本樹檢查由 `scripts/check_public_tree.ps1` 執行。設定範本只保留安全預設與環境變數名稱，不包含實際金鑰。

## 相關文件

- [Hermes 生產化與分批啟用](docs/HERMES_PRODUCTION_RUNBOOK.md)
- [n8n Agent 治理與 Gmail 整合](docs/N8N_AGENT_GOVERNANCE.md)
- [依賴安全稽核](docs/DEPENDENCY_AUDIT.md)
- [Windows 啟動器與更新器](docs/WINDOWS_LAUNCHER_AND_UPDATER.md)
