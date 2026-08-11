# Local AI Workbench

Windows 本機優先的 AI 聊天工作台。現行版本以 Basic Chat 為主體，並提供可選的 Hermes sidecar、專案隔離的 Project Skills、附件、Session 與輸出面板。

## 目前功能

- Ollama 與 OpenAI-compatible 模型連線
- 專案預設根目錄 `<repository>\projects`（此工作站為 `D:` 資料碟）
- 每個 Project Skill 僅屬於單一專案，避免跨專案載入與名稱碰撞
- Skill 專案啟停與 Session 模式：依專案、本對話、下一輪、不使用
- 右側「輸出內容」面板顯示目前專案 Skills
- Hermes 以獨立 loopback sidecar 漸進接入，不取代 Workbench UI
- Hermes 生產化監控、熔斷、回退與 Canary → 5% → 25% → 50% → 全量 rollout
- Hermes Project Skills 工具限 Docker、單一專案、唯讀政策
- 執行資料、對話、附件、資料庫、模型與本機設定不納入 Git

## 目錄

```text
backend/      FastAPI、Basic Chat、Project Skills、Hermes 與資料存取
frontend/     Workbench UI 與輸出 Skills 面板
config/       Hermes 固定版本與安全政策模板
scripts/      Windows 啟動、更新、Hermes 安裝與維運工具
tests/        離線、可重現的契約與整合測試
docs/         現行維運與安全說明
launcher/     Windows 啟動器原始碼
runtime/      本機執行資料（Git 排除）
projects/     使用者專案資料（Git 排除）
```

## Windows 啟動

需求：Windows 10/11、Python 3.11、Node.js 20（僅開發檢查需要）、已安裝的 Ollama 或其他 OpenAI-compatible 服務。

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
- [依賴安全稽核](docs/DEPENDENCY_AUDIT.md)
- [Windows 啟動器與更新器](docs/WINDOWS_LAUNCHER_AND_UPDATER.md)
