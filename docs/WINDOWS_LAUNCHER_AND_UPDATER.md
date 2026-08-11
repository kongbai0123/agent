# Windows 啟動器與更新器

`LocalAIWorkbench.exe` 只負責啟動受控 PowerShell 入口；應用程式本體仍由 repository 內的 Python、前端與 scripts 組成。

## 啟動流程

```text
LocalAIWorkbench.exe
  -> scripts/launch_workbench.ps1
       -> scripts/update_workbench.ps1 -Mode Check
       -> scripts/start_workbench.ps1
            -> 啟動載入頁
            -> 啟動 loopback FastAPI
            -> 開啟 Edge 或 Chrome app 視窗
            -> 視窗關閉後清理本次擁有的程序
```

若啟用 Hermes，launcher 會先解析固定 manifest、驗證安裝 receipt 與安全政策，再啟動或監控自己擁有的 sidecar。它不會停止無正確 labels 的外部容器。

## 建置啟動器

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\scripts\build_launcher.ps1
```

產物為 `LocalAIWorkbench.exe` 與 `launcher/LocalAIWorkbench.ico`。

## 更新安全

更新器只接受可驗證的 Git fast-forward 更新。工作樹有未提交變更、遠端歷史分歧、公開樹檢查失敗或驗證失敗時都會停止，不會使用 `reset --hard` 或強制覆蓋本機資料。

`runtime/`、`projects/`、`workspaces/`、`artifacts/`、本機設定與秘密不屬於更新內容。
