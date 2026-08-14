# n8n Agent 節點編排、治理與 Gmail 整合

適用版本：`0.8.0-n8n-graph-authoring-beta.1`

## 本版進度

| 項目 | 狀態 | 說明 |
|---|---|---|
| Node Catalog | 已完成 | 固定 n8n `2.32.5`、`n8n-nodes-base 2.32.3`、package lock 與 Catalog digest；只收錄官方內建節點。 |
| 節點圖編譯與驗證 | 已完成 | Agent 產生語意 Spec；伺服器產生節點 ID、名稱、版本、位置與連線，並驗證參數、Credential、埠、分支、孤立節點與循環。 |
| Planner materialize | 已完成 | 先顯示 2–3 個架構，選定後才編譯；只有 `graph_ready` 能建立操作提案。 |
| Patch、採用與 Diff | 已完成 | 既有流程使用語意 Patch；未受管理流程要先精確名稱確認；Diff 與 digest 由伺服器依 n8n 權威快照產生。 |
| Workbench Agent／Approval Bridge | 程式與範本已完成 | 受保護的未啟用範本、HMAC 簽章 API、Agent task runtime 與 readiness 驗證已完成。尚未自動安裝或啟用。 |
| Credential alias | 已完成 | Project 隔離；模型及公開回應只看到別名、類型與狀態。 |
| Runtime approvals | 已完成 | 外部寫入預設逐次核准；限時許可只在符合條件的 Full Audit 模式生效。 |
| 高風險隔離 Runner | 尚未就緒 | Code、Execute Command、檔案系統、Community／Custom Node 及其他高風險能力保持 fail closed。 |
| 實際寄信 | 未執行 | 本次實作與驗證沒有啟用 Workflow，也沒有寄出任何郵件。 |

## 「拉節點」的定義

Workbench 不模擬滑鼠拖曳，也不重做 n8n 畫布。「拉節點」是以下受治理流程：

1. Agent 依需求提出 2–3 個輕量架構，說明 Trigger、外部影響、風險與所需 Credential alias。
2. 使用者選定架構後，Agent 只輸出語意化 `workflow_spec.v1`，不直接輸出原始 n8n Workflow JSON。
3. 伺服器以固定 Node Catalog 配對節點、參數及連線，產生唯一實際節點圖。
4. 缺少 Trigger、必要參數、資料欄位、Credential alias 或無法確認動態埠時，回傳 `needs_input` 並停止建立草稿。
5. 圖形通過驗證後進入 `graph_ready`；使用者確認不可變 Proposal 與 Diff 後，Broker 只建立未啟用草稿。
6. Workbench 以已驗證的 loopback URL 開啟該 Workflow；發布、啟用及每次外部寫入仍須走各自核准邊界。

Stage 1 不載入完整 Node Catalog，也不保存可執行 Spec。選項 ID 由伺服器產生；選定後的補充訊息會保留同一架構。Stage 2 才載入最新 Catalog 並鎖定 Plan 建立時的模型；模型、Catalog、Project、Session 或 digest 漂移都會要求重新規劃。

## Node Catalog 與圖形編譯器

- Catalog 首次載入時驗證固定 n8n、nodes-base、產生的節點 metadata、版本索引與 package lock；任一不符即拒絕 materialize。
- 全部已安裝的官方內建節點可搜尋與規劃；Community／Custom Node 不進 Catalog。
- 編譯器負責節點 ID、唯一名稱、`typeVersion`、位置、`connections` 與 Credential ID 的伺服器端插入。
- 驗證必要參數、顯示條件、Credential 類型、輸入／輸出埠、IF／Switch／Merge 分支、斷線／孤立節點、非法循環及資料欄位對應。
- 動態資料結構或埠不能安全確認時必須詢問使用者；最多自動修復兩次，仍不合法即停止。
- Plan／Operation 公開資料只包含安全化 `graph_preview`、`validation_status`、Catalog／Graph digest 與 Diff，不包含 Secret 或 n8n Credential ID。

## 規劃、Patch、核准與 n8n 畫布

- 舊 Plan 沒有 Catalog／Graph digest 時必須重新規劃，不能直接核准。
- 更新既有流程使用 `add`、`update`、`remove`、`connect`、`disconnect` Patch，未修改節點與 Credential 綁定必須保留。
- 未由 Workbench 管理的 Workflow 必須先預覽風險，再輸入完整 Workflow 名稱確認採用。
- Active Workflow 必須先停用、重新取得快照、修改、重新核准，再另行啟用。
- Inspector 顯示 Before／After、節點新增／刪除／變更、參數摘要、連線、IF／Switch 分支、Credential alias、外部目標、可復原性與風險。
- 核准 digest 同時綁定 Catalog、原 Workflow 快照與編譯後節點圖。內容、版本或 n8n 手動修改任何一項改變，舊核准立即失效。
- Broker 建立草稿後會重新 GET；回讀 graph digest 不一致時視為失敗，不會發布、啟用或執行。

## Workbench Agent Bridge

畫布中的 `Workbench Agent` 與 `Workbench Approval` 都會編譯為 n8n 內建 Execute Sub-workflow；不安裝自訂節點：

```text
Trigger → Workbench Agent → Workbench Approval → Gmail／HTTP／其他明示外部節點
```

- `Workbench Agent Bridge v1` 只接收不透明 `agent_binding_id`、Workflow revision、節點 ID、request ID 與有界輸入。
- 任務指示、模型、Project Skills digest 與輸出 Schema 存在 Workbench 加密邊界，不寫進 n8n Workflow。
- 專用 Agent runtime 關閉 Hermes tools、Project 檔案工具、網址存取及外部寫入工具；n8n 輸入一律標示為不可信資料。
- 模型輸出必須符合有界結構化 Schema；最多兩次格式修復，仍失敗就保存安全錯誤狀態。
- Bridge 使用 HMAC-SHA256 簽章的 loopback API；timestamp、nonce 與 body digest 都要驗證，n8n 不得選擇 Workbench Project。
- Agent binding 只有在受管理 Workflow 的精確 active revision 驗證後才會啟用；Workflow 修改、停用或刪除會停用 binding 並撤銷舊授權。

`Workbench Approval Gate v1` 在外部寫入節點前建立執行時核准，輪詢安全狀態後才回傳原始輸入。它不持有 Workbench 管理權，也不能自行核准。

## Credential alias 與執行時核准

- 使用者在 Project 範圍採用 n8n Credential，並指定不含秘密的 alias。
- n8n Credential ID 只由本機安全表單提交，不會由讀取 API 回傳；伺服器加密保存並於編譯時插入。
- Agent、Planner、Inspector、Log、SSE 與 Audit 只看到 alias、Credential 類型、連線狀態及 metadata digest；OAuth Token、API Key、密碼及 Secret 永不送進模型。
- 每個 runtime action 綁定 Project、Workflow active revision、節點、操作、Credential alias、外部目標 digest、run key 與 request digest。
- 預設 `duration_minutes=0` 是單次精確核准，不會產生可重用授權。每一封 Email、每一次刪除或 HTTP／資料庫寫入都會重新停下等待核准。
- 最長 60 分鐘的限時許可只允許非 Session 的 `full_audit`，且 runtime 必須 ready；授權只涵蓋精確 revision、節點、操作、alias 與目標。
- 權限降級、Workflow revision 改變、Credential alias 更新／撤銷、n8n 停止或 Workbench 重啟會撤銷未使用核准與限時許可。

## 權限與安全邊界

- `off`：Agent 不能讀取或操作 n8n。
- `restricted`：預設模式；可規劃及建立安全草稿，發布、啟停、刪除、執行與外部寫入必須核准，高風險節點禁止。
- `full_audit`：可提出更多管理操作，但每次狀態變更仍需核准；發布／啟用前必須通過 Security Audit。

完整管理可設定一小時、目前 Session、持續或智慧降級。智慧模式在閒置 30 分鐘、n8n 停止或 Workbench 重啟時降回限制權限並撤銷未使用核准。

下列規則不因 Full Audit 而消失：

- Agent 不取得 n8n API Key、OAuth Token、密碼、HMAC Secret 或 Workbench Secret Store 存取權。
- Agent 不能直接呼叫 `127.0.0.1:5678`，也不能自行提升 Project 權限或跳過 Broker。
- Code、Execute Command、檔案系統、Community／Custom Node 及其他高風險節點，在隔離 Runner 未就緒時只能停在草稿，不能發布或執行。
- Security Audit 失敗、逾時或結果無法驗證時，發布及啟用 fail closed。
- 系統 Gmail V1、Workbench Agent Bridge 與 Approval Gate 都是受保護 Workflow，Agent 不能採用、修改或刪除。

## 首次設定與 Bridge 部署下一步

1. 以系統管理員 Windows PowerShell 完成既有受管理 n8n 隔離：

   ```powershell
   powershell.exe -NoProfile -ExecutionPolicy Bypass `
     -File .\scripts\setup_managed_n8n_isolation.ps1 -Mode Apply -Json
   ```

2. 在 Sidebar「流程」確認 n8n `2.32.5`、Node.js `24.15.0`、低權限身分、D 槽資料目錄與 ACL 均 ready，並以安全表單保存 Public API Key。
3. 由受控部署流程載入：

   - `config/n8n-workflows/workbench-agent-bridge-v1.json`
   - `config/n8n-workflows/workbench-approval-gate-v1.json`

   部署流程必須先以 Workbench HMAC Credential ID 取代範本 placeholder，通過嚴格範本驗證，再發布兩個子流程。n8n `2.32.5` 以 `active` 表示已發布；範本只有 Execute Workflow Trigger，沒有排程或 Webhook，因此不會自行啟動。不可把 HMAC Secret 寫進 Repository、命令列、Log 或一般 API DTO。
4. 從 n8n 取得兩個受保護子流程的 Workflow ID，設定使用者環境變數：

   ```powershell
   [Environment]::SetEnvironmentVariable(
     "WORKBENCH_N8N_AGENT_BRIDGE_WORKFLOW_ID",
     "<agent-bridge-workflow-id>",
     "User"
   )
   [Environment]::SetEnvironmentVariable(
     "WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID",
     "<approval-gate-workflow-id>",
     "User"
   )
   ```

5. 重新啟動 Workbench，確認 Bridge readiness 通過；建立 Project-scoped Credential alias，再 materialize 一個不含外部寫入的未啟用 canary 草稿。
6. 重新 GET canary，確認 graph digest 一致。發布與啟用要另行通過人工核准與 Security Audit；外部寫入再以固定目標執行逐次核准 canary。

Workbench／Launcher 目前不會自動完成第 3、4 步，也不會自行發布或啟用 Bridge。這是刻意的部署邊界，不應以寬鬆權限或把 Secret 提供給 Agent 的方式繞過。

此 beta 的兩個 Bridge 範本目前固定呼叫 `http://127.0.0.1:8000`。若 Launcher 因埠衝突改用 8080 或其他候選埠，Bridge 必須保持未啟用；不可直接修改受保護範本的 URL，應先完成受審核的 API base 綁定能力或讓受管理 Workbench 重新取得 8000 埠。

## Gmail V1 邊界

如需使用既有 Gmail V1，先設定唯一允許收件者，再重新啟動 Workbench：

```powershell
[Environment]::SetEnvironmentVariable(
  "WORKBENCH_N8N_GMAIL_RECIPIENT",
  "your-fixed-recipient@example.com",
  "User"
)
```

- 單一 Gmail 帳號、單一 Profile、單一固定 Project；觸發標籤為 `Workbench-Agent`。
- 收件者由本機環境變數鎖定，不寫入公開原始碼；未設定時 Mail Profile fail closed。
- 只處理純文字；附件只保留檔名、MIME 與大小，不下載、不解析、不寄出。
- Reply 主旨鎖定；Compose 可修改主旨與正文。
- 所有寄信都需人工核准，Delivery 只允許一次原子 claim。

## 故障處理

- `needs_input`：缺少 Trigger、必要參數、欄位對應或 Credential alias；補充問題後重新 materialize，不會建立草稿。
- `N8N_CATALOG_*`：本機 n8n、nodes-base、metadata、版本索引或 package lock 不符合固定 Catalog；修復固定安裝後重新規劃。
- `N8N_WORKFLOW_STALE`：n8n Workflow 已變更；重新取得快照、Diff 與 digest，再建立新核准。
- `N8N_RUNTIME_APPROVAL_STALE`：操作內容、Workflow revision、Credential 或 Policy 已改變；舊核准不能使用。
- `N8N_RUNTIME_TIMED_GRANT_FORBIDDEN`：限時許可不符合 Full Audit 或 runtime readiness；改用單次核准或完成治理設定。
- `N8N_SECURITY_AUDIT_FINDINGS`：Security Audit 發現風險；處理後重新提案，不能略過。
- `N8N_EXECUTION_OUTCOME_UNKNOWN`：不得盲目重送；先在 n8n 對帳，再建立新人工提案。

## 相關 API

Browser API 由 Workbench UI 使用，均要求本機 Session 驗證：

- `GET /api/integrations/n8n/node-catalog`
- `POST /api/integrations/n8n/plans/{plan_id}/materialize`
- `GET /api/integrations/n8n/managed-workflows/{workflow_id}/adoption-preview`
- `POST /api/integrations/n8n/managed-workflows/{workflow_id}/adopt`
- `GET|POST|DELETE /api/integrations/n8n/credential-aliases...`
- `GET|POST /api/integrations/n8n/runtime-approvals...`
- `GET|POST /api/integrations/n8n/agent-bindings...`

n8n 專用 Agent task／runtime action API 只接受 loopback、`agent-runtime` HMAC profile、timestamp、nonce 與 body digest。Schema 不含 Project 選擇或任何可回傳 Secret 的欄位。
