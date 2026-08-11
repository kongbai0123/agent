# Python 相依安全盤點

## 2026-07-28 直接依賴修復結果

已依序升級並在每一步執行完整測試：

| 套件 | 原版本 | 新版本 | 驗證 |
|---|---:|---:|---|
| `requests` | 2.32.3 | 2.34.2 | 251 passed |
| `python-multipart` | 0.0.9 | 0.0.32 | 251 passed |
| `pypdf` | 4.2.0 | 6.14.2 | 251 passed |

`pip-audit` 的已知弱點由 **113 筆／13 套件**降至 **69 筆／10 套件**。剩餘項目集中在 `starlette`、`docling`、LangChain 系列、`transformers`、`pillow` 與 `lxml`；這些套件牽涉 FastAPI 或文件／NLI 模型堆疊，需建立相容性分支後成組升級，不能解讀為本版本已達零弱點。

目前唯一測試警告是舊版 Starlette 仍使用 `import multipart` 的 PendingDeprecationWarning，不影響 251 項測試通過；後續應透過 FastAPI／Starlette 成組升級消除。

盤點日期：2026-07-27
來源：`backend/requirements.txt` 的固定版本
工具：`pip-audit`（OSV / PyPI 公開弱點資料）

## 目前基線

| 指標 | 數量 |
|---|---:|
| 解析後套件 | 179 |
| 含已知弱點的套件 | 13 |
| 已知弱點紀錄 | 113 |

直接相依中目前被標記的套件為：

- `pypdf==4.2.0`
- `docling==2.15.0`
- `python-multipart==0.0.9`
- `langchain==0.2.5`
- `langchain-community==0.2.5`
- `requests==2.32.3`

間接相依中目前被標記的套件為：

- `starlette==0.37.2`
- `pillow==11.3.0`
- `langchain-core==0.2.43`
- `langchain-text-splitters==0.2.4`
- `langsmith==0.1.147`
- `transformers==4.57.6`
- `lxml==5.4.0`

## 判讀

這是「庫存盤點」，不是已修復聲明。部分修正版跨越主要版本，直接一次升級
`docling`、LangChain、Transformers 或 Starlette 可能破壞文件解析、向量檢索與
模型載入契約，因此不可只為讓掃描歸零而跳過相容性測試。

公開版本目前應視為 blocked，直到至少完成：

1. 先升級可低風險替換的直接相依（`requests`、`python-multipart`、`pypdf`），執行完整測試。
2. 建立文件匯入、RAG、NLI、PDF 的固定回歸資料，再分批升級 Docling / LangChain / Transformers。
3. FastAPI 與 Starlette 必須成組升級，並重跑 API 安全、CORS、SSE 與檔案上傳測試。
4. 每次 CI 都產生 `pip-audit.json`，趨勢只能下降；基線穩定後再把新增弱點設為阻擋 gate。

## 重現方式

```powershell
uvx pip-audit -r backend/requirements.txt --format json --output runtime/ci-artifacts/pip-audit.json
```

`pip-audit` 在發現弱點時使用 exit code 1。CI 會保留報告並顯示警告；工具本身無法完成
（exit code 大於 1）才會使工作失敗。這避免把「已有基線債務」誤判成掃描器故障，同時
確保公開前不會遺失可稽核資料。
