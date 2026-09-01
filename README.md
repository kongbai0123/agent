# Local AI Workbench

<p align="center">
  <img src="frontend/app-icon.png" alt="Local AI Workbench" width="88">
</p>

Local AI Workbench 是一套以 Windows 與本機優先為核心的 AI Agent 工作平台。它把聊天、專案知識、Skills、工具執行、工作流程與外部服務整合放在同一個桌面介面中，讓使用者可以在保留資料控制權的前提下，建立可持續使用的 Agent 工作環境。

> 目前專案仍在持續開發中，主要面向本機部署、研究與進階使用情境。

## 主要能力

- **本機與多模型聊天**：支援 Ollama，以及相容 OpenAI API 的模型供應商。
- **Project 工作空間**：依專案管理對話、Session、知識、設定與執行範圍。
- **Skills 與多步驟 Agent**：可為不同專案配置 Skills；明確的多步驟要求會建立有界計畫，逐步執行，並驗證工具成功、非空輸出、相依步驟與安全停止等結構條件。
- **專案知識檢索**：提供文件匯入、增量索引、專案隔離檢索與可追溯引用；預設採本機保守基線，也可選擇既有本機 Embedding／Reranker 模型或受治理的模型服務。
- **回答事實核對**：Basic Chat 可依專案知識的證據標記核對最終回答，並提供提醒、嚴格與關閉三種模式；預設核對器是保守的本機基線，不等同通用事實查核服務。
- **外掛程式中心**：集中管理安裝、信任、啟停、健康狀態、權限與 Audit。
- **統一整合與對外 Agent API**：集中管理 Gmail、GitHub、Notion、n8n、本機 MCP、Project 權限與本機安裝綁定的 API Key，讓受信任的外部系統呼叫 Agent。
- **安全工具執行**：工具依目前 Project、Connection 與資源範圍動態提供；外部寫入必須經過使用者批准。
- **本機 MCP 擴充**：支援經信任的本機 `stdio` MCP Tool，並以獨立程序隔離故障。
- **MLOps 工作區**：以共用執行、政策、產物與健康契約管理本機資料集、實驗、訓練及模型版本。
- **可稽核能力評估**：可把正式 Basic Chat 執行轉成已遮罩、具來源證明的評估證據，再交由版本化 Gate 判定。
- **Windows 桌面體驗**：提供 Launcher、響應式工作區、執行檢查器與本機更新流程。

## 外掛與服務整合

Workbench 以標準化 Hook、Connector 與 MCP 架構擴充功能，不需要把第三方程式碼直接載入核心程序。

目前包含或展示的整合方向有：

- GitHub 與 Notion Connector
- n8n 工作流程與受治理的 Gmail 能力
- Ollama 與其他相容模型供應商
- 經人工信任的本機 Manifest 與 MCP Tools

主 README 只保留產品層級說明。各整合的權限模型、設定方式、治理流程、API 與故障排查，請查看本 repository 內的詳細文件。

## 安全設計

- 本機優先，不要求雲端 Relay。
- OAuth Token、Client Secret 等敏感資料不寫入一般設定檔或聊天紀錄。
- 外掛以 Manifest digest、信任狀態及 Project 範圍控制啟用資格。
- 讀取與寫入能力分離；外部寫入採逐次、單次使用的人工批准。
- Hook、Connector 或 MCP 發生錯誤時採故障隔離；安全關鍵的 Transform、Guard 與工具政策會 fail-closed，Observe 失敗則記錄 Audit 並降低健康狀態。
- 執行、授權、外掛狀態及重要操作均保留 Audit 記錄。

## 快速開始

### 環境需求

- Windows 10 或 Windows 11
- Python 3.11
- Node.js 20 或更新版本（前端開發與語法檢查；受管理的 n8n 使用應用程式指定版本）
- Ollama 或其他相容模型服務（依使用需求安裝）

### 啟動

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --require-hashes -r backend\requirements.lock
Copy-Item backend\settings.json.example backend\settings.json
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\start_workbench.ps1
```

啟動後，Workbench 會在本機 loopback 介面提供 UI 與後端服務。模型、外掛、Connector 與專案權限可在應用程式內設定。

## Repository 結構

```text
backend/      FastAPI、聊天、Agent、外掛、Connector 與安全執行層
frontend/     Workbench 桌面介面
config/       受版本控制的服務與工作流程設定
launcher/     Windows Launcher
scripts/      啟動、更新、建置與安全檢查工具
tests/        後端、前端、安全與整合測試
docs/         各功能的詳細設計、操作與治理文件
```

使用者資料、Token、Runtime 狀態、Project 工作內容與本機資料庫不應提交到公開 repository；相關路徑已由 `.gitignore` 與公開樹檢查保護。

## 詳細文件

- [完整功能與架構總覽](docs/FEATURES_AND_ARCHITECTURE.md)
- [功能與版本變更](WORKBENCH_CHANGES.md)
- [Windows Launcher 與更新流程](docs/WINDOWS_LAUNCHER_AND_UPDATER.md)
- [Hermes 部署與維運](docs/HERMES_PRODUCTION_RUNBOOK.md)
- [n8n Agent、治理與 Gmail 整合](docs/N8N_AGENT_GOVERNANCE.md)
- [Workbench 對外 Agent API](docs/EXTERNAL_AGENT_API.md)
- [依賴與供應鏈稽核](docs/DEPENDENCY_AUDIT.md)
- [執行安全與權限交接](docs/handoff-execution-guards-20260728.md)

## 驗證

```powershell
.\.venv\Scripts\python.exe -m compileall -q backend scripts
.\.venv\Scripts\python.exe -m pytest -q
Get-ChildItem frontend -Filter *.js | ForEach-Object { node --check $_.FullName }
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\check_public_tree.ps1
```

Agent 能力另有 24 題離線評測契約與 fail-closed Gate；執行方式與門檻請見 [Agent 能力評測](evals/README.md)。CI 的確定性 contract smoke 只驗證證據鏈、政策情境及部分產品核心前置條件，不代表任何正式模型或完整聊天 Runtime 已通過；正式成績仍須由候選 Runtime 的權威 Run collector／adapter 產生。
