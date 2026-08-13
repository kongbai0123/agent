# n8n Agent 治理與 Gmail 整合

適用版本：`0.7.0-n8n-agent-governance-beta.1`

## 架構

Workbench 與 n8n 是兩個獨立服務：

- n8n 只監聽 `127.0.0.1:5678`，保管 Gmail OAuth、Thread 與實際寄送。
- Workbench 管理 Project、Session、Agent 規劃、人工核准、稽核與 Artifact。
- Agent 不直接連線 n8n，也不取得 API Key、OAuth Token、密碼或 Credential Secret。
- 所有狀態變更均由 Workbench Broker 依結構化提案執行。

## 首次設定

1. 以系統管理員 Windows PowerShell 執行：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass `
     -File .\scripts\setup_managed_n8n_isolation.ps1 -Mode Apply -Json
   ```

2. 在本機設定 V1 唯一允許的 Gmail 收件者，再重新啟動 Workbench：

   ```powershell
   [Environment]::SetEnvironmentVariable(
     "WORKBENCH_N8N_GMAIL_RECIPIENT",
     "your-fixed-recipient@example.com",
     "User"
   )
   ```

   地址不會寫入 Repository；既有安裝若已保存固定地址則會自動沿用。未設定時 Mail Profile 會 fail closed，不能啟用或寄信。

3. 進入 Sidebar「流程」。
4. 確認 n8n Runtime、隔離身分及固定版本均顯示就緒。
5. 在 n8n 建立 Public API Key，再貼入 Workbench 的安全表單。Key 只存於 DPAPI 保護的本機 Secret Store。
6. 選擇 Project 與一般聊天 Session，預設使用「限制權限」。

## Agent 操作流程

1. 在「Agent n8n 操作助理」描述目標。
2. Agent 回覆 2–3 個方案，列出外部影響、風險、預期結果及需要的權限。
3. 選擇方案只會鎖定 Plan revision 與 digest，不會改動 n8n。
4. 明確確認後建立待核准 Operation。
5. 右上浮動檢查器顯示伺服器權威 Before／After Diff、節點、外部目標、Credential alias 與可復原性。
6. 核准時伺服器重新取得 n8n Workflow 快照；目標有變更即拒絕舊核准。
7. Broker 執行後記錄安全結果。若遠端結果不明，狀態改為 `execution_unknown`，必須先在 n8n 對帳，不可直接重送。

## 權限

- `off`：Agent 不能讀取或操作 n8n。
- `restricted`：可提出安全草稿；所有 Planner-origin 提案仍須人工核准，高風險節點禁止。
- `full_audit`：可提出發布、啟停與刪除；仍需逐次核准，發布／啟用前必須通過 Security Audit。

完整管理可設定一小時、目前 Session、持續或智慧降級。智慧模式在閒置 30 分鐘、n8n 停止或 Workbench 重啟時降回限制權限並撤銷未使用核准。

## Gmail V1 邊界

- 單一 Gmail 帳號、單一 Profile、單一固定 Project。
- 觸發標籤為 `Workbench-Agent`。
- 收件者由本機 `WORKBENCH_N8N_GMAIL_RECIPIENT` 設定鎖定，不寫入公開原始碼；既有安裝會沿用資料庫中已保存的固定地址。
- 只處理純文字；附件只保留檔名、MIME 與大小，不下載、不解析、不寄出。
- Reply 主旨鎖定；Compose 可修改主旨與正文。
- 所有寄信都需人工核准，Delivery 只允許一次原子 claim。

## 故障處理

- `API Key 尚未設定`：Agent 可規劃，但不能建立可執行提案；先完成安全表單。
- `Broker 尚未就緒`：確認 n8n 服務、固定版本、隔離帳號與 ACL。
- `N8N_WORKFLOW_STALE`：n8n Workflow 已變更；重新規劃並核准新 Diff。
- `N8N_SECURITY_AUDIT_FINDINGS`：官方 Audit 發現 Nodes、Filesystem、Database 或 Instance 風險；處理風險後重新提案。
- `N8N_EXECUTION_OUTCOME_UNKNOWN`：不得重送。先在 n8n 查核 Workflow／Execution，再建立新的人工提案。

## 目前刻意停用

- Agent Credential 管理。
- 任意 Workflow 直接 Execute。
- Code、Execute Command、檔案系統及 Community Node。
- 未經明確 Project adoption 的既有手動 Workflow。

這些功能必須等 Project ownership、受審核 Trigger binding 或隔離 Runner 完成後才可開放。
