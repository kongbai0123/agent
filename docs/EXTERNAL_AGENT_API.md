# Workbench 對外 Agent API

Workbench 可以在安裝電腦上簽發自己的 API Key，讓同一台電腦上的 n8n 或其他受信任程式建立、查詢及取消 Agent 工作。API Key 不是模型供應商的金鑰，也不會讓外部程式取得整套 Workbench 管理權限。

## 從哪裡設定

1. 在左側欄開啟「整合」。
2. 選擇要授權的 Project。
3. 到「權限方案」納入「Workbench 對外 API」，設定該 Project 可以使用的整合與資源。
4. 到「對外 API」建立金鑰，選擇能力、到期日、每分鐘上限及每日上限。
5. 立即把完整金鑰存入 n8n Credentials 或其他外部系統的秘密儲存區。

完整金鑰只顯示一次。Workbench 之後只顯示前綴、狀態及最後使用時間，無法替使用者找回原金鑰；遺失時請旋轉金鑰，並把新值更新到呼叫端。

## 安裝電腦綁定

每套 Workbench 首次啟用時會建立隨機安裝身分及驗證資料。簽發的金鑰含有該安裝的隨機標記，驗證所需的 HMAC 秘密則放在 Windows DPAPI 保護的本機秘密庫；SQLite 只保存查找前綴與不可逆摘要，不保存完整金鑰。

因此，只複製資料庫或 API Key 到另一套 Workbench，不能讓另一套安裝代替原安裝驗證。若 DPAPI 資料遺失、無法解密或安裝身分重設，Workbench 會停止對外驗證；重設安裝身分會撤銷全部舊金鑰，外部系統必須改用新簽發的金鑰。

## 公開呼叫契約

「對外 API」頁會顯示此電腦目前後端埠所對應的基底位址，例如：

```text
http://127.0.0.1:8000/api/public/v1
```

所有公開端點都必須帶入：

```http
Authorization: Bearer wbk_...
```

建立工作還必須帶入 8 至 128 個不含空白的 `Idempotency-Key`。呼叫端對同一個邏輯請求重試時，必須重用相同值；相同值若搭配不同內容，Workbench 會以 `409` 拒絕，避免重複執行或誤把兩個工作視為同一個工作。

### 讀取可用能力

```http
GET /api/public/v1/capabilities
Authorization: Bearer wbk_...
```

需要 `capabilities:read`，只回傳該 Key 綁定 Project 當下允許看見的能力。

### 建立 Agent 工作

```http
POST /api/public/v1/runs
Authorization: Bearer wbk_...
Idempotency-Key: 4e9efc14-73ac-4fab-a42b-62c85f7bb05c
Content-Type: application/json

{
  "message": "整理本週待處理事項並列出優先順序",
  "use_rag": true
}
```

可選擇傳入 `model`；呼叫端不能指定 Workbench Session，也不能附加任意 metadata。成功接受時回傳 `202` 與由伺服器產生的 `run_id`。

### 查詢與取消

```http
GET /api/public/v1/runs/{run_id}
Authorization: Bearer wbk_...
```

需要 `runs:read`。只能讀取同一把 Key 所屬 Project 的工作。

```http
POST /api/public/v1/runs/{run_id}/cancel
Authorization: Bearer wbk_...
```

需要 `runs:cancel`。取消已產生外部副作用的工作，不代表第三方服務會自動復原。

## n8n 呼叫方式

在同一台電腦的 n8n，可使用 HTTP Request 節點：

- Method：`POST`
- URL：從「整合 → 對外 API」複製，後面加上 `/runs`
- Authentication：在 n8n Credentials 中保存 Bearer API Key
- Header：加入固定或由工作項目產生的 `Idempotency-Key`
- Body Content Type：JSON
- Body：至少包含 `message`

建議為自動化建立專用 Project，只授權必要的 Connection、資源及能力。`runs:create` 會讓 Agent 使用該 Project 已放行的工具，所以不要把高權限日常 Project 直接交給廣泛的外部流程。

## 限制與安全行為

- 公開 `POST`／`PUT`／`PATCH` 要求內容上限為 128 KiB；建立工作中的 `message` 上限為 100,000 字元。
- API Key 同時受自身 scope、啟用狀態、到期日、速率／每日上限及 Project 整合權限約束；任一層拒絕就不會執行。
- 需要人工批准的外部寫入仍會停在 Workbench，MVP 不提供從公開 API 代替使用者批准的端點。
- 管理、簽發、旋轉、撤銷與安裝重設端點不是公開 API，只接受本機 Workbench 工作階段。
- 稽核只保存遮罩後的操作資料，不保存 Authorization、完整 API Key 或原始秘密。
- Workbench 預設只監聽 loopback。其他電腦或雲端 n8n 無法直接連線；請勿把本機後端埠直接暴露到網際網路。跨電腦存取需要另行設計受控 HTTPS Gateway、來源限制與額外身分驗證。

常見回應：

- `401`：未提供、格式錯誤、已撤銷或不屬於此安裝的金鑰。
- `403`：Key scope 或 Project 統一權限不允許。
- `409`：`Idempotency-Key` 已搭配不同請求使用。
- `413`：要求內容超過 128 KiB。
- `429`：達到速率或每日請求上限；依 `Retry-After` 延後重試。
- `503`：Runtime 或安裝驗證資料暫時不可用。
