/* Independent Hermes sidecar settings panel. The host injects a container and dependencies. */

(() => {
    'use strict';

    const DEFAULTS = Object.freeze({
        enabled: false,
        baseUrl: 'http://127.0.0.1:8642',
        apiKeyEnv: 'HERMES_API_SERVER_KEY',
        rolloutMode: 'disabled',
        rolloutPercentage: 0,
        canarySessionIds: [],
        toolsEnabled: false,
    });
    const ROLLOUT_MODES = new Set(['disabled', 'canary', 'percentage', 'all']);
    const ENV_NAME_PATTERN = /^[A-Za-z_][A-Za-z0-9_]{0,79}$/;
    const READONLY_TOOL_POLICY_PROFILE = 'project-readonly-v1';
    const READONLY_TOOL_CAPABILITY = 'hermes.project.read';
    const PERCENTAGE_LADDER = Object.freeze([5, 25, 50]);
    const ROLLOUT_STAGE_LABELS = Object.freeze([
        '關閉',
        'Canary',
        '5%',
        '25%',
        '50%',
        '全量',
    ]);

    const state = {
        deps: null,
        container: null,
        refs: {},
        settings: {},
        settingsLoaded: false,
        settingsLoading: false,
        settingsSaving: false,
        settingsError: null,
        status: null,
        statusLoading: false,
        statusProbing: false,
        statusError: null,
        settingsRequestId: 0,
        statusRequestId: 0,
    };

    function element(tag, className = '', text = null) {
        const node = document.createElement(tag);
        if (className) node.className = className;
        if (text !== null) node.textContent = String(text);
        return node;
    }

    function button(text, className, action) {
        const node = element('button', className, text);
        node.type = 'button';
        node.dataset.hermesAction = action;
        return node;
    }

    function input(id, type = 'text') {
        const node = document.createElement('input');
        node.id = id;
        node.type = type;
        node.className = 'settings-input hermes-settings-input';
        return node;
    }

    function helpItems(value) {
        const items = Array.isArray(value) ? value : value ? [value] : [];
        return items.map(item => String(item || '').trim()).filter(Boolean);
    }

    function fieldTitle(id, labelText, helpContent = []) {
        const row = element('div', 'hermes-field-title-row');
        const label = element('label', 'settings-label', labelText);
        label.htmlFor = id;
        row.appendChild(label);

        const items = helpItems(helpContent);
        if (!items.length) return { row, help: null };

        const helpId = `${id}-help`;
        const toggle = element('span', 'hermes-field-help-trigger');
        toggle.id = `${id}-help-trigger`;
        toggle.tabIndex = 0;
        toggle.setAttribute('aria-label', `「${labelText}」說明`);
        toggle.setAttribute('aria-describedby', helpId);
        const helpIcon = element('i');
        helpIcon.dataset.lucide = 'lightbulb';
        helpIcon.setAttribute('aria-hidden', 'true');
        toggle.appendChild(helpIcon);

        const help = element('div', 'hermes-field-help');
        help.id = helpId;
        help.setAttribute('role', 'tooltip');
        const list = element('ul', 'hermes-field-help-list');
        items.forEach(item => list.appendChild(element('li', '', item)));
        help.appendChild(list);
        const disclosure = element('span', 'hermes-field-help-disclosure');
        disclosure.append(toggle, help);
        row.appendChild(disclosure);
        return { row, help };
    }

    function labeledField(id, labelText, control, helpContent = []) {
        const group = element('div', 'settings-group hermes-settings-field');
        const title = fieldTitle(id, labelText, helpContent);
        group.append(title.row, control);
        return group;
    }

    function toggleField(id, labelText, helpContent = []) {
        const control = input(id, 'checkbox');
        control.className = 'hermes-settings-toggle';
        const group = element('div', 'settings-group toggle-group hermes-settings-field');
        const copy = element('div', 'hermes-settings-toggle-copy');
        const title = fieldTitle(id, labelText, helpContent);
        copy.appendChild(title.row);
        group.append(copy, control);
        return { group, control };
    }

    function resolveContainer(value) {
        if (typeof value === 'string') return document.querySelector(value);
        return value && typeof value.replaceChildren === 'function' ? value : null;
    }

    function apiBase(value) {
        return String(value || '').replace(/\/+$/, '');
    }

    function errorMessage(payload, status) {
        const detail = payload && typeof payload.detail === 'object'
            ? payload.detail
            : payload?.detail;
        const message = typeof detail === 'string'
            ? detail
            : detail?.message || detail?.error || detail?.code
                || payload?.message || payload?.error;
        return String(message || `HTTP ${status}`);
    }

    async function request(path, options = {}) {
        if (!state.deps?.apiFetch) throw new Error('Hermes 設定尚未初始化。');
        const response = await state.deps.apiFetch(`${state.deps.apiBase}${path}`, options);
        let payload = {};
        try {
            payload = await response.json();
        } catch (_error) {
            payload = {};
        }
        if (!response.ok || payload?.success === false) {
            const error = new Error(errorMessage(payload, response.status));
            error.status = response.status;
            throw error;
        }
        return payload;
    }

    function unwrapSettings(payload) {
        if (payload?.settings && typeof payload.settings === 'object') {
            return { ...payload.settings };
        }
        const settings = payload && typeof payload === 'object' ? { ...payload } : {};
        delete settings.success;
        delete settings.effective;
        delete settings.reload_required;
        return settings;
    }

    function unwrapStatus(payload) {
        for (const key of ['status', 'probe', 'hermes', 'result']) {
            if (payload?.[key] && typeof payload[key] === 'object') {
                return { ...payload[key] };
            }
        }
        return payload && typeof payload === 'object' ? { ...payload } : {};
    }

    function boolSetting(value, fallback = false) {
        if (typeof value === 'boolean') return value;
        if (value === 1 || value === '1' || value === 'true') return true;
        if (value === 0 || value === '0' || value === 'false') return false;
        return fallback;
    }

    function numberSetting(value, fallback) {
        const parsed = Number(value);
        return Number.isFinite(parsed) ? parsed : fallback;
    }

    function formValues(settings) {
        const mode = String(settings.hermes_rollout_mode || DEFAULTS.rolloutMode).toLowerCase();
        return {
            enabled: boolSetting(settings.hermes_enabled, DEFAULTS.enabled),
            baseUrl: String(settings.hermes_base_url || DEFAULTS.baseUrl),
            apiKeyEnv: String(settings.hermes_api_key_env || DEFAULTS.apiKeyEnv),
            rolloutMode: ROLLOUT_MODES.has(mode) ? mode : DEFAULTS.rolloutMode,
            rolloutPercentage: numberSetting(
                settings.hermes_rollout_percentage,
                DEFAULTS.rolloutPercentage,
            ),
            canarySessionIds: Array.isArray(settings.hermes_canary_session_ids)
                ? settings.hermes_canary_session_ids.map(value => String(value))
                : [],
            toolsEnabled: boolSetting(
                settings.hermes_tools_enabled,
                DEFAULTS.toolsEnabled,
            ),
        };
    }

    function persistedRolloutMode() {
        return formValues(state.settings).rolloutMode;
    }

    function rolloutStage(mode, percentage) {
        if (mode === 'disabled') return 0;
        if (mode === 'canary') return 1;
        if (mode === 'all') return 5;
        if (mode !== 'percentage') return null;
        const index = PERCENTAGE_LADDER.indexOf(Number(percentage));
        return index < 0 ? null : index + 2;
    }

    function persistedRolloutStage() {
        const values = formValues(state.settings);
        return rolloutStage(values.rolloutMode, values.rolloutPercentage);
    }

    function rolloutExpansionError(targetMode, targetPercentage = null) {
        const currentStage = persistedRolloutStage();
        const percentage = targetMode === 'percentage'
            ? Number(targetPercentage ?? state.refs.rolloutPercentage?.value)
            : 0;
        const targetStage = rolloutStage(targetMode, percentage);
        if (targetStage === null) {
            return '分批比例僅允許 5%、25% 或 50%。';
        }
        if (currentStage === null) {
            return '現有 Hermes rollout 階段無效，請先重設為關閉。';
        }
        if (targetStage <= currentStage) return '';
        if (boolSetting(state.settings.hermes_tools_enabled, false)) {
            return '請先關閉 Hermes 專案唯讀工具並儲存，再擴大文字流量。';
        }
        if (targetStage > currentStage + 1) {
            return 'Hermes 流量必須依序由 Canary、5%、25%、50% 到全量擴大。';
        }
        return '';
    }

    function setFeedback(message, kind = '') {
        const feedback = state.refs.feedback;
        if (!feedback) return;
        feedback.textContent = String(message || '');
        feedback.className = `hermes-settings-feedback${kind ? ` is-${kind}` : ''}`;
        feedback.hidden = !message;
        feedback.setAttribute('role', kind === 'error' ? 'alert' : 'status');
    }

    function yesNo(value, unknown = '未知') {
        if (value === true) return '是';
        if (value === false) return '否';
        return unknown;
    }

    function safeScalar(value, fallback = '未知') {
        if (typeof value === 'string' || typeof value === 'number') return String(value);
        return fallback;
    }

    function safeCount(value) {
        const parsed = Number(value);
        return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : 0;
    }

    function countTotal(value) {
        if (!value || typeof value !== 'object' || Array.isArray(value)) return 0;
        return Object.values(value).reduce((total, item) => total + safeCount(item), 0);
    }

    function rolloutStageLabel(value) {
        const labels = {
            disabled: '關閉',
            canary: 'Canary',
            percentage_5: '5%',
            percentage_25: '25%',
            percentage_50: '50%',
            all: '全量',
        };
        return labels[value] || safeScalar(value);
    }

    function hasVerifiedReadonlyToolPolicy(status = state.status) {
        if (!status || typeof status !== 'object') return false;
        const dockerAttestation = status.docker_attestation;
        return Boolean(status.deployment_mode === 'docker'
            && status.tool_policy_profile === READONLY_TOOL_POLICY_PROFILE
            && dockerAttestation
            && typeof dockerAttestation === 'object'
            && dockerAttestation.verified === true);
    }

    function statusEntries(status) {
        const configuration = status.configuration && typeof status.configuration === 'object'
            ? status.configuration
            : {};
        const health = status.health && typeof status.health === 'object'
            ? status.health
            : {};
        const rollout = status.rollout && typeof status.rollout === 'object'
            ? status.rollout
            : {};
        const healthLabel = typeof status.health === 'string'
            ? status.health
            : health.status || health.state || status.state || status.status;
        const reachable = status.reachable ?? health.reachable ?? health.ok ?? status.healthy;
        const configured = status.configured
            ?? configuration.configured
            ?? status.api_key_configured;
        const enabled = status.enabled ?? configuration.enabled;
        const endpoint = status.base_url ?? status.endpoint ?? configuration.base_url;
        const rolloutMode = status.rollout_mode ?? rollout.mode;
        const toolsEnabled = status.tools_enabled ?? configuration.tools_enabled;
        const operations = status.operations && typeof status.operations === 'object'
            ? status.operations
            : {};
        const circuit = operations.circuit_breaker
            && typeof operations.circuit_breaker === 'object'
            ? operations.circuit_breaker
            : {};
        const routingCounts = operations.routing_counts
            && typeof operations.routing_counts === 'object'
            ? operations.routing_counts
            : {};
        const outcomeCounts = operations.outcome_counts
            && typeof operations.outcome_counts === 'object'
            ? operations.outcome_counts
            : {};
        const fallbackCounts = operations.fallback_counts
            && typeof operations.fallback_counts === 'object'
            ? operations.fallback_counts
            : {};
        const supervisor = status.supervisor && typeof status.supervisor === 'object'
            ? status.supervisor
            : {};
        const rolloutControl = status.rollout_control
            && typeof status.rollout_control === 'object'
            ? status.rollout_control
            : {};
        const rolloutBlockers = Array.isArray(rolloutControl.blockers)
            ? rolloutControl.blockers
            : [];
        const firstRolloutBlocker = rolloutBlockers.find(
            item => item && typeof item === 'object',
        );
        const dockerAttestation = status.docker_attestation
            && typeof status.docker_attestation === 'object'
            ? status.docker_attestation
            : {};
        const lastProbe = status.last_probe_at ?? health.checked_at ?? status.checked_at;
        return [
            ['服務狀態', safeScalar(healthLabel)],
            ['可連線', yesNo(reachable)],
            ['Hermes 已啟用', yesNo(enabled)],
            ['API Key 已由環境提供', yesNo(configured)],
            ['Loopback 端點', safeScalar(endpoint)],
            ['Rollout', rolloutMode === 'percentage'
                ? `percentage (${safeScalar(rollout.percentage, '0')}%)`
                : safeScalar(rolloutMode)],
            ['工具模式', yesNo(toolsEnabled)],
            ['部署模式', safeScalar(status.deployment_mode)],
            ['工具政策', safeScalar(status.tool_policy_profile)],
            ['Docker 驗證', yesNo(dockerAttestation.verified)],
            ['監控狀態', safeScalar(supervisor.state)],
            ['目前階段', rolloutStageLabel(rolloutControl.current_stage)],
            ['下一階段', rolloutControl.next_stage == null
                ? '無'
                : rolloutStageLabel(rolloutControl.next_stage)],
            ['可晉升', yesNo(rolloutControl.can_promote)],
            ['晉升阻擋', firstRolloutBlocker
                ? safeScalar(firstRolloutBlocker.message || firstRolloutBlocker.code)
                : '無'],
            ['健康原因', safeScalar(health.reason)],
            ['健康延遲', health.latency_ms == null
                ? '未知'
                : `${safeScalar(health.latency_ms)} ms`],
            ['熔斷狀態', safeScalar(circuit.state)],
            ['連續失敗', `${safeCount(circuit.consecutive_failures)} / ${safeCount(circuit.failure_threshold)}`],
            ['熔斷重試', `${safeScalar(circuit.retry_after_seconds, '0')} s`],
            ['路由摘要', `Hermes ${safeCount(routingCounts.hermes)} / 基本 ${safeCount(routingCounts.basic_chat)}`],
            ['結果摘要', `成功 ${safeCount(outcomeCounts.success)} / 失敗 ${safeCount(outcomeCounts.failure)} / 放棄 ${safeCount(outcomeCounts.abandoned)}`],
            ['Fallback 總數', safeScalar(countTotal(fallbackCounts), '0')],
            ['最後檢查', safeScalar(lastProbe, '尚未檢查')],
        ];
    }

    function renderStatus() {
        const panel = state.refs.statusBody;
        if (!panel) return;
        panel.replaceChildren();
        if (state.statusLoading || state.statusProbing) {
            const message = state.statusProbing ? '正在執行 Hermes 健康檢查…' : '正在載入 Hermes 狀態…';
            const loading = element('div', 'hermes-status-message is-loading', message);
            loading.setAttribute('role', 'status');
            panel.appendChild(loading);
            return;
        }
        if (state.statusError) {
            const error = element('div', 'hermes-status-message is-error', state.statusError);
            error.setAttribute('role', 'alert');
            panel.appendChild(error);
            return;
        }
        if (!state.status) {
            panel.appendChild(element('div', 'hermes-status-message', '尚未取得 Hermes 狀態。'));
            return;
        }

        const list = element('dl', 'hermes-status-list');
        for (const [label, value] of statusEntries(state.status)) {
            const row = element('div', 'hermes-status-row');
            row.append(
                element('dt', 'hermes-status-label', label),
                element('dd', 'hermes-status-value', value),
            );
            list.appendChild(row);
        }
        panel.appendChild(list);
        const message = state.status.message || state.status.detail;
        if (typeof message === 'string' && message.trim()) {
            panel.appendChild(element('p', 'hermes-status-detail', message));
        }
    }

    function updateConditionalFields({ reset = false } = {}) {
        const mode = state.refs.rolloutMode?.value || DEFAULTS.rolloutMode;
        if (reset) {
            if (mode === 'disabled' || mode === 'canary') {
                state.refs.rolloutPercentage.value = '0';
            } else if (mode === 'all') {
                state.refs.rolloutPercentage.value = '100';
            } else {
                const current = formValues(state.settings);
                state.refs.rolloutPercentage.value = String(
                    current.rolloutMode === 'all'
                        ? PERCENTAGE_LADDER[PERCENTAGE_LADDER.length - 1]
                        : current.rolloutMode === 'percentage'
                            && PERCENTAGE_LADDER.includes(current.rolloutPercentage)
                            ? current.rolloutPercentage
                            : PERCENTAGE_LADDER[0],
                );
            }
        }
        state.refs.percentageField.hidden = mode !== 'percentage';
        state.refs.canaryField.hidden = mode !== 'canary';
        updateBusyState();
    }

    function updateRolloutStageControls() {
        const currentStage = persistedRolloutStage();
        const persistedTools = boolSetting(state.settings.hermes_tools_enabled, false);
        const toolsActive = persistedTools || Boolean(state.refs.toolsEnabled?.checked);
        const currentValues = formValues(state.settings);
        const percentageForMode = currentValues.rolloutMode === 'all'
            ? PERCENTAGE_LADDER[PERCENTAGE_LADDER.length - 1]
            : currentValues.rolloutMode === 'percentage'
                && PERCENTAGE_LADDER.includes(currentValues.rolloutPercentage)
                ? currentValues.rolloutPercentage
                : PERCENTAGE_LADDER[0];

        for (const [mode, option] of Object.entries(state.refs.rolloutOptions || {})) {
            const transitionError = rolloutExpansionError(mode, percentageForMode);
            option.disabled = Boolean(transitionError)
                || (toolsActive && mode !== 'canary');
        }
        for (const [percentage, option] of Object.entries(
            state.refs.rolloutPercentageOptions || {},
        )) {
            option.disabled = Boolean(
                rolloutExpansionError('percentage', Number(percentage)),
            ) || toolsActive;
        }

        if (!state.refs.rolloutStageNotice) return;
        if (currentStage === null) {
            state.refs.rolloutStageNotice.textContent = '現有 rollout 階段無效，請使用「立即回復 Basic Chat」安全重設。';
        } else if (persistedTools) {
            state.refs.rolloutStageNotice.textContent = '專案唯讀工具啟用中；請先關閉並儲存，才能擴大文字流量。';
        } else if (currentStage >= ROLLOUT_STAGE_LABELS.length - 1) {
            state.refs.rolloutStageNotice.textContent = '目前為全量；可隨時降級到任一較低階段。';
        } else {
            state.refs.rolloutStageNotice.textContent = `目前：${ROLLOUT_STAGE_LABELS[currentStage]}；下一個可擴大階段：${ROLLOUT_STAGE_LABELS[currentStage + 1]}。`;
        }
        state.refs.rolloutStageNotice.dataset.hermesRolloutStage = currentStage == null
            ? 'invalid'
            : String(currentStage);
    }

    function updateBusyState() {
        if (!state.container) return;
        const settingsBusy = state.settingsLoading || state.settingsSaving;
        const anyBusy = settingsBusy || state.statusLoading || state.statusProbing;
        state.container.setAttribute('aria-busy', String(anyBusy));

        const editable = [
            state.refs.enabled,
            state.refs.baseUrl,
            state.refs.apiKeyEnv,
            state.refs.rolloutMode,
            state.refs.canarySessionIds,
        ];
        editable.forEach(control => {
            if (control) control.disabled = settingsBusy || !state.settingsLoaded;
        });
        if (state.refs.toolsEnabled) {
            const eligible = hasVerifiedReadonlyToolPolicy();
            const explicitCanary = persistedRolloutMode() === 'canary'
                && state.refs.rolloutMode?.value === 'canary';
            const toolsDisabled = anyBusy
                || !state.settingsLoaded
                || !eligible
                || !explicitCanary;
            state.refs.toolsEnabled.disabled = toolsDisabled;
            state.refs.toolsEnabled.setAttribute('aria-disabled', String(toolsDisabled));
            state.refs.toolsEnabled.dataset.hermesToolsUnavailable = String(!eligible);
            state.refs.toolsEnabled.title = eligible && explicitCanary
                ? '已驗證 Docker 唯讀專案工具政策，且目前為明確 Canary。'
                : eligible
                    ? '請先儲存明確 Canary 階段，才能操作專案唯讀工具。'
                : '僅在 Docker 部署、project-readonly-v1 政策與即時驗證通過後可啟用。';
            if (!eligible) state.refs.toolsEnabled.checked = false;
            if (state.refs.toolsWarning) {
                state.refs.toolsWarning.textContent = eligible && explicitCanary
                    ? '已驗證 Docker 隔離：僅允許明確 Canary 讀取設定中的單一專案。'
                    : eligible
                        ? '工具保持關閉：請先設定並儲存明確 Canary。'
                    : '工具保持關閉：後端尚未通過 Docker 唯讀專案政策的即時驗證。';
                state.refs.toolsWarning.dataset.hermesDockerAttestation = eligible
                    ? 'verified'
                    : 'unverified';
            }
        }
        updateRolloutStageControls();
        if (state.refs.rolloutPercentage) {
            state.refs.rolloutPercentage.disabled = settingsBusy
                || !state.settingsLoaded
                || state.refs.rolloutMode.value !== 'percentage';
        }
        if (state.refs.save) {
            state.refs.save.disabled = anyBusy || !state.settingsLoaded;
        }
        if (state.refs.rollback) {
            state.refs.rollback.disabled = anyBusy || !state.settingsLoaded;
        }
        if (state.refs.reload) state.refs.reload.disabled = settingsBusy;
        if (state.refs.refreshStatus) {
            state.refs.refreshStatus.disabled = state.statusLoading || state.statusProbing;
        }
        if (state.refs.probe) {
            state.refs.probe.disabled = state.statusLoading || state.statusProbing;
        }
    }

    function applySettings(settings) {
        state.settings = { ...settings };
        const values = formValues(settings);
        state.refs.enabled.checked = values.enabled;
        state.refs.baseUrl.value = values.baseUrl;
        state.refs.apiKeyEnv.value = values.apiKeyEnv;
        state.refs.rolloutMode.value = values.rolloutMode;
        state.refs.rolloutPercentage.value = String(values.rolloutPercentage);
        state.refs.canarySessionIds.value = values.canarySessionIds.join('\n');
        state.refs.toolsEnabled.checked = values.toolsEnabled
            && hasVerifiedReadonlyToolPolicy();
        updateConditionalFields();
    }

    function validateLoopbackUrl(value) {
        const raw = String(value || '').trim().replace(/\/+$/, '');
        let parsed;
        try {
            parsed = new URL(raw);
        } catch (_error) {
            throw new Error('Hermes URL 格式無效。');
        }
        if (!['http:', 'https:'].includes(parsed.protocol)) {
            throw new Error('Hermes URL 必須使用 HTTP 或 HTTPS。');
        }
        if (parsed.username || parsed.password || parsed.search || parsed.hash) {
            throw new Error('Hermes URL 不可包含帳密、查詢參數或片段。');
        }
        if (parsed.pathname && parsed.pathname !== '/') {
            throw new Error('Hermes URL 不可包含額外路徑。');
        }
        const host = parsed.hostname.toLowerCase();
        const ipv4Loopback = /^127(?:\.\d{1,3}){3}$/.test(host);
        const ipv6Loopback = host === '::1' || host === '[::1]';
        if (host !== 'localhost' && !ipv4Loopback && !ipv6Loopback) {
            throw new Error('Hermes 必須使用本機 loopback 位址。');
        }
        return raw;
    }

    function canaryIds(value) {
        const result = [];
        const seen = new Set();
        for (const raw of String(value || '').split(/[\n,]+/)) {
            const item = raw.trim();
            if (!item || seen.has(item)) continue;
            if (item.length > 256 || /[\u0000-\u001f\u007f]/.test(item)) {
                throw new Error('Canary Session ID 格式無效。');
            }
            seen.add(item);
            result.push(item);
        }
        if (result.length > 500) throw new Error('Canary Session ID 數量超過上限。');
        return result;
    }

    function percentageForTransition() {
        const current = formValues(state.settings);
        if (current.rolloutMode === 'all') {
            return PERCENTAGE_LADDER[PERCENTAGE_LADDER.length - 1];
        }
        if (current.rolloutMode === 'percentage'
            && PERCENTAGE_LADDER.includes(current.rolloutPercentage)) {
            return current.rolloutPercentage;
        }
        return PERCENTAGE_LADDER[0];
    }

    function restorePersistedRollout(message) {
        const current = formValues(state.settings);
        state.refs.rolloutMode.value = current.rolloutMode;
        state.refs.rolloutPercentage.value = String(current.rolloutPercentage);
        setFeedback(message, 'error');
        updateConditionalFields();
    }

    function handleRolloutModeChange() {
        const targetMode = state.refs.rolloutMode.value;
        const targetPercentage = targetMode === 'percentage'
            ? percentageForTransition()
            : targetMode === 'all' ? 100 : 0;
        state.refs.rolloutPercentage.value = String(targetPercentage);
        const toolsActive = boolSetting(state.settings.hermes_tools_enabled, false)
            || Boolean(state.refs.toolsEnabled?.checked);
        const transitionError = rolloutExpansionError(targetMode, targetPercentage);
        if (transitionError || (toolsActive && targetMode !== 'canary')) {
            restorePersistedRollout(
                transitionError || '請先關閉專案唯讀工具並儲存，再變更文字 rollout。',
            );
            return;
        }
        updateConditionalFields();
    }

    function handleRolloutPercentageChange() {
        const targetPercentage = Number(state.refs.rolloutPercentage.value);
        const toolsActive = boolSetting(state.settings.hermes_tools_enabled, false)
            || Boolean(state.refs.toolsEnabled?.checked);
        const transitionError = rolloutExpansionError('percentage', targetPercentage);
        if (transitionError || toolsActive) {
            state.refs.rolloutPercentage.value = String(percentageForTransition());
            setFeedback(
                transitionError || '請先關閉專案唯讀工具並儲存，再擴大文字 rollout。',
                'error',
            );
        }
        updateBusyState();
    }

    function handleToolsChange() {
        if (state.refs.toolsEnabled.checked
            && state.refs.rolloutMode.value !== 'canary') {
            state.refs.toolsEnabled.checked = false;
            setFeedback('Hermes 專案唯讀工具只能在已儲存的明確 Canary 階段啟用。', 'error');
        }
        updateBusyState();
    }

    function collectSettings() {
        const rolloutMode = state.refs.rolloutMode.value;
        if (!ROLLOUT_MODES.has(rolloutMode)) throw new Error('Rollout 模式無效。');
        const apiKeyEnv = state.refs.apiKeyEnv.value.trim();
        if (!ENV_NAME_PATTERN.test(apiKeyEnv)) {
            throw new Error('API Key 環境變數名稱格式無效。');
        }

        let percentage = Number(state.refs.rolloutPercentage.value);
        const requestedPercentage = percentage;
        let canaries = canaryIds(state.refs.canarySessionIds.value);
        const transitionError = rolloutExpansionError(rolloutMode, requestedPercentage);
        if (transitionError) throw new Error(transitionError);
        if (rolloutMode !== 'canary'
            && (boolSetting(state.settings.hermes_tools_enabled, false)
                || state.refs.toolsEnabled.checked)) {
            throw new Error('請先關閉專案唯讀工具並儲存，再變更文字 rollout。');
        }
        if (rolloutMode === 'disabled') {
            percentage = 0;
            canaries = [];
        } else if (rolloutMode === 'all') {
            percentage = 100;
            canaries = [];
        } else if (rolloutMode === 'percentage') {
            if (!Number.isFinite(percentage) || !PERCENTAGE_LADDER.includes(percentage)) {
                throw new Error('分批比例只能選擇 5%、25% 或 50%。');
            }
            canaries = [];
        } else {
            percentage = 0;
            if (!canaries.length) throw new Error('Canary 模式至少需要一個 Session ID。');
        }

        const toolsEnabled = hasVerifiedReadonlyToolPolicy()
            && state.refs.toolsEnabled.checked;
        const readonlyProjectId = String(
            state.settings.hermes_readonly_project_id || '',
        );
        if (toolsEnabled && !readonlyProjectId.trim()) {
            throw new Error('Hermes 唯讀工具尚未綁定專案。');
        }

        const payload = {
            ...state.settings,
            hermes_enabled: state.refs.enabled.checked,
            hermes_base_url: validateLoopbackUrl(state.refs.baseUrl.value),
            hermes_api_key_env: apiKeyEnv,
            hermes_rollout_mode: rolloutMode,
            hermes_rollout_percentage: percentage,
            hermes_canary_session_ids: canaries,
            hermes_tools_enabled: toolsEnabled,
            hermes_allowed_capabilities: toolsEnabled
                ? [READONLY_TOOL_CAPABILITY]
                : [],
            // There is deliberately no project-id control in this panel. The
            // backend-owned setting is preserved to prevent redirecting an
            // attested sidecar to another project.
            hermes_readonly_project_id: readonlyProjectId,
        };
        // Secrets belong to the sidecar process environment and never to settings.json.
        delete payload.hermes_api_key;
        delete payload.hermes_api_key_secret;
        delete payload.hermes_api_key_ref;
        delete payload.hermes_bearer_token;
        delete payload.hermes_password;
        delete payload.hermes_secret;
        delete payload.hermes_token;
        return payload;
    }

    async function loadSettings() {
        const requestId = ++state.settingsRequestId;
        state.settingsLoading = true;
        state.settingsError = null;
        setFeedback('正在載入 Hermes 設定…');
        updateBusyState();
        try {
            const payload = await request('/api/settings');
            if (requestId !== state.settingsRequestId) return false;
            applySettings(unwrapSettings(payload));
            state.settingsLoaded = true;
            setFeedback('Hermes 設定已載入。', 'success');
            return true;
        } catch (error) {
            if (requestId !== state.settingsRequestId) return false;
            state.settingsLoaded = false;
            state.settingsError = String(error.message || error);
            setFeedback(`無法載入 Hermes 設定：${state.settingsError}`, 'error');
            return false;
        } finally {
            if (requestId === state.settingsRequestId) {
                state.settingsLoading = false;
                updateBusyState();
            }
        }
    }

    async function refreshStatus({ silent = false } = {}) {
        const requestId = ++state.statusRequestId;
        state.statusLoading = true;
        state.statusError = null;
        updateBusyState();
        renderStatus();
        try {
            const payload = await request('/api/hermes/status');
            if (requestId !== state.statusRequestId) return false;
            state.status = unwrapStatus(payload);
            if (state.refs.toolsEnabled && hasVerifiedReadonlyToolPolicy()) {
                state.refs.toolsEnabled.checked = boolSetting(
                    state.settings.hermes_tools_enabled,
                    DEFAULTS.toolsEnabled,
                );
            }
            if (!silent) setFeedback('Hermes 狀態已更新。', 'success');
            return true;
        } catch (error) {
            if (requestId !== state.statusRequestId) return false;
            state.status = null;
            state.statusError = `無法取得 Hermes 狀態：${String(error.message || error)}`;
            return false;
        } finally {
            if (requestId === state.statusRequestId) {
                state.statusLoading = false;
                updateBusyState();
                renderStatus();
            }
        }
    }

    async function save() {
        if (
            !state.settingsLoaded
            || state.settingsSaving
            || state.statusLoading
            || state.statusProbing
        ) return false;
        state.settingsSaving = true;
        state.settingsError = null;
        setFeedback('正在儲存 Hermes 設定…');
        updateBusyState();
        try {
            const nextSettings = collectSettings();
            const response = await request('/api/settings', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify(nextSettings),
            });
            applySettings(unwrapSettings(response));
            state.settingsLoaded = true;
            setFeedback('Hermes 設定已儲存。', 'success');
            state.deps?.showToast?.('Hermes 設定已儲存', 'success');
            await refreshStatus({ silent: true });
            return true;
        } catch (error) {
            state.settingsError = String(error.message || error);
            setFeedback(`Hermes 設定儲存失敗：${state.settingsError}`, 'error');
            state.deps?.showToast?.('Hermes 設定儲存失敗', 'error');
            return false;
        } finally {
            state.settingsSaving = false;
            updateBusyState();
        }
    }

    async function probe() {
        if (state.statusLoading || state.statusProbing) return false;
        const requestId = ++state.statusRequestId;
        state.statusProbing = true;
        state.statusError = null;
        setFeedback('正在檢查 Hermes sidecar…');
        updateBusyState();
        renderStatus();
        try {
            const payload = await request('/api/hermes/probe', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: '{}',
            });
            if (requestId !== state.statusRequestId) return false;
            state.status = unwrapStatus(payload);
            if (state.refs.toolsEnabled && hasVerifiedReadonlyToolPolicy()) {
                state.refs.toolsEnabled.checked = boolSetting(
                    state.settings.hermes_tools_enabled,
                    DEFAULTS.toolsEnabled,
                );
            }
            setFeedback('Hermes 健康檢查完成。', 'success');
            return true;
        } catch (error) {
            if (requestId !== state.statusRequestId) return false;
            state.status = null;
            state.statusError = `Hermes 健康檢查失敗：${String(error.message || error)}`;
            setFeedback(state.statusError, 'error');
            return false;
        } finally {
            if (requestId === state.statusRequestId) {
                state.statusProbing = false;
                updateBusyState();
                renderStatus();
            }
        }
    }

    async function rollback() {
        if (state.settingsSaving || state.settingsLoading
            || state.statusLoading || state.statusProbing) return false;
        const confirmAction = typeof state.deps?.confirmAction === 'function'
            ? state.deps.confirmAction
            : typeof window.confirm === 'function'
                ? window.confirm.bind(window)
                : null;
        if (!confirmAction) {
            setFeedback('無法顯示安全確認視窗，因此未執行回復。', 'error');
            return false;
        }
        const confirmed = confirmAction(
            '要立即停止 Hermes 文字路由與專案工具，回復 Basic Chat 嗎？這不會刪除任何對話、專案或設定資料。',
        );
        if (!confirmed) return false;

        state.settingsSaving = true;
        setFeedback('正在安全回復 Basic Chat…');
        updateBusyState();
        try {
            await request('/api/hermes/rollout/rollback', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            });
            state.settingsSaving = false;
            const refreshed = await refresh();
            if (!refreshed) {
                throw new Error('已送出回復，但無法重新確認最新狀態。');
            }
            setFeedback('已立即回復 Basic Chat；資料均保留。', 'success');
            state.deps?.showToast?.('已回復 Basic Chat，資料均保留。', 'success');
            return true;
        } catch (error) {
            setFeedback(`無法回復 Basic Chat：${String(error.message || error)}`, 'error');
            state.deps?.showToast?.('無法回復 Basic Chat', 'error');
            return false;
        } finally {
            state.settingsSaving = false;
            updateBusyState();
        }
    }

    async function refresh() {
        const results = await Promise.allSettled([loadSettings(), refreshStatus({ silent: true })]);
        return results.every(result => result.status === 'fulfilled' && result.value === true);
    }

    function buildPanel() {
        const root = element('section', 'hermes-settings-panel');
        root.dataset.hermesSettings = 'panel';
        root.setAttribute('aria-labelledby', 'hermes-settings-title');

        const heading = element('div', 'hermes-settings-heading');
        const headingCopy = element('div', 'hermes-settings-heading-copy');
        const title = element('h3', 'hermes-settings-title', 'Hermes Agent Sidecar');
        title.id = 'hermes-settings-title';
        headingCopy.append(
            title,
            element(
                'p',
                'settings-tip',
                'Workbench UI 保持不變；Hermes 以本機隔離服務逐步接入。',
            ),
        );
        const reload = button('重新載入', 'btn btn-secondary compact', 'reload');
        heading.append(headingCopy, reload);

        const feedback = element('div', 'hermes-settings-feedback');
        feedback.setAttribute('aria-live', 'polite');
        feedback.hidden = true;

        const form = element('form', 'hermes-settings-form');
        form.noValidate = true;

        const enabledField = toggleField(
            'hermes-setting-enabled',
            '啟用 Hermes 路由',
            [
                '關閉時，所有聊天維持 Workbench 原本的 Basic Chat 路徑。',
                '啟用後，仍會依 Rollout 規則決定哪些 Session 進入 Hermes。',
            ],
        );
        const baseUrl = input('hermes-setting-base-url', 'url');
        baseUrl.placeholder = DEFAULTS.baseUrl;
        baseUrl.autocomplete = 'off';
        const baseUrlField = labeledField(
            baseUrl.id,
            'Loopback URL',
            baseUrl,
            [
                '只接受 localhost、127.0.0.0/8 或 ::1。',
                '不可包含帳號、密碼或 API Key。',
                '不可加入額外路徑、查詢參數或 fragment。',
            ],
        );

        const apiKeyEnv = input('hermes-setting-api-key-env');
        apiKeyEnv.placeholder = DEFAULTS.apiKeyEnv;
        apiKeyEnv.autocomplete = 'off';
        apiKeyEnv.spellcheck = false;
        const apiKeyField = labeledField(
            apiKeyEnv.id,
            'API Key 環境變數名稱',
            apiKeyEnv,
            [
                '此處只儲存環境變數名稱。',
                '介面不顯示、不接收也不儲存 secret。',
                '實際金鑰由 Hermes 隔離服務的執行環境提供。',
            ],
        );

        const rolloutMode = document.createElement('select');
        rolloutMode.id = 'hermes-setting-rollout-mode';
        rolloutMode.className = 'settings-input hermes-settings-input';
        const rolloutOptions = {};
        for (const [value, label] of [
            ['disabled', '停用'],
            ['canary', 'Canary 指定 Session'],
            ['percentage', '依比例分批'],
            ['all', '全部啟用'],
        ]) {
            const option = document.createElement('option');
            option.value = value;
            option.textContent = label;
            rolloutMode.appendChild(option);
            rolloutOptions[value] = option;
        }
        const rolloutField = labeledField(
            rolloutMode.id,
            'Rollout 模式',
            rolloutMode,
            [
                '先使用 Canary 指定少量 Session 驗證。',
                '驗證通過後依序擴大為 5%、25%、50%。',
                '最後才切換為全部啟用；需要時可立即降級。',
            ],
        );

        const rolloutPercentage = document.createElement('select');
        rolloutPercentage.id = 'hermes-setting-rollout-percentage';
        rolloutPercentage.className = 'settings-input hermes-settings-input';
        const rolloutPercentageOptions = {};
        for (const percentage of PERCENTAGE_LADDER) {
            const option = document.createElement('option');
            option.value = String(percentage);
            option.textContent = `${percentage}%`;
            rolloutPercentage.appendChild(option);
            rolloutPercentageOptions[String(percentage)] = option;
        }
        const percentageField = labeledField(
            rolloutPercentage.id,
            '啟用比例（%）',
            rolloutPercentage,
            [
                '固定使用 5%、25%、50% 三個階段。',
                '升級時不可跳過中間階段。',
                '降級不受階段限制，可立即降低比例。',
            ],
        );
        const rolloutStageNotice = element('div', 'hermes-rollout-stage-notice');
        rolloutStageNotice.dataset.hermesRolloutGuidance = 'true';
        rolloutStageNotice.setAttribute('role', 'note');

        const canarySessionIds = document.createElement('textarea');
        canarySessionIds.id = 'hermes-setting-canary-session-ids';
        canarySessionIds.className = 'settings-input hermes-settings-input';
        canarySessionIds.rows = 4;
        canarySessionIds.placeholder = '每行一個 Workbench Session ID';
        canarySessionIds.autocomplete = 'off';
        const canaryField = labeledField(
            canarySessionIds.id,
            'Canary Session IDs',
            canarySessionIds,
            [
                '只有列出的 Session 會進入 Hermes。',
                '每行填寫一個完整的 Workbench Session ID。',
                '未列出的 Session 仍使用原本的 Basic Chat 路徑。',
            ],
        );

        const toolsField = toggleField(
            'hermes-setting-tools-enabled',
            '允許 Hermes 工具流程',
            [
                '此功能預設關閉。',
                '工具核准與 allowlist 本身不是安全隔離邊界。',
                '啟用前必須通過 Docker 與 OS 層級隔離驗證。',
            ],
        );
        toolsField.control.disabled = true;
        toolsField.control.checked = false;
        toolsField.control.setAttribute('aria-disabled', 'true');
        toolsField.control.dataset.hermesToolsUnavailable = 'true';
        toolsField.control.title = '目前 Native canary 僅支援純文字；完成 Docker 隔離與即時驗證後才會開放工具。';
        const toolsWarning = element(
            'div',
            'hermes-tools-isolation-warning',
            '安全提醒：Hermes 工具必須在獨立程序、容器或其他 OS 隔離邊界內執行。',
        );
        toolsWarning.setAttribute('role', 'note');
        toolsWarning.textContent = '目前 Native canary 僅支援純文字；完成 Docker 隔離、工具 allowlist 與即時驗證後才會開放。';
        toolsWarning.dataset.hermesOsIsolationWarning = 'true';

        form.append(
            enabledField.group,
            baseUrlField,
            apiKeyField,
            rolloutField,
            rolloutStageNotice,
            percentageField,
            canaryField,
            toolsField.group,
            toolsWarning,
        );

        const statusSection = element('section', 'hermes-status-section');
        statusSection.setAttribute('aria-labelledby', 'hermes-status-title');
        const statusHeading = element('div', 'hermes-status-heading');
        const statusTitle = element('h4', 'hermes-status-title', '服務狀態與健康檢查');
        statusTitle.id = 'hermes-status-title';
        const statusActions = element('div', 'hermes-status-actions');
        const refreshStatusButton = button('更新狀態', 'btn btn-secondary compact', 'status');
        const probeButton = button('執行健康檢查', 'btn btn-secondary compact', 'probe');
        statusActions.append(refreshStatusButton, probeButton);
        statusHeading.append(statusTitle, statusActions);
        const statusBody = element('div', 'hermes-status-body');
        statusBody.setAttribute('aria-live', 'polite');
        statusSection.append(statusHeading, statusBody);

        const actions = element('div', 'hermes-settings-actions');
        const rollbackButton = button(
            '立即回復 Basic Chat',
            'btn btn-danger',
            'rollback',
        );
        const saveButton = button('儲存 Hermes 設定', 'btn btn-primary', 'save');
        actions.append(rollbackButton, saveButton);

        root.append(heading, feedback, form, statusSection, actions);
        state.container.replaceChildren(root);
        state.refs = {
            root,
            form,
            feedback,
            reload,
            enabled: enabledField.control,
            baseUrl,
            apiKeyEnv,
            rolloutMode,
            rolloutOptions,
            rolloutPercentage,
            rolloutPercentageOptions,
            rolloutStageNotice,
            percentageField,
            canarySessionIds,
            canaryField,
            toolsEnabled: toolsField.control,
            toolsWarning,
            refreshStatus: refreshStatusButton,
            probe: probeButton,
            statusBody,
            rollback: rollbackButton,
            save: saveButton,
        };

        form.addEventListener('submit', event => {
            event.preventDefault();
            void save();
        });
        rolloutMode.addEventListener('change', handleRolloutModeChange);
        rolloutPercentage.addEventListener('change', handleRolloutPercentageChange);
        toolsField.control.addEventListener('change', handleToolsChange);
        reload.addEventListener('click', () => { void refresh(); });
        refreshStatusButton.addEventListener('click', () => { void refreshStatus(); });
        probeButton.addEventListener('click', () => { void probe(); });
        rollbackButton.addEventListener('click', () => { void rollback(); });
        saveButton.addEventListener('click', () => { void save(); });

        applySettings(DEFAULTS);
        state.settingsLoaded = false;
        updateBusyState();
        renderStatus();
        state.deps?.createIcons?.();
    }

    async function init(options = {}) {
        const container = resolveContainer(options.container);
        if (!container) throw new Error('Hermes 設定需要有效的容器。');
        const defaultFetch = typeof window.fetch === 'function' ? window.fetch.bind(window) : null;
        const apiFetch = options.apiFetch || window.apiFetch || defaultFetch;
        if (typeof apiFetch !== 'function') throw new Error('Hermes 設定需要 apiFetch。');

        state.settingsRequestId += 1;
        state.statusRequestId += 1;
        state.deps = {
            apiFetch,
            apiBase: apiBase(options.apiBase ?? window.API_BASE ?? ''),
            showToast: options.showToast,
            createIcons: options.createIcons,
            confirmAction: options.confirmAction,
        };
        state.container = container;
        state.settings = {};
        state.settingsLoaded = false;
        state.settingsLoading = false;
        state.settingsSaving = false;
        state.settingsError = null;
        state.status = null;
        state.statusLoading = false;
        state.statusProbing = false;
        state.statusError = null;
        buildPanel();
        await refresh();
        return publicApi;
    }

    function getState() {
        return {
            initialized: Boolean(state.container),
            settingsLoaded: state.settingsLoaded,
            settingsLoading: state.settingsLoading,
            settingsSaving: state.settingsSaving,
            settingsError: state.settingsError,
            statusLoading: state.statusLoading,
            statusProbing: state.statusProbing,
            statusError: state.statusError,
            status: state.status ? { ...state.status } : null,
        };
    }

    function destroy() {
        state.settingsRequestId += 1;
        state.statusRequestId += 1;
        state.container?.replaceChildren();
        state.container = null;
        state.deps = null;
        state.refs = {};
        state.settings = {};
        state.settingsLoaded = false;
        state.status = null;
    }

    const publicApi = Object.freeze({
        init,
        refresh,
        refreshStatus,
        save,
        rollback,
        probe,
        getState,
        destroy,
    });

    window.HermesSettings = publicApi;
    window.workbenchHermesSettings = publicApi;
})();
