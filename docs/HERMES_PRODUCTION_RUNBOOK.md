# Hermes 生產化與分批啟用手冊

## 安全邊界

- Workbench UI 與 Basic Chat 保持主體；Hermes 仍是可選的 loopback sidecar。
- 生產流量只允許經過已釘選版本的 Docker 部署。
- Project Skills 工具只允許單一專案、唯讀 Docker canary；工具開啟時禁止擴大文字流量。
- 任何健康、監控資料庫、能力驗證或斷路器異常都會關閉 Hermes 路由，回到 Basic Chat。
- 監控資料只保存固定事件種類、延遲及 cohort 雜湊，不保存 prompt、回覆、Session ID、Project ID、路徑或憑證。

## Rollout 階梯

升級只能前進一階；降級可直接回到任一較低階段。

| 目前階段 | 下一階段 | 目前 cohort 最少完成樣本 | 最低成功率 |
| --- | --- | ---: | ---: |
| Disabled | Canary | 0 | 啟動與能力檢查通過 |
| Canary | 5% | 20 | 95% |
| 5% | 25% | 50 | 97% |
| 25% | 50% | 100 | 98% |
| 50% | All | 200 | 99% |

每次升級還要求：連續兩次背景健康檢查成功、Runs 能力完整、斷路器關閉、持久化 metrics 可用、當前 cohort 沒有工具政策拒絕、Basic Chat fallback 保持啟用。

## 操作方式

1. 在 Hermes 設定面板查看「目前階段／下一階段／可否晉升／阻擋原因」。
2. Project Skills 工具仍在使用時，先停用工具並單獨儲存。
3. 停用工具會切換到新的 NoTools cohort；必須重新累積 Canary 證據，不沿用工具模式的樣本。
4. 只有下一階段會被允許；若證據不足，儲存會被後端拒絕，原設定不變。
5. 發生異常時使用「立即回復 Basic Chat」。這會一次關閉 rollout 與工具，但不刪除 Hermes runtime、模型或專案資料。

## 自動監控與恢復

- Launcher 每 10 秒檢查一次釘選的 Hermes health contract。
- 連續 3 次失敗後，只會停止並重啟帶有 Workbench ownership labels 的容器。
- 每次 Workbench 啟動最多自動重啟 2 次；超過後停止並報錯，不會接管未知容器。
- Backend 同時執行應用層 authenticated probe；健康狀態、斷路器與 SQLite metrics 共同決定是否路由。

## 驗證與稽核證據

安全驗證工具：

```powershell
.\.venv\Scripts\python.exe scripts\hermes_production_ops.py verify
.\.venv\Scripts\python.exe scripts\hermes_production_ops.py monitor --samples 6 --interval-seconds 10
```

驗證證據寫入 `runtime/hermes/evidence/`。證據採 allowlist 格式，禁止寫入 API key、authorization、canary/session/project 身分、環境內容與專案路徑。
