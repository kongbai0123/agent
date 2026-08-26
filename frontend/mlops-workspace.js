(function () {
    'use strict';
    let apiFetch, apiBase = '', showToast, getActiveProjectId, onWorkspaceOpen, onWorkspaceClose;
    let overview = { datasets: [], experiments: [], models: [], adapters: [] };
    const el = id => document.getElementById(id);
    const escapeHtml = value => String(value ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    const statusLabel = value => ({
        draft: '草稿', queued: '等待中', preparing: '準備中', running: '執行中',
        completed: '已完成', failed: '失敗', cancelled: '已取消',
        healthy: '健康', degraded: '降級', unavailable: '無法使用', disabled: '已停用', unknown: '未知',
        candidate: '候選版本', production: '正式版本', archived: '已封存'
    })[value] || value || '未知';
    const componentLabel = value => ({
        provider: '模型供應商', mcp: 'MCP 擴充', hermes: 'Hermes 服務',
        training_adapter: '訓練 Adapter'
    })[value] || value;

    async function request(path, options = {}) {
        const response = await apiFetch(`${apiBase}${path}`, options);
        const payload = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(payload?.detail?.message || payload?.message || `請求失敗（${response.status}）`);
        return payload;
    }
    function card(title, meta, body = '', action = '') {
        return `<article class="mlops-item"><div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(meta)}</span>${body}</div>${action}</article>`;
    }
    async function refresh() {
        const projectId = getActiveProjectId?.();
        if (!projectId) {
            el('mlops-status').textContent = '請先選擇專案，MLOps 資料不會跨專案共用。';
            overview = { datasets: [], experiments: [], models: [], adapters: [] }; render(); return;
        }
        try {
            overview = await request(`/api/mlops/overview?project_id=${encodeURIComponent(projectId)}`);
            const health = await request(`/api/operations/health?project_id=${encodeURIComponent(projectId)}`);
            overview.health = health.components || [];
            el('mlops-status').textContent = `已載入專案；${overview.datasets.length} 個資料集、${overview.experiments.length} 個實驗。`;
            render();
        } catch (error) {
            el('mlops-status').textContent = error.message; showToast?.(error.message, 'error');
        }
    }
    function render() {
        const metrics = [
            ['資料集', overview.datasets.length], ['實驗', overview.experiments.length],
            ['已登錄模型', overview.models.length], ['可用 Adapter', overview.adapters.length]
        ];
        el('mlops-summary').innerHTML = metrics.map(([label, value]) => `<article><strong>${value}</strong><span>${label}</span></article>`).join('');
        el('mlops-dataset-list').innerHTML = overview.datasets.length ? overview.datasets.map(item => card(item.name, `${item.version_count} 個版本`, `<p>${escapeHtml(item.description || '尚無說明')}</p>`)).join('') : '<p class="mlops-empty">尚未建立資料集。</p>';
        const versions = overview.datasets.flatMap(dataset => dataset.latest_version ? [dataset.latest_version] : []);
        const select = el('mlops-dataset-version');
        select.innerHTML = versions.length ? versions.map(v => `<option value="${escapeHtml(v.version_id)}">${escapeHtml(v.dataset_name)} · 第 ${v.revision} 版</option>`).join('') : '<option value="">請先建立資料集版本</option>';
        el('mlops-experiment-list').innerHTML = overview.experiments.length ? overview.experiments.map(item => card(item.name, `${statusLabel(item.status)} · ${item.adapter_id}`, item.metrics?.accuracy !== undefined ? `<p>評估準確率：${Math.round(item.metrics.accuracy * 1000) / 10}%</p>` : '', ['draft', 'failed'].includes(item.status) ? `<button class="btn btn-primary compact" data-train-experiment="${item.experiment_id}">開始訓練</button>` : '')).join('') : '<p class="mlops-empty">尚未建立實驗。</p>';
        el('mlops-model-list').innerHTML = overview.models.length ? overview.models.map(item => card(item.name, `${statusLabel(item.stage)} · ${item.adapter_id}`, `<p>模型版本：${escapeHtml(item.model_version_id)}</p>`)).join('') : '<p class="mlops-empty">完成訓練後，模型版本會出現在這裡。</p>';
        el('mlops-health-list').innerHTML = (overview.health || []).map(item => card(`${componentLabel(item.component_type)} · ${item.component_id}`, `${statusLabel(item.status)} · ${item.reason_code}`, `<p>最後檢查：${escapeHtml(item.checked_at || '未知')}</p>`)).join('') || '<p class="mlops-empty">尚無健康紀錄。</p>';
    }
    async function parseRows(file) {
        if (!file) throw new Error('請選擇 JSON 或 JSONL 資料檔案。');
        const text = await file.text();
        if (file.name.toLowerCase().endsWith('.jsonl')) return text.split(/\r?\n/).filter(Boolean).map(line => JSON.parse(line));
        const value = JSON.parse(text); return Array.isArray(value) ? value : value.rows;
    }
    function init(config) {
        ({ apiFetch, apiBase = '', showToast, getActiveProjectId, onWorkspaceOpen, onWorkspaceClose } = config);
        document.querySelectorAll('[data-mlops-tab]').forEach(button => button.addEventListener('click', () => {
            document.querySelectorAll('[data-mlops-tab]').forEach(item => item.classList.toggle('active', item === button));
            document.querySelectorAll('[data-mlops-panel]').forEach(panel => { panel.hidden = panel.dataset.mlopsPanel !== button.dataset.mlopsTab; });
        }));
        el('mlops-close').addEventListener('click', () => onWorkspaceClose?.());
        el('mlops-refresh').addEventListener('click', refresh);
        el('mlops-create-dataset').addEventListener('click', async () => {
            try {
                const projectId = getActiveProjectId?.(); if (!projectId) throw new Error('請先選擇專案。');
                const name = el('mlops-dataset-name').value.trim(); if (!name) throw new Error('請輸入資料集名稱。');
                const rows = await parseRows(el('mlops-dataset-file').files[0]);
                const dataset = await request('/api/mlops/datasets', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId, name }) });
                await request(`/api/mlops/datasets/${encodeURIComponent(dataset.dataset_id)}/versions`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rows }) });
                showToast?.('資料集已建立並保存在本機。', 'success'); await refresh();
            } catch (error) { showToast?.(error.message, 'error'); }
        });
        el('mlops-create-experiment').addEventListener('click', async () => {
            try {
                const projectId = getActiveProjectId?.(), name = el('mlops-experiment-name').value.trim(), datasetVersion = el('mlops-dataset-version').value;
                if (!projectId || !name || !datasetVersion) throw new Error('請選擇專案、輸入名稱並選擇資料集版本。');
                await request('/api/mlops/experiments', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ project_id: projectId, name, dataset_version_id: datasetVersion, parameters: { seed: 42 } }) });
                showToast?.('實驗已建立。', 'success'); await refresh();
            } catch (error) { showToast?.(error.message, 'error'); }
        });
        el('mlops-experiment-list').addEventListener('click', async event => {
            const button = event.target.closest('[data-train-experiment]'); if (!button) return;
            button.disabled = true;
            try { await request(`/api/mlops/experiments/${encodeURIComponent(button.dataset.trainExperiment)}/train`, { method: 'POST' }); showToast?.('本機訓練已開始。', 'success'); await refresh(); }
            catch (error) { showToast?.(error.message, 'error'); button.disabled = false; }
        });
    }
    window.workbenchMLOps = {
        init,
        open: () => { onWorkspaceOpen?.(); void refresh(); },
        close: () => onWorkspaceClose?.(),
        refresh
    };
})();
