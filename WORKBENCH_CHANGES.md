# Local AI Workbench 改版紀錄

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
