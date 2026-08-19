# Local AI Workbench 改版紀錄

## 2026-08-19 — `0.9.0-model-catalog-beta.1`

### 模型管理

- 將本機 Ollama 型錄由 4 個擴充為 62 個經官方 Library 核對的生成模型，涵蓋 Qwen 3.5／3.6、Gemma 4、Granite 4、Ministral 3、DeepSeek R1、gpt-oss、Phi-4、Llama、程式模型與多模態模型。
- 型錄只收錄可本機下載的明確 tag，不納入 cloud-only、重複量化、社群 namespace、Embedding、Reranker、Guard 或分類器，避免專用模型被誤當一般聊天模型。
- 「可安裝」新增名稱／開發者搜尋、用途篩選、友善名稱、發布者、授權、Context、硬體需求與收錄數量；找不到的官方生成模型可用安全的 Ollama tag 手動安裝。
- 修正硬體資料格式接錯而把所有模型判為不適合、已安裝模型仍出現在可安裝清單、安裝完成後快取未更新，以及下載進度 SSE 使用未定義 formatter 的問題。
- 自訂模型名稱加入長度、字元與路徑片段驗證；安裝仍只透過本機、受擴充權限保護的 Ollama API。

### 擴充功能與 n8n HMI

- 擴充中心改以「已安裝／未安裝」為主要分類，連線與私人本機工具降為次要入口，並避免相同擴充同時出現在兩邊。
- n8n 操作改為「啟動服務 → 說出需求 → 確認執行」；單人本機首次使用會自動準備個人 Project／Session，權限與 Gmail 進階設定預設收合。
- 保留既有人工核准、Credential 隔離、稽核、Extension gate、受管理 runtime ownership 與本機 Editor URL allowlist。

### 驗證

- Python、JavaScript、Public-tree、模型型錄／安裝串流、Extension／n8n 及完整 deterministic suite 的最終結果以本版發布報告為準。

---

## 2026-08-14 — `0.8.0-n8n-graph-authoring-beta.1`

### 已完成

- 新增固定版本 Node Catalog，綁定 n8n `2.32.5`、`n8n-nodes-base 2.32.3`、package lock 與 Catalog digest；可搜尋全部已安裝的官方內建節點，排除 Community／Custom Node。
- Agent 改為產生語意化 `workflow_spec.v1`，不再直接猜整份 n8n JSON。伺服器端編譯器負責節點 ID、唯一名稱、`typeVersion`、位置及 `connections`，並驗證必要參數、Credential 類型、輸入／輸出埠、IF／Switch／Merge 分支、孤立節點、循環與資料欄位對應。
- Planner 正式拆成兩階段：Stage 1 僅提供 2–3 個不含 Workflow Spec 的輕量架構；Stage 2 才為選定方案生成唯一語意 Spec 並交給 Graph Compiler。每階段最多首次加兩次修復，資料不足會回到 `needs_input`。
- 新 Plan 使用 `workbench.n8n.two-stage.v1`、鎖定 provider／protocol／model，並以 revision＋digest CAS 與 `materializing` lease 防止併發重複生成；舊 Plan 不自動轉換，必須重新規劃。
- 更新既有 Workflow 改用 `add／update／remove／connect／disconnect` 語意 Patch；手動 Workflow 必須先以完整名稱確認採用。Diff 由伺服器依權威快照產生，顯示節點、參數、連線、分支、Credential alias 與外部目標。
- Proposal 與核准 digest 同時綁定 Catalog、原 Workflow 及編譯後節點圖。核准後只建立未啟用草稿；重新 GET 的 graph digest 不一致、n8n 有手動修改或核准內容改變時均 fail closed。
- 新增受保護 `Workbench Agent Bridge v1` 與 `Workbench Approval Gate v1` 範本。兩者只使用 n8n 內建節點與簽章 loopback API；Agent task 使用無工具模型、Project Skills 快照、有界結構化輸出及加密持久化。
- 新增 Project-scoped Credential alias。Credential ID 只由本機安全表單提交並加密保存，不會出現在回應；Agent 與一般讀取 API 只看到別名、類型與連線狀態，OAuth Token、API Key 與 Secret 不進模型、Log、SSE、Audit 或 Inspector。
- 新增執行時核准：每次 Email、刪除、HTTP／資料庫外部寫入均綁定精確 Workflow revision、節點、操作、Credential alias、目標 digest 與 request digest。預設為單次核准；最長 60 分鐘的限時許可只在非 Session 的 `full_audit` 且 runtime ready 時成立。
- 權限降級、Workflow revision 改變、Credential alias 更新／撤銷、n8n 停止或 Workbench 重啟會撤銷未使用核准與限時許可。發布／啟用仍需 Security Audit；隔離 Runner 未就緒時高風險節點不可發布或執行。
- 「流程」工作區新增 Catalog 搜尋、節點圖預覽、問題／風險、materialize、Credential alias、執行時核准及既有 Workflow 採用介面；成功建立草稿後可開啟對應 n8n 畫布檢視。

### 部署狀態與安全邊界

- 兩個 Bridge 範本及其嚴格驗證、HMAC 綁定與 readiness 檢查已完成，但不會由 Workbench／Launcher 自動匯入或發布。
- 正式 canary 前仍須以受控流程綁定 n8n HMAC Credential、匯入並發布兩個僅含 Execute Workflow Trigger 的子流程，設定 `WORKBENCH_N8N_AGENT_BRIDGE_WORKFLOW_ID` 與 `WORKBENCH_N8N_APPROVAL_GATE_WORKFLOW_ID`，再重新啟動 Workbench 驗證 readiness。n8n `2.32.5` 以 `active` 表示已發布；這兩個受保護子流程沒有排程或 Webhook，不會自行啟動。
- 此 beta 的 Bridge 範本目前固定呼叫 `127.0.0.1:8000`；部署時必須確認 Workbench 使用該受管理埠。若 Launcher 改用其他埠，Bridge 應保持未啟用，不能以手動修改受保護範本繞過驗證。
- 本次只建立與驗證程式、範本及未啟用草稿路徑；沒有啟用 n8n Workflow，也沒有寄出任何郵件。

### 驗證

- Node Catalog／編譯器、Planner materialize／Patch、Broker Diff／digest、Agent task runtime、Credential alias、執行時核准、Bridge 範本及前端契約均有離線測試覆蓋。
- 實際通過數以本版最終驗證報告為準；驗證不會啟用 Workflow 或執行外部寫入。

---

## 2026-08-13 — `0.7.0-n8n-agent-governance-beta.2`

### 主要更新

- 右上浮動檢查器不再覆蓋聊天內容：桌面保留 16px 安全間距，訊息與輸入框維持既有最大寬度，只在可用空間不足時縮窄；窄螢幕改由聊天在檢查器下方開始。
- 一般聊天中的明確 n8n 操作要求會轉入既有受治理 Planner；寄信要求則使用固定收件者的 Gmail Draft Runtime，只建立待核准草稿。兩者都不再交給沒有 n8n 工具的一般模型回覆。
- 交接仍維持 Project／Session scope、不可變 digest、明確提案確認與人工核准；Agent 不取得 n8n API Key，也不能直接呼叫 Broker 或提升權限。
- n8n 按需啟動完成後才取得 Planner readiness，避免啟動競態誤報；尚未設定 API Key、未選 Project／Session或服務未就緒時均清楚 fail closed。

### 驗證

- `python -m pytest tests -q`：865 passed、3 skipped。
- 前端 JavaScript、PowerShell、Inspector／n8n Planner 契約及 Git diff 檢查通過。

---

## 2026-08-13 — `0.7.0-n8n-agent-governance-beta.1`

### 主要更新

- Sidebar 新增「流程」工作區，提供受管理的 n8n 啟停、狀態、Gmail Profile、Compose、背景 Run 與待核准項目。
- 新增 Agent n8n 操作助理：先以無工具規劃器說明 2–3 個方案、風險、預期結果與所需權限；選定方案只建立不可變提案，不直接操作 n8n。
- 新增 Project／Session 綁定的 n8n Broker。人工核准伺服器產生的 Before／After Diff 與 digest 後，才可建立或修改未啟用草稿、發布、啟用、停用或刪除 Workflow。
- n8n API Key 使用 DPAPI 保護，不進模型、Prompt、Log、SSE、Audit 或 Inspector；跨 Project、封存及 Integration-only Session 均 fail closed。
- 核准與執行前重新驗證 n8n 目標快照、Project binding、Policy、Runtime、API Key 與官方 Security Audit；任何 stale target 回傳 409。
- Broker 呼叫後結果無法判定時改列 `execution_unknown`，要求人工對帳並禁止盲目重送。
- 新增單一 Gmail V1：標籤來信與 Compose 產生純文字草稿，固定收件者、24 小時核准、一次性 Delivery claim、HMAC 防重播與 AES-256-GCM 內容保存。
- n8n 固定為 2.32.5、Node.js 固定為 24.15.0、資料只使用 D 槽 runtime；低權限帳號／ACL 未就緒時拒絕啟動，不回退到互動使用者。

### 安全限制

- Credential 建立／更新／刪除保持關閉，直到完成 Project-scoped credential ownership。
- Code、Execute Command、檔案系統與 Community Node 保持關閉，直到額外隔離 Runner 完成 attestation。
- 任意 Workflow 直接執行保持關閉，直到具備受審核的 Trigger binding。
- 系統管理的 Gmail Workflow 受保護，Agent 無法採用、修改或刪除。

### 驗證

- `python -m pytest tests -q`：857 passed、3 skipped。
- n8n 專項：122 passed。
- Python／JavaScript／PowerShell 語法、Git diff 與 public-tree 邊界檢查通過。

---

## 2026-08-04 — `0.5.0-agent-skills-beta.1`

### 主要更新

- 新增 Agent Skills 安全生命週期：本機來源檢查、內容雜湊、安裝、信任、全域／專案／Session 啟用、快捷選用、Subagent 裁切繼承、稽核、量測與功能旗標回滾。
- 新增 Skill Center 與輸入列快捷入口；Skill 只載入指令與唯讀資源，不能授予、擴大或繞過既有工具權限。
- 修正自然語句含 `Agent` 與「沒有」時被誤判為能力查詢、回傳固定能力表的問題。
- 新增輕量路由：發想、重述、簡短直接回答與框架討論不派 Subagent；需要讀現有程式碼時只開放唯讀工具並強制唯讀權限，回答修復最多一輪。
- 每輪新增 Session／Run／Turn／prompt SHA-256 綁定；Subagent assignment、handoff、Verifier、Final 與 Done 均保留同一身分，錯配時停止交付與保存。
- 對話歷史改以 `assistant.parent_message_id` 與相同 `turn_id` 精確配對；失敗或被較新回合取代的回答只供 UI 查看，不進入下一輪模型上下文。
- 將 Subagent 失敗恢復狀態改為非阻擋稽核警告；只把可由回答改善的公開驗證問題送入有限修復，避免內部 check key 外洩或覆蓋原任務。
- 新增四主題、設計重述、Skill 框架與完整性回答契約，以及 12 項 v0.4 多輪回歸 scenario 與可選 hard quality gate。
- tokens/sec 改用模型回報的 completion tokens／eval duration；無模型 eval 資料時顯示無法量測，不再用整輪牆鐘時間推算高估值。
- 回歸 runner 2.1 支援同 Session `turns[]`、每輪 RAG／權限、完整 Run／Turn／prompt／parent message 綁定、事件欄位比對及硬品質門檻；自託管 Windows workflow 預設執行 v0.4 的 12×3 consistency gate。

### 相容性與更新

- Agent protocol 由 3 升為 4；舊版單輪 regression dataset 與既有 Skill 關閉狀態維持相容。
- 封裝的 Launcher 圖示與 EXE 未變；`start_workbench.ps1` 僅更新前端 cache key，無需重新封裝。
- 本版不修改更新器保護的 `backend/database.py`，乾淨 `main` 安裝仍可走既有 deterministic CI 後的 fast-forward 更新流程。

### 驗證

- `python -m compileall -q backend scripts evals`
- `python -m pytest tests -q`
- `node --check frontend/app.js` 與 `frontend/skill-center.js`
- `scripts/check_public_tree.ps1`、PowerShell syntax、鎖定依賴與 secret/public-tree 邊界檢查

---

## 2026-07-31 — `0.4.1-hybrid-subagent-beta`

本次更新用於 GitHub `origin/main` fast-forward 更新測試。推上 GitHub 後，乾淨的本機安裝可由 `LocalAIWorkbench.exe` 檢查 `origin/main`，在 Windows deterministic CI 通過後套用更新。

### 主要更新

- 評測發布規則已收緊：100 題 v0.3 必須在相同模型 digest、程式 revision、資料集與 gate 下完成 3 trial（300 次）才可稱為趨勢；既有 17/100 改列為 historical smoke baseline。
- README 會分開列出本版可重跑的程式驗證，以及歷史模型 baseline，避免把測試通過誤寫成模型能力提升。

- Subagent Planner 新增受限 `model_policy` 與 `depends_on_roles` 欄位；Planner 只能選政策與依賴，不能輸入任意模型、provider URL 或金鑰。
- 父程序新增模型路由解析器，只會從設定中心已核准、角色合格的本機或 OpenAI 相容模型中挑選，且「智慧雲端改派」預設關閉。
- Subagent 排程改為智慧混合：本機 Ollama 永遠只有一個受保護通道，獨立遠端 Explorer／Implementer 可依 `subagent_max_parallel` 與本機工作重疊。
- 設定中心新增「允許智慧雲端改派」與 1/2/3 智慧並行上限，資源預覽會顯示本機通道、遠端通道與改派預覽。
- Process-isolated worker 啟動後會清空繼承環境，再套用同一份子程序 allowlist，避免父程序秘密流入子代理。
- 資源估算邏輯拆成獨立模組，執行前與 Planner 重新派工後都會重新送出 `resource_guard`。

### 更新後測試方式

1. 在另一個乾淨 checkout 或已安裝目錄保持 `main` 分支且工作樹乾淨。
2. 雙擊 `LocalAIWorkbench.exe`，讓啟動器檢查 GitHub `origin/main`。
3. 若 GitHub deterministic CI 已通過，依提示套用 fast-forward 更新。
4. 啟動後到設定中心確認版本為 `0.4.1-hybrid-subagent-beta`，並檢查 Subagent 設定中有「允許智慧雲端改派」與「智慧並行上限」。

### 相關檔案

- `backend/subagent_model_routing.py`
- `backend/subagent_resources.py`
- `backend/subagent_assignment_builder.py`
- `frontend/index.html`
- `frontend/app.js`
- `tests/test_subagent_hybrid_routing.py`

---

## 歷史紀錄（Sprint 1 + Sprint 2）

依據《Fable5 前端 UX 執行文件》完成 Sprint 1（資訊架構）與 Sprint 2（模型與初次使用），並擴充後端 API 支援 Model Manager。

## 檔案異動

| 檔案 | 異動 |
|---|---|
| `frontend/index.html` | 全新 App Shell：Top Bar + 64px Icon Rail + Drawer + Start Dashboard + Composer 模式列；新增 Model Manager / 切換確認 / Setup Wizard / 安裝進度 / Toast 等 UI 骨架 |
| `frontend/workbench.js` | **新檔**。工作台殼層邏輯：狀態 chips、Drawer、Dashboard、模式切換、Model Manager、Wizard、安裝串流、測速、Toast |
| `frontend/app.js` | 手術式修改：WB 掛鉤（狀態/模型/RAG/token/sources）、生成可中止（AbortController + 停止鈕）、「只套用本輪」模型、alert 全面導向 Toast；**並修復 14 處因編碼損毀被註解吞掉的程式碼行**（含 RAG 狀態載入、sources 事件、escapeHtml fallback 等既有 bug），修正設定儲存成功卻顯示 "Operation failed." 的問題 |
| `frontend/style.css` | 檔尾新增 `99_workbench` 區塊（Top Bar / Rail / Drawer / Dashboard / Model Manager / Wizard / Toast / 響應式），原樣式全數保留 |
| `backend/app.py` | 新增 API（見下） |

## 新增後端 API

- `GET /api/hardware` — RAM（psutil / Windows ctypes fallback）、NVIDIA GPU（nvidia-smi）、CPU
- `GET /api/models/catalog` — 精選模型目錄 + 依實際硬體計算相容性 + 推薦清單
- `POST /api/models/pull` — 代理 Ollama pull，以 SSE 回傳下載進度（percent / bytes）
- `DELETE /api/models/{name}` — 刪除模型
- `GET /api/models/info?name=` — context window / 參數量 / 量化（Ollama /api/show）
- `POST /api/models/benchmark` — 短生成測速，回傳 TTFT 與 tokens/sec

## Sprint 1 交付

1. **Top Bar 狀態列**：系統就緒／後端未連線／Ollama 未啟動／尚未安裝模型 chip、模型 chip（→ Model Manager）、RAG ON／一般對話、N docs · N chunks（→ 知識庫）、tok/s（→ Benchmark）、n/k ctx（→ Context Inspector）
2. **64px Icon Rail**：Chat / 知識庫 / Runs / Artifacts / Models / 設定，點擊開 Drawer，不再常駐 320px sidebar
3. **Context Drawer**：RAG Sources、Temporary Context、Conversation Context、Context Usage（估計值）
4. **Welcome Dashboard**：目前狀態六項 + 三種空狀態 CTA（安裝推薦模型／上傳文件建立知識庫／開始詢問知識庫）+ 錯誤恢復卡
5. **Composer mode chips**：Ask / RAG / Code / Analyze / Build UI；生成中顯示「正在生成回答... N tok/s」，送出鈕變停止鈕（可真正中止串流）

## Sprint 2 交付

1. **First-run Setup Wizard**：後端未啟動／Ollama 未連線／無模型／首次啟動時自動開啟；7 步：環境 → Ollama → 硬體 → 推薦 → 安裝（含進度）→ 測速 → 完成
2. **Model Manager**：Installed / Recommended / Available / Benchmark 四分頁
3. **安裝進度 UI**：右下角進度卡（%、GB、MB/s、可取消），Wizard 內同步顯示
4. **硬體相容性卡**：RAM / GPU / 可用 VRAM / CPU + 每個模型的相容性徽章與預估速度
5. **模型切換確認**：只套用本輪／套用目前對話／設為預設／取消

## 其他

- Error UX：`alert()` 不再作為主要錯誤 UI，統一 Toast + Recovery Card（附下一步按鈕）
- A11y：icon 按鈕 aria-label、ESC 關閉 modal/drawer、prefers-reduced-motion
- 響應式：Tablet 保留 Rail；Mobile Rail 轉為底部 tab bar、Drawer 全螢幕
- tok/s 於前端由 token 串流即時計算；ctx 用量為字元估算（CJK≈1 token/字），context window 取自 `/api/models/info`

## 備註

- `psutil` 若未安裝會自動 fallback（Windows ctypes），不需改 requirements；建議可加 `psutil` 取得更準確的可用記憶體。
- Sprint 3（Answer Card / Agent Timeline / Knowledge Center 分頁）與 Sprint 4（Inspector tabs / Command Palette 等）尚未實作。
