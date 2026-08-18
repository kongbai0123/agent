# Local AI Workbench 功能與架構總覽

本文件補充主 [README](../README.md) 未展開的產品能力與技術邊界。各子系統的操作手冊、部署程序與歷史變更，請再依文末索引查閱對應文件。

## 1. 產品架構

Local AI Workbench 採本機優先架構：

- `frontend/` 提供桌面工作區、聊天、Project、外掛、工作流程與執行檢查器。
- `backend/` 提供 FastAPI、模型呼叫、Agent 執行、資料存取、權限判斷與整合服務。
- SQLite 保存非秘密狀態；Token、Client Secret 等敏感資料使用 Windows 安全儲存邊界。
- `runtime/` 下的本機資料（安全範例設定除外）、`projects/`、`workspaces/` 與 `artifacts/` 保存本機使用者資料，不納入公開 Git 版本樹。
- 外部服務與可選 runtime 發生故障時，核心聊天與工作區仍應保持可用。

## 2. 工作區與聊天

### Project 與 Session

- 對話、Skills、外部連線、資源範圍及執行紀錄均以 Project 為主要隔離單位。
- Session 保留對話脈絡與當次能力選擇，不會自動取得其他 Project 的資源。
- 左側導覽可切換聊天、工作流程、模型、外掛與設定；右側檢查器顯示 Skills、執行與結果。

### 模型與 Model Gateway

- 支援 Ollama 與 OpenAI-compatible 模型供應商。
- Host-side 模型呼叫統一經過 Model Gateway，套用固定政策與模型 Hook。
- 只有通過能力驗證且宣告支援 Tools 的模型會收到工具 Schema；其他模型維持一般聊天。
- Hermes 為可選的獨立 Agent runtime，失效時不取代或破壞 Basic Chat。

### 工具呼叫

- 工具清單依安裝、信任、啟用、健康狀態、Project 與資源 allowlist 動態建立。
- 工具名稱使用固定 namespace，例如 `github.*`、`notion.*` 與 `mcp.<extension_id>.*`。
- 參數與結果都需通過 Schema 驗證、秘密遮罩及輸出大小限制。
- 外部讀取可在已授權範圍內執行；外部寫入必須逐次取得人工批准。
- 無法確認寫入是否成功時標記為結果未知，不會自動重送。

## 3. Knowledge、Skills 與 Agent

### Knowledge

- 文件與檢索資料依 Project 使用。
- 對話可結合文件內容與暫時上下文，並在 UI 顯示相關來源與使用狀態。
- 使用者資料與模型檔不會提交到 repository。

### Project Skills

- Skill 以 Project 隔離，可設定專案預設、目前 Session 或單次使用。
- Skill 的識別、內容與資源會納入 digest，避免執行期間發生未審查漂移。
- UI 提供選擇、啟停與本輪使用紀錄。

### Agent 執行與結果

- 執行面板呈現任務、工具、核准、錯誤、產物、修改與驗證結果。
- 長時間任務可顯示進度；右側面板在桌面、窄視窗與行動尺寸採不同停靠策略。
- 非同步回應以 Project、Session、Run 與內容 owner 驗證，避免舊回應覆蓋新工作區。

## 4. 外掛程式平台

### Extension Center

外掛中心包含探索、已安裝、連線及私人／本機頁面，支援：

- Manifest 驗證與內容 digest
- 安裝、人工信任、全域／Project 啟停
- 健康檢查、Audit 與移除
- 設定變更後撤銷信任
- 不可用 adapter 的清楚原因與停用狀態

MVP 不下載或執行未知遠端程式碼。來源限內建目錄、人工信任的本機 Manifest，以及受限制的本機 MCP 設定。

### Hook Dispatcher

內建 Hook 使用 Pluggy 驗證與註冊，再由非同步 Dispatcher 控制順序、逾時與 Audit。第三方 Python 不直接載入 FastAPI 程序。

Hook 分成三類：

- `observe`：觀察事件，不修改流程；失敗時隔離並記錄。
- `transform`：轉換具型別的輸入或輸出；失敗時停止該次操作。
- `guard`：拒絕、要求批准或不表態；不能放寬 Host 固定政策。

支援的事件涵蓋應用程式、Session、聊天 Run、Host model、Connector／MCP Tool 與回應保存等階段。實際 Hook contract、timeout 與錯誤碼以程式碼及測試為準。

## 5. Connector 與 OAuth

### GitHub

- 使用自備 GitHub App OAuth 設定。
- 讀取能力涵蓋 Repository、檔案、Commit、Issue、Pull Request 與 Check。
- 寫入限 Issue 建立／更新，以及 Issue／Pull Request 的一般討論留言。
- 不提供程式碼修改、分支建立、Merge、刪除或 Repository 管理操作。

### Notion

- 使用自備 Notion Public Integration OAuth 設定。
- 搜尋與讀取限使用者授權且綁定至目前 Project 的 Page／Database root 及其子項。
- 寫入限建立 Page、更新 Page 及附加 Blocks。
- 不提供刪除、封存或留言操作。

### 共同安全邊界

- OAuth callback 只使用本機目前後端位址。
- OAuth state 有效期有限、單次使用，並以雜湊形式保存。
- Client Secret、Access Token、Refresh Token 與 PKCE verifier 不存入一般 SQLite 欄位。
- Connection 與資源 allowlist 綁定 Project；工具執行前及批准後都會重新確認 revision 與範圍。
- Disconnect 優先嘗試遠端撤銷；遠端失敗時不會靜默刪除本機憑證。
- MVP 不接收外部 Webhook，也不需要公開 Tunnel 或雲端 Relay。

## 6. 本機 MCP

- 只支援經人工信任的本機 `stdio` MCP Tools。
- 執行檔必須是絕對路徑，並綁定檔案 SHA-256、允許的工作目錄、參數與環境變數名稱。
- 不接受 Shell、URL executable、相對路徑、自動套件安裝或未知 HTTP MCP。
- 每個 MCP 外掛使用獨立程序；停用、更新、移除與關機時會終止。
- Tool Schema 需通過驗證並套用同一套 Project scope、Hook、批准與 Audit 流程。
- 程序隔離只提供故障隔離，不等同完整 OS Sandbox。

## 7. n8n、Gmail 與 Hermes

### n8n 與 Gmail

Workbench 可管理本機 n8n 工作流程，並提供受治理的 Gmail 草稿與核准流程。Agent 不直接取得 n8n API Key 或 Gmail OAuth Token；外部操作受 Project、Policy、Diff、digest 與人工核准約束。

完整部署、版本要求、Bridge、Credential alias、Workflow 編譯、風險政策與故障排查請參閱 [n8n Agent 治理與 Gmail 整合](N8N_AGENT_GOVERNANCE.md)。

### Hermes

Hermes 以獨立 loopback sidecar 漸進接入，具有健康檢查、能力驗證、回退與 rollout 控制。完整維運方式請參閱 [Hermes 部署與維運](HERMES_PRODUCTION_RUNBOOK.md)。

## 8. 安全與資料邊界

- 固定 Host policy 的優先級高於任何外掛或 Hook。
- 所有外部寫入批准綁定 Project、Connection、Tool、完整參數 digest、資源與 Manifest digest，且只能使用一次。
- 外掛、Connector、Hook、MCP 與重要設定變更都保留 Audit。
- API、Log、聊天紀錄及一般資料表不得包含未遮罩秘密。
- 本機 Manifest 或可執行設定變更後，系統會撤銷原信任並要求重新審查。
- 單一外掛、Connector、Hook 或 MCP 故障不應造成整個 Workbench 崩潰。

## 9. 測試與維運文件

- [功能與版本變更](../WORKBENCH_CHANGES.md)
- [Windows Launcher 與更新流程](WINDOWS_LAUNCHER_AND_UPDATER.md)
- [Hermes 部署與維運](HERMES_PRODUCTION_RUNBOOK.md)
- [n8n Agent、治理與 Gmail 整合](N8N_AGENT_GOVERNANCE.md)
- [依賴與供應鏈稽核](DEPENDENCY_AUDIT.md)
- [執行安全與權限交接](handoff-execution-guards-20260728.md)

主要驗證包含後端單元／整合測試、前端 DOM contract、JavaScript 語法檢查、公開版本樹秘密檢查及 Windows 啟動流程測試。
